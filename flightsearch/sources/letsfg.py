from __future__ import annotations

import asyncio
import os
import time
from collections.abc import Mapping
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any, ClassVar
from urllib.parse import urljoin

import httpx

from flightsearch.context import RunContext
from flightsearch.models import Offer, SearchQuery
from flightsearch.sources.base import Source, SourceError

DISCOVERY_URL = (
    "https://letsfg.co/developers/api/.well-known/oauth-authorization-server"
)
TOKEN_URL = "https://letsfg.co/developers/api/oauth/token"
SEARCH_URL = "https://letsfg.co/api/search"
RESULTS_URL = "https://letsfg.co/api/results/{search_id}"

DAILY_CAP = 95
STARTS_PER_10MIN = 10
WINDOW_10MIN_S = 600.0
STARTS_PER_HOUR = 30
WINDOW_HOUR_S = 3600.0
POLL_INTERVAL_S = 10.0
POLL_SLOW_AFTER_S = 180.0
POLL_SLOW_INTERVAL_S = 20.0
POLL_TIMEOUT_S = 240.0
CHEAPEST_PER_CELL = 5
DEFAULT_MAX_SEARCHES = 20
DEFAULT_TIMEOUT_S = 1800.0

_TRUE = {True, "true", "True", "yes", "YES", "1", 1}
_SEPARATE = {"unprotected", "protected", "separate", "separate tickets"}


class RateLimited(Exception):
    """LetsFG returned 429; the run should stop and keep the cursor."""

    def __init__(self, retry_after_s: float | None = None) -> None:
        super().__init__("letsfg rate limited")
        self.retry_after_s = retry_after_s


def _monotonic() -> float:
    return time.monotonic()


async def _sleep(seconds: float) -> None:
    if seconds > 0:
        await asyncio.sleep(seconds)


def _env(env: Mapping[str, str], key: str) -> str:
    return (env.get(key) or "").strip()


def _parse_local_dt(value: Any) -> datetime | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        return value.replace(tzinfo=None)
    text = str(value).strip()
    if not text:
        return None
    if text.endswith("Z"):
        text = text[:-1]
    if "+" in text[10:] or (text.count("-") > 2 and "T" in text):
        for sep in ("+", "-"):
            idx = text.find(sep, 10)
            if idx != -1:
                text = text[:idx]
                break
    try:
        return datetime.fromisoformat(text)
    except ValueError:
        return None


def _as_float(value: Any) -> float | None:
    if value is None or value == "":
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _as_int(value: Any) -> int | None:
    if value is None or value == "":
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def cells_for(query: SearchQuery, today: date) -> list[tuple[str, str, date]]:
    """Origin × destination × date grid. WMI is covered by WAW all-airports."""
    dates = query.dates(today)
    origin = query.home_origin
    return [(origin, dest, day) for day in dates for dest in query.destinations]


