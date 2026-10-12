from __future__ import annotations

import asyncio
import logging
import math
import re
from abc import abstractmethod
from collections.abc import Mapping, Sequence
from datetime import date, datetime, timezone
from typing import Any, ClassVar

import httpx

from flightsearch.context import RunContext
from flightsearch.models import Offer, SearchQuery
from flightsearch.sources.base import Source, SourceError

MAX_OFFERS_PER_OD = 20
DEFAULT_APIFY_MAX_CONCURRENT_RUNS = 3
_DEFAULT_MAX_ROUTES_PER_DAY = 4
_DEFAULT_MAX_RESULTS = 10
_DEFAULT_DATE_TIMEOUT_S = 240
_DEFAULT_DATE_CONCURRENCY = 2
# First call, then three retries. Runs already in flight need time to finish
# and free Apify memory.
_CAPACITY_BACKOFF_S = (15.0, 30.0, 60.0)

_CAPACITY_TYPES = frozenset(
    {
        "actor-memory-limit-exceeded",
        "concurrent-runs-limit-exceeded",
        "rate-limit-exceeded",
        "too-many-requests",
    }
)
_CREDIT_TYPES = frozenset({"not-enough-usage-to-run-paid-actor"})
_CREDIT_NEEDLES = (
    "insufficient credit",
    "not enough credit",
    "not-enough-usage",
    "monthly usage",
    "payment required",
)
_CAPACITY_NEEDLES = (
    "memory limit",
    "memory-limit",
    "concurrent actor",
    "concurrent runs",
    "too many requests",
    "rate limit",
)

_slot: asyncio.Semaphore | None = None
_slot_limit: int | None = None
_slot_loop: asyncio.AbstractEventLoop | None = None


class ApifyCapacityError(Exception):
    """Apify refused a run because of memory, concurrency, or rate limits."""


class ApifyCreditError(SourceError):
    """Apify refused a run because the account has no usage left."""


_exhausted_tokens: set[str] = set()
_TOKEN_ENV_KEYS = ("APIFY_TOKEN", "APIFY_TOKEN_BACKUP")


def apify_tokens(env: Mapping[str, str]) -> list[str]:
    """Primary Apify token, then the backup token when that account is set."""
    tokens: list[str] = []
    for key in _TOKEN_ENV_KEYS:
        value = (env.get(key) or "").strip()
        if value and value not in tokens:
            tokens.append(value)
    return tokens


def clear_exhausted_apify_tokens() -> None:
    """Forget which Apify tokens have run out of credits."""
    _exhausted_tokens.clear()


def configure_apify_concurrency(limit: int) -> None:
    """Set the account-wide cap on simultaneous Apify actor runs."""
    global _slot, _slot_limit, _slot_loop
    limit = max(1, int(limit))
    try:
        loop: asyncio.AbstractEventLoop | None = asyncio.get_running_loop()
    except RuntimeError:
        loop = None
    if _slot is not None and _slot_limit == limit and _slot_loop is loop:
        return
    _slot_limit = limit
    if loop is None:
        _slot = None
        _slot_loop = None
        return
    _slot = asyncio.Semaphore(limit)
    _slot_loop = loop


def apify_run_slot() -> asyncio.Semaphore:
    """Lazily create the shared Apify run semaphore on the running loop."""
    loop = asyncio.get_running_loop()
    if _slot is None or _slot_loop is not loop:
        configure_apify_concurrency(_slot_limit or DEFAULT_APIFY_MAX_CONCURRENT_RUNS)
    assert _slot is not None
    return _slot


def _token_chain(token: str, backup_tokens: Sequence[str]) -> list[str]:
    chain: list[str] = []
    for raw in (token, *backup_tokens):
        value = str(raw or "").strip()
        if value and value not in chain:
            chain.append(value)
    return chain


