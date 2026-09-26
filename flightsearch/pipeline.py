from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import date, datetime, timezone
from typing import Any
from zoneinfo import ZoneInfo

import httpx

from flightsearch.config import AppConfig
from flightsearch.context import RunContext
from flightsearch.fx import LiveFx
from flightsearch.hops import attach_hops
from flightsearch.models import Offer, SearchQuery
from flightsearch.report import format_deals, format_health_alert, format_heartbeat
from flightsearch.sources import SOURCE_CLASSES, load_source
from flightsearch.state import State

BANGKOK = ZoneInfo("Asia/Bangkok")
DEFAULT_SOURCE_TIMEOUT_S = 900
HOPS_STATUS_NAME = "hops"
log = logging.getLogger("flightsearch")


@dataclass
class RunResult:
    window_over: bool
    offers: list[Offer]
    qualifying: list[Offer]
    new_alerts: list[Offer]
    near_misses: list[Offer]
    dropped_count: int
    best_per_source: dict[str, Offer]
    source_status: dict[str, dict]
    messages_sent: int
    started_at: datetime
    finished_at: datetime
    also_seen: dict[str, list[dict]] = field(default_factory=dict)


@dataclass
class _SourceOutcome:
    name: str
    status: str
    count: int = 0
    seconds: float = 0.0
    error: str | None = None
    offers: list[Offer] = field(default_factory=list)
    health_alert: bool = False


async def fetch_hops(query: SearchQuery, ctx: RunContext) -> list[Offer]:
    from flightsearch.sources.kiwi import fetch_hops as _fetch_hops

    return await _fetch_hops(query, ctx)


async def run_search(
    config: AppConfig,
    *,
    env: Mapping[str, str],
    state: State,
    now: datetime,
    only_sources: list[str] | None = None,
    notifier: Any | None = None,
    dry_run: bool = False,
    http: httpx.AsyncClient | None = None,
) -> RunResult:
    started_at = now
    ctx_now = _as_bangkok(now)
    today = ctx_now.date()
    window_over = not bool(config.query.dates(today))

    owns_http = http is None
    if owns_http:
        http = httpx.AsyncClient(timeout=60.0)
    assert http is not None

    try:
        result, health_needed, new_alerts = await _execute(
            config,
            env=env,
            state=state,
            now=now,
            only_sources=only_sources,
            http=http,
            window_over=window_over,
            started_at=started_at,
            ctx_now=ctx_now,
        )
        if notifier is not None and not dry_run:
            result.messages_sent = await _notify(
                notifier,
                result,
                state,
                now,
                new_alerts,
                health_needed,
                window_over,
                config,
            )
        if not dry_run:
            state.runs += 1
            state.prune(today)
        return result
    finally:
        if owns_http:
            await http.aclose()


async def _execute(
    config: AppConfig,
    *,
    env: Mapping[str, str],
    state: State,
    now: datetime,
    only_sources: list[str] | None,
    http: httpx.AsyncClient,
    window_over: bool,
    started_at: datetime,
    ctx_now: datetime,
) -> tuple[RunResult, list[_SourceOutcome], list[Offer]]:
    source_status: dict[str, dict] = {}
    offers: list[Offer] = []
    qualifying: list[Offer] = []
    near_misses: list[Offer] = []
    new_alerts: list[Offer] = []
    dropped_count = 0
    best_per_source: dict[str, Offer] = {}
    health_needed: list[_SourceOutcome] = []
    also_seen: dict[str, list[dict]] = {}

    if not window_over:
        fx = await LiveFx.load(http)
        planned = _plan_sources(config, only_sources)
        hops_task = asyncio.create_task(
            _run_hops(config, http, fx, state, now, ctx_now, env)
        )
        try:
            outcomes = await _run_sources(
                planned, config, env, state, now, ctx_now, http, fx
            )
            hop_outcome = await hops_task
        except BaseException:
            hops_task.cancel()
            raise
        raw_offers: list[Offer] = []
        for outcome in outcomes:
            source_status[outcome.name] = {
                "status": outcome.status,
                "count": outcome.count,
                "seconds": outcome.seconds,
                "error": outcome.error,
            }
            raw_offers.extend(outcome.offers)
        source_status[hop_outcome.name] = {
            "status": hop_outcome.status,
            "count": hop_outcome.count,
            "seconds": hop_outcome.seconds,
            "error": hop_outcome.error,
        }
        hops = list(hop_outcome.offers)
        kept, dropped = attach_hops(
            raw_offers,
            hops,
            config.query,
            config.hop_min_connection_minutes,
            config.hop_max_connection_hours,
        )
        dropped_count = len(dropped)
        best_per_source = _best_per_source(kept)
        offers, also_seen = _dedupe(kept, state)
        qualifying = [o for o in offers if o.total_usd <= config.query.max_total_usd]
        near_misses = [
            o
            for o in offers
            if config.query.max_total_usd < o.total_usd <= config.query.near_miss_usd
        ]
        new_alerts = [o for o in qualifying if state.should_alert(o)]
        health_needed = [o for o in [*outcomes, hop_outcome] if o.health_alert]

    result = RunResult(
        window_over=window_over,
        offers=offers,
        qualifying=qualifying,
        new_alerts=new_alerts,
        near_misses=near_misses,
        dropped_count=dropped_count,
        best_per_source=best_per_source,
        source_status=source_status,
        messages_sent=0,
        started_at=started_at,
        finished_at=datetime.now(timezone.utc),
        also_seen=also_seen,
    )
    return result, health_needed, new_alerts