def _write_rotated_refresh(path: str, token: str) -> None:
    dest = Path(path)
    dest.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(str(dest), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        fh.write(token)
    os.chmod(dest, 0o600)


def _self_transfer(raw: dict[str, Any]) -> bool | None:
    split = raw.get("split_ticket")
    if split in _TRUE or split == "true":
        return True
    combo = str(raw.get("combo_type") or "").lower()
    if combo in {"virtual_interlining", "self_transfer", "separate_tickets"}:
        return True
    flag = raw.get("self_transfer")
    if flag is None:
        notes = str(raw.get("notes") or raw.get("flag") or "").lower()
        if "separate ticket" in notes or "self-transfer" in notes or "self transfer" in notes:
            return True
        return None
    if isinstance(flag, str) and flag.lower() in _SEPARATE:
        return True
    if flag in _TRUE:
        return True
    if flag in {False, "false", "False", "no", 0, "0"}:
        return False
    return bool(flag)


def _cabin_bag_included(raw: dict[str, Any]) -> bool | None:
    for key in (
        "cabin_bag_included",
        "hand_bag_included",
        "cabin_bag",
        "included_cabin_bag",
    ):
        if key in raw and raw[key] is not None:
            val = raw[key]
            if isinstance(val, (int, float)) and not isinstance(val, bool):
                return val > 0
            return val in _TRUE
    bags = raw.get("bags") or raw.get("baggage") or raw.get("baggages")
    if not isinstance(bags, dict):
        return None
    for key in ("cabin", "hand", "cabin_bag", "hand_bag", "included_cabin", "cabin_included"):
        if key in bags and bags[key] is not None:
            val = bags[key]
            if isinstance(val, (int, float)) and not isinstance(val, bool):
                return val > 0
            return val in _TRUE
    return None


def _segments(raw: dict[str, Any]) -> list[dict[str, Any]]:
    segs = raw.get("segments")
    if isinstance(segs, list):
        return [s for s in segs if isinstance(s, dict)]
    outbound = raw.get("outbound")
    if isinstance(outbound, dict):
        inner = outbound.get("segments")
        if isinstance(inner, list):
            return [s for s in inner if isinstance(s, dict)]
    legs = raw.get("legs")
    if isinstance(legs, list):
        out: list[dict[str, Any]] = []
        for leg in legs:
            if not isinstance(leg, dict):
                continue
            inner = leg.get("segments")
            if isinstance(inner, list):
                out.extend(s for s in inner if isinstance(s, dict))
            else:
                out.append(leg)
        return out
    return []


def _airlines_and_flights(raw: dict[str, Any]) -> tuple[list[str], list[str]]:
    airlines: list[str] = []
    flights: list[str] = []

    def add_airline(value: Any) -> None:
        text = str(value or "").strip()
        if not text:
            return
        if text not in airlines:
            airlines.append(text)

    def add_flight(value: Any) -> None:
        text = str(value or "").strip().upper().replace(" ", "")
        if text and text not in flights:
            flights.append(text)

    codes = raw.get("airlines") or raw.get("airline_codes")
    if isinstance(codes, list):
        for item in codes:
            add_airline(item)
    elif isinstance(codes, str):
        for part in codes.split(","):
            add_airline(part.strip())

    add_airline(raw.get("airline_code"))
    if not airlines:
        add_airline(raw.get("airline") or raw.get("owner_airline"))

    existing = raw.get("flight_numbers") or raw.get("flight_nos")
    if isinstance(existing, list):
        for item in existing:
            add_flight(item)
    elif isinstance(existing, str):
        for part in existing.replace("/", ",").split(","):
            add_flight(part)

    add_flight(raw.get("flight_number") or raw.get("flight_no"))

    for seg in _segments(raw):
        code = str(seg.get("airline_code") or seg.get("carrier") or "").strip()
        name = str(seg.get("airline") or "").strip()
        add_airline(code or name)
        fn = (
            seg.get("flight_number")
            or seg.get("flightNumber")
            or seg.get("flight_no")
        )
        if fn:
            text = str(fn).strip()
            if code and not text.upper().startswith(code.upper()):
                add_flight(f"{code}{text}")
            else:
                add_flight(text)
        elif code and seg.get("number") is not None:
            add_flight(f"{code}{seg.get('number')}")

    return airlines, flights


def _offer_link(raw: dict[str, Any], search_id: str | None) -> str | None:
    for key in ("link", "url", "booking_url", "bookingUrl"):
        val = raw.get(key)
        if isinstance(val, str) and val.startswith("http"):
            return val
    oid = raw.get("id") or raw.get("offer_id")
    if search_id and oid:
        return (
            f"https://letsfg.co/en?stage=results&sid={search_id}"
            f"&offer={oid}&cur=USD"
        )
    if search_id:
        return f"https://letsfg.co/results/{search_id}"
    return None


def offer_from_raw(
    raw: dict[str, Any],
    *,
    fx: Any,
    requested_origin: str,
    requested_dest: str,
    requested_date: date,
    search_id: str | None = None,
    source: str = "letsfg",
) -> Offer | None:
    price = _as_float(
        raw.get("price")
        if raw.get("price") is not None
        else raw.get("total_price")
        if raw.get("total_price") is not None
        else raw.get("total")
    )
    if price is None:
        return None
    currency = str(raw.get("currency") or raw.get("currency_code") or "USD").upper()
    price_usd = float(fx.to_usd(price, currency))

    origin = str(
        raw.get("origin") or raw.get("from") or requested_origin
    ).upper()
    destination = str(
        raw.get("destination") or raw.get("to") or requested_dest
    ).upper()
    segs = _segments(raw)
    if segs:
        origin = str(segs[0].get("origin") or segs[0].get("from") or origin).upper()
        destination = str(
            segs[-1].get("destination") or segs[-1].get("to") or destination
        ).upper()

    depart_time = _parse_local_dt(
        raw.get("departure_time") or raw.get("depart_time") or raw.get("departureTime")
    )
    arrive_time = _parse_local_dt(
        raw.get("arrival_time") or raw.get("arrive_time") or raw.get("arrivalTime")
    )
    if depart_time is None and segs:
        depart_time = _parse_local_dt(
            segs[0].get("departure_time") or segs[0].get("departureTime")
        )
    if arrive_time is None and segs:
        arrive_time = _parse_local_dt(
            segs[-1].get("arrival_time") or segs[-1].get("arrivalTime")
        )

    depart_date = depart_time.date() if depart_time is not None else requested_date
    airlines, flight_numbers = _airlines_and_flights(raw)
    stops = raw.get("stops")
    if stops is None and segs:
        stops = max(0, len(segs) - 1)
    duration = _as_int(raw.get("duration_minutes") or raw.get("duration"))
    self_xfer = _self_transfer(raw)

    notes: list[str] = []
    if destination != requested_dest.upper():
        notes.append(f"arrives {destination}")
    if self_xfer:
        notes.append("self-transfer / separate tickets")
    extra = str(raw.get("notes") or "").strip()
    if extra:
        notes.append(extra)

    return Offer(
        source=source,
        origin=origin,
        destination=destination,
        depart_date=depart_date,
        price_usd=price_usd,
        price_original=price,
        currency_original=currency,
        depart_time=depart_time,
        arrive_time=arrive_time,
        airlines=airlines,
        flight_numbers=flight_numbers,
        stops=_as_int(stops),
        duration_minutes=duration,
        link=_offer_link(raw, search_id),
        cabin_bag_included=_cabin_bag_included(raw),
        self_transfer=self_xfer,
        notes="; ".join(notes),
    )


def parse_results(
    payload: Any,
    *,
    fx: Any,
    requested_origin: str,
    requested_dest: str,
    requested_date: date,
    search_id: str | None = None,
    source: str = "letsfg",
    limit: int = CHEAPEST_PER_CELL,
) -> list[Offer]:
    if not isinstance(payload, dict):
        return []
    rows = payload.get("offers")
    if not isinstance(rows, list):
        rows = payload.get("results") or []
    sid = search_id or payload.get("search_id")
    offers: list[Offer] = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        offer = offer_from_raw(
            row,
            fx=fx,
            requested_origin=requested_origin,
            requested_dest=requested_dest,
            requested_date=requested_date,
            search_id=str(sid) if sid else None,
            source=source,
        )
        if offer is not None:
            offers.append(offer)
    offers.sort(key=lambda o: (o.price_usd, o.depart_date.isoformat()))
    return offers[:limit]


def _retry_after_s(resp: httpx.Response) -> float | None:
    raw = resp.headers.get("Retry-After")
    if not raw:
        return None
    try:
        return max(0.0, float(raw))
    except ValueError:
        return None


class LetsFGSource(Source):
    """LetsFG PFS lane: OAuth refresh + POST /api/search + GET /api/results."""

    name: ClassVar[str] = "letsfg"

    def __init__(self, settings: dict | None = None) -> None:
        super().__init__(settings)
        self._access_token: str | None = None
        self._refresh_token: str = ""
        self._client_id: str = ""
        self._token_lock = asyncio.Lock()

    def is_available(self, env: Mapping[str, str]) -> tuple[bool, str]:
        if not _env(env, "LETSFG_REFRESH_TOKEN"):
            return False, "LETSFG_REFRESH_TOKEN not set"
        if not _env(env, "LETSFG_CLIENT_ID"):
            return False, "LETSFG_CLIENT_ID not set"
        return True, ""

    def _max_searches(self) -> int:
        return max(0, int(self.settings.get("max_searches_per_run", DEFAULT_MAX_SEARCHES)))

    def _timeout_s(self) -> float:
        return float(self.settings.get("timeout_s", DEFAULT_TIMEOUT_S))

    def _sync_state(self, ctx: RunContext, cursor: int, day: str, searches_today: int) -> None:
        ctx.state["cursor"] = cursor
        ctx.state["day"] = day
        ctx.state["searches_today"] = searches_today

    def _load_budget(self, ctx: RunContext, today: date) -> tuple[int, int]:
        day = today.isoformat()
        prev_day = str(ctx.state.get("day") or "")
        cursor = int(ctx.state.get("cursor") or 0)
        searches_today = int(ctx.state.get("searches_today") or 0)
        if prev_day != day:
            searches_today = 0
        self._sync_state(ctx, cursor, day, searches_today)
        return cursor, searches_today

    async def _refresh_access(self, ctx: RunContext, *, force: bool = False) -> str:
        async with self._token_lock:
            if self._access_token and not force:
                return self._access_token
            resp = await ctx.http.post(
                TOKEN_URL,
                data={
                    "grant_type": "refresh_token",
                    "refresh_token": self._refresh_token,
                    "client_id": self._client_id,
                },
                headers={"Content-Type": "application/x-www-form-urlencoded"},
            )
            if resp.status_code == 401:
                raise SourceError("letsfg: refresh token rejected (401)")
            if resp.status_code == 402:
                raise SourceError("letsfg: payment required on token refresh (402)")
            if resp.status_code == 429:
                raise RateLimited(_retry_after_s(resp))
            if resp.status_code >= 400:
                raise SourceError(
                    f"letsfg: token refresh failed HTTP {resp.status_code}"
                )
            data = resp.json()
            access = data.get("access_token")
            if not access:
                raise SourceError("letsfg: token refresh missing access_token")
            self._access_token = str(access)
            rotated = data.get("refresh_token")
            if rotated and str(rotated) != self._refresh_token:
                self._refresh_token = str(rotated)
                out = _env(ctx.env, "LETSFG_REFRESH_TOKEN_OUT")
                if out:
                    _write_rotated_refresh(out, self._refresh_token)
            return self._access_token

    def _auth_headers(self) -> dict[str, str]:
        if not self._access_token:
            raise SourceError("letsfg: missing access token")
        return {
            "Authorization": f"Bearer {self._access_token}",
            "Content-Type": "application/json",
            "Accept": "application/json",
        }

    async def _request(
        self,
        ctx: RunContext,
        method: str,
        url: str,
        *,
        json_body: dict[str, Any] | None = None,
        _retried: bool = False,
    ) -> httpx.Response:
        req = ctx.http.build_request(
            method, url, headers=self._auth_headers(), json=json_body
        )
        resp = await ctx.http.send(req)
        if resp.status_code == 401 and not _retried:
            await self._refresh_access(ctx, force=True)
            return await self._request(
                ctx, method, url, json_body=json_body, _retried=True
            )
        if resp.status_code == 429:
            raise RateLimited(_retry_after_s(resp))
        if resp.status_code == 402:
            raise SourceError("letsfg: payment required (402)")
        return resp

    async def _start_search(
        self,
        ctx: RunContext,
        *,
        origin: str,
        destination: str,
        day: date,
        adults: int,
        currency: str,
    ) -> str:
        body = {
            "origin": origin,
            "destination": destination,
            "date_from": day.isoformat(),
            "adults": adults,
            "currency": currency or "USD",
            "cabin_class": "M",
            "origin_all_airports": True,
            "destination_all_airports": True,
            "response_mode": "full",
        }
        resp = await self._request(ctx, "POST", SEARCH_URL, json_body=body)
        if resp.status_code >= 400:
            raise SourceError(
                f"letsfg: search start failed HTTP {resp.status_code}"
            )
        data = resp.json()
        sid = data.get("search_id") or data.get("id")
        if not sid:
            raise SourceError("letsfg: search start missing search_id")
        return str(sid)

    async def _poll_results(
        self,
        ctx: RunContext,
        search_id: str,
        *,
        origin: str,
        destination: str,
        day: date,
    ) -> list[Offer]:
        url = RESULTS_URL.format(search_id=search_id)
        deadline = _monotonic() + POLL_TIMEOUT_S
        started = _monotonic()
        payload: dict[str, Any] = {}
        while True:
            resp = await self._request(ctx, "GET", url)
            if resp.status_code >= 400:
                raise SourceError(
                    f"letsfg: results poll failed HTTP {resp.status_code}"
                )
            try:
                payload = resp.json()
            except ValueError:
                payload = {}
            status = str(payload.get("status") or "").lower()
            if status in {"completed", "complete", "done", "expired"}:
                break
            if _monotonic() >= deadline:
                break
            elapsed = _monotonic() - started
            interval = (
                POLL_SLOW_INTERVAL_S
                if elapsed >= POLL_SLOW_AFTER_S
                else POLL_INTERVAL_S
            )
            remaining = deadline - _monotonic()
            await _sleep(min(interval, max(0.0, remaining)))
        return parse_results(
            payload,
            fx=ctx.fx,
            requested_origin=origin,
            requested_dest=destination,
            requested_date=day,
            search_id=search_id,
        )

    async def _wait_for_start_slot(self, started_at: list[float]) -> None:
        while True:
            now = _monotonic()
            wait = 0.0
            recent_10 = [t for t in started_at if now - t < WINDOW_10MIN_S]
            if len(recent_10) >= STARTS_PER_10MIN:
                wait = max(wait, WINDOW_10MIN_S - (now - min(recent_10)))
            recent_hr = [t for t in started_at if now - t < WINDOW_HOUR_S]
            if len(recent_hr) >= STARTS_PER_HOUR:
                wait = max(wait, WINDOW_HOUR_S - (now - min(recent_hr)))
            if wait <= 0:
                return
            await _sleep(wait + 0.01)

    def _slots_now(self, started_at: list[float]) -> int:
        now = _monotonic()
        used_10 = sum(1 for t in started_at if now - t < WINDOW_10MIN_S)
        used_hr = sum(1 for t in started_at if now - t < WINDOW_HOUR_S)
        return max(
            0,
            min(STARTS_PER_10MIN - used_10, STARTS_PER_HOUR - used_hr),
        )

    async def search(self, query: SearchQuery, ctx: RunContext) -> list[Offer]:
        self._refresh_token = _env(ctx.env, "LETSFG_REFRESH_TOKEN")
        self._client_id = _env(ctx.env, "LETSFG_CLIENT_ID")
        self._access_token = None
        if not self._refresh_token or not self._client_id:
            raise SourceError("letsfg: LETSFG_REFRESH_TOKEN and LETSFG_CLIENT_ID required")

        today = ctx.now.astimezone(timezone.utc).date()
        grid = cells_for(query, today)
        if not grid:
            return []

        cursor, searches_today = self._load_budget(ctx, today)
        remaining = min(self._max_searches(), max(0, DAILY_CAP - searches_today))
        if remaining <= 0:
            ctx.log.info("letsfg: daily search cap reached")
            return []

        try:
            await self._refresh_access(ctx)
        except RateLimited as exc:
            ctx.log.warning("letsfg: 429 on token refresh; stopping run")
            await _sleep(exc.retry_after_s or 30.0)
            return []

        run_started = _monotonic()
        timeout_s = self._timeout_s()
        started_at: list[float] = []
        offers: list[Offer] = []
        n = len(grid)

        while remaining > 0:
            if _monotonic() - run_started >= timeout_s:
                ctx.log.info("letsfg: run timeout, keeping cursor")
                break
            try:
                await self._wait_for_start_slot(started_at)
            except RateLimited:
                break
            slots = self._slots_now(started_at)
            batch_n = min(remaining, slots, STARTS_PER_10MIN)
            if batch_n <= 0:
                break

            batch: list[tuple[int, tuple[str, str, date], str]] = []
            stopped_early = False
            for _ in range(batch_n):
                if _monotonic() - run_started >= timeout_s:
                    stopped_early = True
                    break
                origin, dest, day = grid[cursor % n]
                try:
                    sid = await self._start_search(
                        ctx,
                        origin=origin,
                        destination=dest,
                        day=day,
                        adults=query.adults,
                        currency=query.currency or "USD",
                    )
                except RateLimited as exc:
                    ctx.log.warning("letsfg: 429 starting search; stopping run")
                    await _sleep(exc.retry_after_s or 30.0)
                    stopped_early = True
                    break
                started_at.append(_monotonic())
                batch.append((cursor % n, (origin, dest, day), sid))
                cursor = (cursor + 1) % n if n else cursor
                searches_today += 1
                remaining -= 1
                self._sync_state(ctx, cursor, today.isoformat(), searches_today)

            if batch:
                polled = await asyncio.gather(
                    *(
                        self._poll_results(
                            ctx,
                            sid,
                            origin=cell[0],
                            destination=cell[1],
                            day=cell[2],
                        )
                        for _, cell, sid in batch
                    ),
                    return_exceptions=True,
                )
                for item in polled:
                    if isinstance(item, RateLimited):
                        ctx.log.warning("letsfg: 429 while polling; stopping run")
                        await _sleep(item.retry_after_s or 30.0)
                        stopped_early = True
                        continue
                    if isinstance(item, SourceError):
                        raise item
                    if isinstance(item, Exception):
                        raise SourceError(f"letsfg: poll failed: {item}") from item
                    offers.extend(item)

            if stopped_early:
                break

        self._sync_state(ctx, cursor, today.isoformat(), searches_today)
        offers.sort(key=lambda o: (o.price_usd, o.depart_date.isoformat()))
        return offers


def results_url(search_id: str) -> str:
    return urljoin("https://letsfg.co/", f"api/results/{search_id}")