async def call_apify_actor(
    http: httpx.AsyncClient,
    url: str,
    *,
    payload: dict[str, Any],
    token: str,
    timeout_s: int,
    log_name: str,
    log: logging.Logger,
    backup_tokens: Sequence[str] = (),
) -> list[dict[str, Any]]:
    """POST one actor run and return dataset items.

    Memory, concurrency, and HTTP 429 refusals are retried. When the current
    token is out of credits, the same run is sent with the next backup token
    and that exhausted token is skipped for the rest of the process. If every
    token is out of credits, ``ApifyCreditError`` is raised. Other HTTP errors
    raise ``RuntimeError`` so only that date fails.
    """
    chain = _token_chain(token, backup_tokens)
    if not chain:
        raise SourceError("APIFY_TOKEN not set")
    live = [item for item in chain if item not in _exhausted_tokens] or chain[-1:]
    last_credit: ApifyCreditError | None = None
    for index, current in enumerate(live):
        try:
            return await _call_with_token(
                http,
                url,
                payload=payload,
                token=current,
                timeout_s=timeout_s,
                log_name=log_name,
                log=log,
            )
        except ApifyCreditError as exc:
            already = current in _exhausted_tokens
            _exhausted_tokens.add(current)
            last_credit = exc
            if index >= len(live) - 1:
                raise
            if not already:
                log.warning(
                    "%s: Apify credits exhausted; switching to backup API token",
                    log_name,
                )
    assert last_credit is not None
    raise last_credit


async def _call_with_token(
    http: httpx.AsyncClient,
    url: str,
    *,
    payload: dict[str, Any],
    token: str,
    timeout_s: int,
    log_name: str,
    log: logging.Logger,
) -> list[dict[str, Any]]:
    headers = {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json",
    }
    timeout = httpx.Timeout(float(timeout_s) + 30.0)
    attempts = len(_CAPACITY_BACKOFF_S) + 1
    last: ApifyCapacityError | None = None
    for attempt in range(attempts):
        if attempt:
            delay = _CAPACITY_BACKOFF_S[attempt - 1]
            log.warning(
                "%s: Apify at capacity, retry %s/%s in %.0fs: %s",
                log_name,
                attempt,
                len(_CAPACITY_BACKOFF_S),
                delay,
                last,
            )
            await asyncio.sleep(delay)
        async with apify_run_slot():
            try:
                resp = await http.post(
                    url,
                    params={"timeout": timeout_s},
                    headers=headers,
                    json=payload,
                    timeout=timeout,
                )
            except httpx.HTTPError as exc:
                raise RuntimeError(f"HTTP error calling Apify: {exc}") from exc
        try:
            return _dataset_from_response(resp, log_name)
        except ApifyCapacityError as exc:
            last = exc
    assert last is not None
    raise last


def run_url(actor_id: str) -> str:
    actor = actor_id.strip().replace("/", "~")
    return (
        "https://api.apify.com/v2/acts/"
        f"{actor}/run-sync-get-dataset-items"
    )