def _as_bangkok(now: datetime) -> datetime:
    aware = now if now.tzinfo is not None else now.replace(tzinfo=timezone.utc)
    return aware.astimezone(BANGKOK)


def _plan_sources(
    config: AppConfig,
    only_sources: list[str] | None,
) -> list[_SourceOutcome | tuple[str, dict]]:
    names = list(only_sources) if only_sources is not None else list(config.sources)
    planned: list[_SourceOutcome | tuple[str, dict]] = []
    for name in names:
        settings = dict(config.sources.get(name) or {})
        if name not in config.sources and name not in SOURCE_CLASSES:
            planned.append(
                _SourceOutcome(name=name, status="skipped", error="unknown source")
            )
            continue
        if name in config.sources and not settings.get("enabled", True):
            planned.append(_SourceOutcome(name=name, status="skipped", error="disabled"))
            continue
        if name not in SOURCE_CLASSES:
            planned.append(
                _SourceOutcome(name=name, status="skipped", error="unknown source")
            )
            continue
        planned.append((name, settings))
    return planned


async def _run_hops(
    config: AppConfig,
    http: httpx.AsyncClient,
    fx: LiveFx,
    state: State,
    now: datetime,
    ctx_now: datetime,
    env: Mapping[str, str],
) -> _SourceOutcome:
    timeout = float(config.hop_fetch_timeout_s)
    ctx = RunContext(
        http=http,
        fx=fx,
        state=state.source_data("kiwi"),
        log=logging.getLogger("flightsearch.hops"),
        now=ctx_now,
        env=env,
    )
    t0 = time.perf_counter()
    try:
        found = await asyncio.wait_for(fetch_hops(config.query, ctx), timeout=timeout)
        seconds = time.perf_counter() - t0
        state.record_source_result(HOPS_STATUS_NAME, True, None, now)
        return _SourceOutcome(
            name=HOPS_STATUS_NAME,
            status="ok",
            count=len(found),
            seconds=seconds,
            offers=list(found),
        )
    except Exception as exc:
        seconds = time.perf_counter() - t0
        if isinstance(exc, TimeoutError):
            err = f"timeout after {timeout:g}s"
        else:
            err = str(exc) or type(exc).__name__
        log.warning("hop fetch failed: %s", err)
        return _error_outcome(state, HOPS_STATUS_NAME, now, err, seconds=seconds)


async def _run_sources(
    planned: list[_SourceOutcome | tuple[str, dict]],
    config: AppConfig,
    env: Mapping[str, str],
    state: State,
    now: datetime,
    ctx_now: datetime,
    http: httpx.AsyncClient,
    fx: LiveFx,
) -> list[_SourceOutcome]:
    skipped = [p for p in planned if isinstance(p, _SourceOutcome)]
    runnable = [p for p in planned if isinstance(p, tuple)]
    ran = await asyncio.gather(
        *(
            _run_one(name, settings, config, env, state, now, ctx_now, http, fx)
            for name, settings in runnable
        )
    )
    by_name = {o.name: o for o in [*skipped, *ran]}
    ordered: list[_SourceOutcome] = []
    for item in planned:
        name = item.name if isinstance(item, _SourceOutcome) else item[0]
        ordered.append(by_name[name])
    return ordered