class DatedRouteSource(Source):
    """Paid Apify search: one actor call per day, budgeted by route.

    A pipeline run takes the next priority route and searches every remaining
    day in the window. ``max_routes_per_day`` counts routes, not dates.
    """

    actor_id: ClassVar[str]
    log_name: ClassVar[str]

    def is_available(self, env: Mapping[str, str]) -> tuple[bool, str]:
        if not apify_tokens(env):
            return False, "APIFY_TOKEN not set"
        return True, ""

    def actor(self) -> str:
        raw = str(self.settings.get("actor") or self.actor_id).strip()
        return raw.replace("/", "~") or self.actor_id

    @abstractmethod
    def build_payload(
        self,
        *,
        origin: str,
        destination: str,
        depart: date,
        adults: int,
        currency: str,
        max_results: int,
    ) -> dict[str, Any]:
        ...

    @abstractmethod
    def parse_item(
        self,
        item: dict[str, Any],
        ctx: RunContext,
        *,
        fallback_origin: str,
        fallback_dest: str,
        searched_date: date,
        adults: int,
        window_from: date,
        window_to: date,
    ) -> Offer | None:
        ...

    async def search(self, query: SearchQuery, ctx: RunContext) -> list[Offer]:
        tokens = apify_tokens(ctx.env)
        if not tokens:
            raise SourceError("APIFY_TOKEN not set")
        token, backup_tokens = tokens[0], tokens[1:]

        today = ctx.now.astimezone(timezone.utc).date()
        dates = query.dates(today)
        pairs = query.route_pairs(priority=True)
        if not dates or not pairs:
            return []

        budget = self._routes_this_pipeline(ctx)
        if budget <= 0:
            ctx.log.info("%s: daily route budget exhausted", self.log_name)
            return []

        offers: list[Offer] = []
        failures = 0
        last_error = ""
        for _ in range(budget):
            origin, dest = self._next_pair(ctx, pairs)
            try:
                items_by_date = await self._search_route(
                    ctx,
                    token=token,
                    backup_tokens=backup_tokens,
                    origin=origin,
                    destination=dest,
                    dates=dates,
                    adults=query.adults,
                    currency=query.currency,
                )
            except SourceError:
                raise
            except Exception as exc:
                failures += 1
                last_error = str(exc)
                ctx.log.warning(
                    "%s: route failed for %s→%s: %s",
                    self.log_name,
                    origin,
                    dest,
                    exc,
                )
                continue

            route_items = 0
            for day, items in items_by_date:
                if isinstance(items, Exception):
                    last_error = str(items)
                    ctx.log.warning(
                        "%s: %s→%s %s failed: %s",
                        self.log_name,
                        origin,
                        dest,
                        day.isoformat(),
                        items,
                    )
                    continue
                route_items += 1
                for item in items:
                    offer = self.parse_item(
                        item,
                        ctx,
                        fallback_origin=origin,
                        fallback_dest=dest,
                        searched_date=day,
                        adults=query.adults,
                        window_from=dates[0],
                        window_to=dates[-1],
                    )
                    if offer is not None:
                        offers.append(offer)
            if route_items == 0:
                failures += 1

        if failures and failures == budget and not offers:
            message = f"{self.log_name}: all {failures} route(s) failed"
            if last_error:
                message = f"{message}: {last_error}"
            raise SourceError(message)
        return self._cap_per_od(offers)

    def _routes_this_pipeline(self, ctx: RunContext) -> int:
        max_per_day = int(
            self.settings.get("max_routes_per_day", _DEFAULT_MAX_ROUTES_PER_DAY)
        )
        day = ctx.now.astimezone(timezone.utc).date().isoformat()
        state = ctx.state
        if state.get("day") != day:
            state["day"] = day
            state["routes_today"] = 0
        if "route_cursor" not in state:
            state["route_cursor"] = 0
        remaining = max(0, max_per_day - int(state.get("routes_today", 0)))
        per_pipeline = max(1, math.ceil(max_per_day / 4))
        return min(per_pipeline, remaining)

    def _next_pair(
        self, ctx: RunContext, pairs: list[tuple[str, str]]
    ) -> tuple[str, str]:
        cursor = int(ctx.state.get("route_cursor", 0))
        pair = pairs[cursor % len(pairs)]
        ctx.state["route_cursor"] = cursor + 1
        ctx.state["routes_today"] = int(ctx.state.get("routes_today", 0)) + 1
        return pair

    async def _search_route(
        self,
        ctx: RunContext,
        *,
        token: str,
        backup_tokens: Sequence[str],
        origin: str,
        destination: str,
        dates: list[date],
        adults: int,
        currency: str,
    ) -> list[tuple[date, list[dict[str, Any]] | Exception]]:
        concurrency = max(
            1, int(self.settings.get("date_concurrency", _DEFAULT_DATE_CONCURRENCY))
        )
        sem = asyncio.Semaphore(concurrency)

        async def one(
            day: date,
        ) -> tuple[date, list[dict[str, Any]] | Exception]:
            async with sem:
                try:
                    items = await self._run_actor(
                        ctx,
                        token=token,
                        backup_tokens=backup_tokens,
                        origin=origin,
                        destination=destination,
                        depart=day,
                        adults=adults,
                        currency=currency,
                    )
                except SourceError:
                    raise
                except Exception as exc:
                    return day, exc
                return day, items

        return list(await asyncio.gather(*(one(day) for day in dates)))

    async def _run_actor(
        self,
        ctx: RunContext,
        *,
        token: str,
        backup_tokens: Sequence[str],
        origin: str,
        destination: str,
        depart: date,
        adults: int,
        currency: str,
    ) -> list[dict[str, Any]]:
        timeout_s = int(self.settings.get("date_timeout_s", _DEFAULT_DATE_TIMEOUT_S))
        max_results = int(self.settings.get("max_results", _DEFAULT_MAX_RESULTS))
        actor = self.actor()
        payload = self.build_payload(
            origin=origin,
            destination=destination,
            depart=depart,
            adults=max(1, int(adults)),
            currency=currency,
            max_results=max(1, max_results),
        )
        ctx.log.info(
            "%s: Apify run %s %s→%s %s (maxResults=%s)",
            self.log_name,
            actor,
            origin,
            destination,
            depart.isoformat(),
            payload.get("maxResults"),
        )
        return await call_apify_actor(
            ctx.http,
            run_url(actor),
            payload=payload,
            token=token,
            backup_tokens=backup_tokens,
            timeout_s=timeout_s,
            log_name=self.log_name,
            log=ctx.log,
        )

    @staticmethod
    def _cap_per_od(offers: list[Offer]) -> list[Offer]:
        by_od: dict[tuple[str, str], list[Offer]] = {}
        for offer in offers:
            by_od.setdefault((offer.origin, offer.destination), []).append(offer)
        out: list[Offer] = []
        for group in by_od.values():
            group.sort(key=lambda o: o.price_usd)
            out.extend(group[:MAX_OFFERS_PER_OD])
        out.sort(key=lambda o: o.price_usd)
        return out


def _dataset_items(data: Any) -> list[dict[str, Any]]:
    if isinstance(data, list):
        return [x for x in data if isinstance(x, dict)]
    if isinstance(data, dict) and isinstance(data.get("items"), list):
        return [x for x in data["items"] if isinstance(x, dict)]
    return []


def _dataset_from_response(resp: httpx.Response, log_name: str) -> list[dict[str, Any]]:
    if resp.status_code < 400:
        return _dataset_items(resp.json())
    err_type, message = _apify_error_parts(resp)
    if _is_capacity(resp.status_code, err_type, message):
        raise ApifyCapacityError(
            _format_apify_failure(
                log_name, resp.status_code, err_type, message, credit=False
            )
        )
    if _is_credit(resp.status_code, err_type, message):
        raise ApifyCreditError(
            _format_apify_failure(
                log_name, resp.status_code, err_type, message, credit=True
            )
        )
    raise RuntimeError(
        _format_apify_failure(
            log_name, resp.status_code, err_type, message, credit=False
        )
    )


def _apify_error_parts(resp: httpx.Response) -> tuple[str, str]:
    err_type = ""
    message = ""
    data: Any = None
    try:
        data = resp.json()
    except Exception:
        data = None
    if isinstance(data, dict):
        err = data.get("error")
        if isinstance(err, dict):
            err_type = str(err.get("type") or "").strip()
            message = str(err.get("message") or "").strip()
        elif isinstance(err, str) and err.strip():
            message = err.strip()
    if not message:
        message = (resp.text or "").replace("\n", " ").strip()
    return err_type, message[:300]


def _is_capacity(status: int, err_type: str, message: str) -> bool:
    if status == 429:
        return True
    type_key = err_type.strip().lower()
    if type_key in _CAPACITY_TYPES:
        return True
    if any(part in type_key for part in ("memory-limit", "concurrent", "rate-limit")):
        return True
    blob = message.lower()
    return any(needle in blob for needle in _CAPACITY_NEEDLES)


def _is_credit(status: int, err_type: str, message: str) -> bool:
    type_key = err_type.strip().lower()
    if type_key in _CREDIT_TYPES or "not-enough-usage" in type_key:
        return True
    blob = f"{type_key} {message}".lower()
    if any(needle in blob for needle in _CREDIT_NEEDLES):
        return True
    return status == 402