async def _run_one(
    name: str,
    settings: dict,
    config: AppConfig,
    env: Mapping[str, str],
    state: State,
    now: datetime,
    ctx_now: datetime,
    http: httpx.AsyncClient,
    fx: LiveFx,
) -> _SourceOutcome:
    try:
        source = load_source(name, settings)
    except Exception as exc:
        return _error_outcome(state, name, now, str(exc) or type(exc).__name__)
    try:
        available, reason = source.is_available(env)
    except Exception as exc:
        return _error_outcome(state, name, now, str(exc) or type(exc).__name__)
    if not available:
        return _SourceOutcome(name=name, status="skipped", error=reason or "unavailable")

    timeout = float(settings.get("timeout_s", DEFAULT_SOURCE_TIMEOUT_S))
    ctx = RunContext(
        http=http,
        fx=fx,
        state=state.source_data(name),
        log=logging.getLogger(f"flightsearch.sources.{name}"),
        now=ctx_now,
        env=env,
    )
    t0 = time.perf_counter()
    try:
        found = await asyncio.wait_for(source.search(config.query, ctx), timeout=timeout)
        seconds = time.perf_counter() - t0
        state.record_source_result(name, True, None, now)
        return _SourceOutcome(
            name=name,
            status="ok",
            count=len(found),
            seconds=seconds,
            offers=list(found),
        )
    except Exception as exc:
        seconds = time.perf_counter() - t0
        if isinstance(exc, TimeoutError):
            err = f"timeout after {timeout:g}s"
        else:
            err = str(exc) or type(exc).__name__
        log.warning("source %s failed: %s", name, err)
        return _error_outcome(state, name, now, err, seconds=seconds)


def _error_outcome(
    state: State,
    name: str,
    now: datetime,
    error: str,
    *,
    seconds: float = 0.0,
) -> _SourceOutcome:
    state.record_source_result(name, False, error, now)
    health = state.sources[name]["health"]
    return _SourceOutcome(
        name=name,
        status="error",
        seconds=seconds,
        error=error,
        health_alert=health.get("consecutive_failures") == 3,
    )


def _best_per_source(offers: list[Offer]) -> dict[str, Offer]:
    best: dict[str, Offer] = {}
    for offer in offers:
        current = best.get(offer.source)
        if current is None or offer.total_usd < current.total_usd:
            best[offer.source] = offer
    return best


def _dedupe(
    offers: list[Offer], state: State
) -> tuple[list[Offer], dict[str, list[dict]]]:
    groups: dict[str, list[Offer]] = {}
    for offer in offers:
        groups.setdefault(state.alert_key(offer), []).append(offer)
    kept: list[Offer] = []
    also_seen: dict[str, list[dict]] = {}
    for key, group in groups.items():
        winner = min(group, key=lambda o: (o.total_usd, o.source, o.origin))
        extras = [
            {
                "source": other.source,
                "total_usd": other.total_usd,
                "link": other.link,
                "cabin_bag_included": other.cabin_bag_included,
            }
            for other in sorted(group, key=lambda o: (o.total_usd, o.source))
            if other.source != winner.source
        ]
        also_seen[key] = extras
        kept.append(winner)
    kept.sort(key=lambda o: (o.total_usd, o.source, o.origin, o.destination))
    return kept, also_seen


async def _notify(
    notifier: Any,
    result: RunResult,
    state: State,
    now: datetime,
    new_alerts: list[Offer],
    health_needed: list[_SourceOutcome],
    window_over: bool,
    config: AppConfig,
) -> int:
    sent = 0
    if new_alerts:
        extras = [result.also_seen.get(state.alert_key(o), []) for o in new_alerts]
        await notifier.send(format_deals(new_alerts, extras))
        sent += 1
        for offer in new_alerts:
            state.mark_alerted(offer, now)
    for outcome in health_needed:
        await notifier.send(format_health_alert(outcome.name, outcome.error or ""))
        sent += 1
    if _should_heartbeat(state, now, window_over, config):
        await notifier.send(format_heartbeat(result))
        sent += 1
        state.last_heartbeat_date = _utc_date(now).isoformat()
    return sent


def _should_heartbeat(
    state: State, now: datetime, window_over: bool, config: AppConfig
) -> bool:
    utc_today = _utc_date(now).isoformat()
    if state.last_heartbeat_date == utc_today:
        return False
    hour = now.astimezone(timezone.utc).hour if now.tzinfo else now.hour
    telegram = (config.notify or {}).get("telegram") or {}
    try:
        hb_hour = int(telegram.get("heartbeat_utc_hour", 0))
    except (TypeError, ValueError):
        hb_hour = 0
    if window_over:
        return True
    return hour >= hb_hour


def _utc_date(now: datetime) -> date:
    aware = now if now.tzinfo is not None else now.replace(tzinfo=timezone.utc)
    return aware.astimezone(timezone.utc).date()