def _format_apify_failure(
    log_name: str,
    status: int,
    err_type: str,
    message: str,
    *,
    credit: bool,
) -> str:
    if credit:
        head = (
            f"{log_name}: Apify insufficient credit / payment required (HTTP {status})"
        )
    else:
        head = f"{log_name}: Apify HTTP {status}"
    detail = " — ".join(part for part in (err_type, message[:200]) if part)
    if detail:
        return f"{head}: {detail}"
    return head


_IATA_RE = re.compile(r"^[A-Za-z]{3}$")
_PRICE_RE = re.compile(r"(\d[\d,]*(?:\.\d+)?)")
_DURATION_RE = re.compile(r"(?:(\d+)\s*h)?\s*(?:(\d+)\s*m)?", re.IGNORECASE)


def as_amount(value: Any) -> float | None:
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, (int, float)):
        return float(value)
    match = _PRICE_RE.search(str(value))
    if not match:
        return None
    try:
        return float(match.group(1).replace(",", ""))
    except ValueError:
        return None


def price_and_currency(item: dict[str, Any]) -> tuple[float, str] | None:
    currency = str(item.get("currency") or item.get("search_currency") or "").strip().upper()
    raw = item.get("price")
    if raw is None:
        raw = item.get("bestPrice") or item.get("totalPrice")
    if raw is None and isinstance(item.get("bestOffer"), dict):
        offer = item["bestOffer"]
        raw = offer.get("price")
        if not currency:
            currency = str(offer.get("currency") or "").strip().upper()
    if isinstance(raw, dict):
        if not currency:
            currency = str(raw.get("currency") or "").strip().upper()
        amount = raw.get("amount")
        if amount is None:
            amount = raw.get("total") or raw.get("perPassenger") or raw.get("value")
        raw = amount
    amount = as_amount(raw)
    if amount is None:
        return None
    return amount, currency or "USD"


def iata(value: Any, fallback: str) -> str:
    if isinstance(value, dict):
        value = (
            value.get("code")
            or value.get("airport")
            or value.get("iata")
            or value.get("displayCode")
        )
    if isinstance(value, str) and _IATA_RE.match(value.strip()):
        return value.strip().upper()
    return fallback


def parse_local_dt(value: Any) -> datetime | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        return value.replace(tzinfo=None) if value.tzinfo else value
    text = str(value).strip()
    if not text:
        return None
    if text.endswith("Z"):
        text = text[:-1]
    if "+" in text[10:]:
        text = text[: text.find("+", 10)]
    try:
        dt = datetime.fromisoformat(text)
    except ValueError:
        return None
    return dt.replace(tzinfo=None) if dt.tzinfo else dt


def clock_on_date(day: date, value: Any) -> datetime | None:
    parsed = parse_local_dt(value)
    if parsed is not None and (parsed.year > 1900 and ("T" in str(value) or len(str(value)) > 8)):
        return parsed
    text = str(value or "").strip()
    if not text or ":" not in text:
        return parsed
    hhmm = text.split("T")[-1][:5]
    try:
        hour_s, minute_s = hhmm.split(":", 1)
        return datetime(day.year, day.month, day.day, int(hour_s), int(minute_s[:2]))
    except ValueError:
        return parsed


def duration_minutes(value: Any) -> int | None:
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, (int, float)):
        return int(value)
    match = _DURATION_RE.search(str(value).strip())
    if not match or (match.group(1) is None and match.group(2) is None):
        return None
    return int(match.group(1) or 0) * 60 + int(match.group(2) or 0)


def depart_date_of(item: dict[str, Any], fallback: date) -> date:
    for key in (
        "departureDate",
        "departDate",
        "search_depart_date",
        "searchDepartDate",
        "date",
    ):
        raw = item.get(key)
        if not raw:
            continue
        try:
            return date.fromisoformat(str(raw)[:10])
        except ValueError:
            continue
    dt = parse_local_dt(item.get("departureTime") or item.get("departTime"))
    if dt is not None and dt.year > 1900 and len(str(item.get("departureTime") or item.get("departTime"))) > 8:
        return dt.date()
    return fallback
