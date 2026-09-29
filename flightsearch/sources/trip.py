from __future__ import annotations

import asyncio
import json
import re
from datetime import date, datetime, time, timedelta
from typing import Any, ClassVar
from urllib.parse import urlencode

import httpx

from flightsearch.context import RunContext
from flightsearch.models import Offer, SearchQuery
from flightsearch.sources.base import Source, SourceError

TRIP_AIRLINE_URL = "https://tripgenie-openclaw-prod.trip.com/openclaw/airline"
_ENV_KEY = "TRIPGENIE_API_KEY"
_MAX_PER_OD = 20
_DEFAULT_BUDGET = 12
_DEFAULT_CONCURRENCY = 2
_DEFAULT_REQUEST_TIMEOUT_S = 75.0
# TripGenie airline `departure` / `arrival` are city codes. These airports
# do not share a code with their city.
_AIRPORT_CITY = {
    "DMK": "BKK",
    "WMI": "WAW",
}

_FLIGHT_SPLIT = re.compile(r"\*\*Flight No:\s*", re.I)
_RECOMMENDATION = re.compile(r"\*\*Recommendation\*\*", re.I)
_PRICE_RE = re.compile(
    r"Price:\s*(?:Total\s+)?([\d][\d,]*(?:\.\d+)?)\s*([A-Z]{3})",
    re.I,
)
_TIME_RE = re.compile(
    r"Time:\s*(\d{4}-\d{2}-\d{2})\s+(\d{1,2}:\d{2})\s*[-–]\s*"
    r"(?:(\d{4}-\d{2}-\d{2})\s+)?(\d{1,2}:\d{2})",
    re.I,
)
_DURATION_RE = re.compile(r"Duration\s+(\d+)\s+minutes", re.I)
_AIRPORT_LINE_RE = re.compile(r"Airport:\s*(.+)", re.I)
_AIRLINE_LINE_RE = re.compile(r"Airline:\s*(.+)", re.I)
_FLIGHT_NO_RE = re.compile(r"\b([A-Z]{1,3}\d{2,5})\b")
_PAREN_CODE_RE = re.compile(r"\(([A-Z]{3})\)")
_BARE_CODE_RE = re.compile(r"\b([A-Z]{3})\b")
_AIRLINE_CODE_RE = re.compile(r"\(([A-Z0-9]{2})\)")
_STOPS_RE = re.compile(r"(\d+)\s+stops?\b", re.I)
_NONSTOP_RE = re.compile(r"\b(?:non-?stop|direct)\b", re.I)
_LINK_RE = re.compile(r"https://(?:www\.)?trip\.com/\S+")
_AUTH_RE = re.compile(
    r"invalid token|token expired|unauthori[sz]ed|activation code|"
    r"do not yet have access|all beta spots",
    re.I,
)
_ERROR_RE = re.compile(r"\b(error|failed|exception|timeout)\b", re.I)
_CABIN_NO_RE = re.compile(
    r"(?:no|without)\s+(?:a\s+)?(?:cabin bag|carry-?on)|"
    r"(?:cabin bag|carry-?on).{0,40}(?:not included|excluded|not available)",
    re.I,
)
_CABIN_YES_RE = re.compile(
    r"(?:cabin bag|carry-?on).{0,40}included|"
    r"includes?\s+(?:a\s+)?(?:cabin bag|carry-?on)",
    re.I,
)
_SELF_TRANSFER_RE = re.compile(r"self-?transfer|separate tickets?", re.I)
_CODE_STOPWORDS = frozenset(
    {"THE", "AND", "FOR", "VIA", "AIR", "INT", "TOTAL", "FROM", "NOT"}
)


class _CallFailure(Exception):
    def __init__(self, message: str, *, auth: bool = False) -> None:
        super().__init__(message)
        self.auth = auth


def city_code(iata: str) -> str:
    code = iata.strip().upper()
    return _AIRPORT_CITY.get(code, code)


def coerce_markdown(payload: Any) -> str:
    """Unwrap a TripGenie body that may be markdown, a JSON string, or an object."""
    if isinstance(payload, str):
        text = payload.strip()
        if text[:1] in {'"', "{", "["}:
            try:
                decoded = json.loads(text)
            except json.JSONDecodeError:
                return payload
            if decoded is payload or decoded == text:
                return payload
            return coerce_markdown(decoded)
        return payload
    if isinstance(payload, dict):
        for key in (
            "text",
            "markdown",
            "content",
            "result",
            "data",
            "message",
            "answer",
            "output",
        ):
            value = payload.get(key)
            if isinstance(value, (str, dict, list)) and value:
                return coerce_markdown(value)
        return json.dumps(payload)
    if isinstance(payload, list):
        return "\n".join(coerce_markdown(item) for item in payload)
    if payload is None:
        return ""
    return str(payload)


def classify_markdown(markdown: str) -> str:
    """Return ``ok``, ``empty``, ``auth``, or ``error``."""
    if _FLIGHT_SPLIT.search(markdown or ""):
        return "ok"
    if _AUTH_RE.search(markdown or ""):
        return "auth"
    if _ERROR_RE.search(markdown or "") and len(markdown or "") < 2000:
        return "error"
    return "empty"


def _parse_clock(value: str) -> time:
    hour, minute = value.split(":", 1)
    return time(hour=int(hour), minute=int(minute))


def _airport_codes(line: str) -> list[str]:
    paren = _PAREN_CODE_RE.findall(line)
    if len(paren) >= 2:
        return paren
    bare = [c for c in _BARE_CODE_RE.findall(line) if c not in _CODE_STOPWORDS]
    return bare


def _flight_numbers(header: str) -> list[str]:
    seen: list[str] = []
    for match in _FLIGHT_NO_RE.findall(header.upper()):
        if match not in seen:
            seen.append(match)
    return seen


def _airline_codes(line: str, flight_numbers: list[str]) -> list[str]:
    codes: list[str] = []
    for match in _AIRLINE_CODE_RE.findall(line.upper()):
        if match not in codes and not match.isdigit():
            codes.append(match)
    if codes:
        return codes
    for number in flight_numbers:
        prefix = re.match(r"[A-Z]{1,3}", number)
        if prefix and prefix.group(0) not in codes:
            codes.append(prefix.group(0))
    return codes


def _stops(block: str, flight_numbers: list[str], airports: list[str]) -> int | None:
    match = _STOPS_RE.search(block)
    if match:
        return int(match.group(1))
    if _NONSTOP_RE.search(block):
        return 0
    if len(airports) > 2:
        return len(airports) - 2
    if len(flight_numbers) > 1:
        return len(flight_numbers) - 1
    if len(airports) >= 2 or len(flight_numbers) == 1:
        return 0
    return None


def _cabin_bag(block: str) -> bool | None:
    if _CABIN_NO_RE.search(block):
        return False
    if _CABIN_YES_RE.search(block):
        return True
    return None


def _block_link(
    block: str,
    *,
    origin: str,
    destination: str,
    depart: date,
    adults: int,
) -> str:
    match = _LINK_RE.search(block)
    if match:
        url = match.group(0).rstrip(").,]>\"'")
        path = url.split("trip.com", 1)[-1].split("?", 1)[0].rstrip("/")
        if path not in {"", "/flights"}:
            return url
    query = urlencode(
        {
            "dcity": origin.lower(),
            "acity": destination.lower(),
            "ddate": depart.isoformat(),
            "triptype": "ow",
            "class": "y",
            "quantity": max(1, int(adults)),
            "locale": "en-US",
            "curr": "USD",
        }
    )
    return f"https://www.trip.com/flights/showfarefirst?{query}"


def _iter_blocks(markdown: str) -> list[tuple[str, str]]:
    blocks: list[tuple[str, str]] = []
    for part in _FLIGHT_SPLIT.split(markdown)[1:]:
        part = _RECOMMENDATION.split(part, maxsplit=1)[0]
        header, sep, rest = part.partition("**")
        if not sep:
            header, _, rest = part.partition("\n")
        blocks.append((header.strip(), rest))
    return blocks


def _offer_from_block(
    header: str,
    body: str,
    *,
    fx: Any,
    adults: int,
) -> Offer | None:
    block = f"{header}\n{body}"
    numbers = _flight_numbers(header)
    price_match = _PRICE_RE.search(body) or _PRICE_RE.search(block)
    time_match = _TIME_RE.search(body) or _TIME_RE.search(block)
    airport_match = _AIRPORT_LINE_RE.search(body) or _AIRPORT_LINE_RE.search(block)
    if price_match is None or time_match is None or airport_match is None:
        return None
    airports = _airport_codes(airport_match.group(1))
    if len(airports) < 2 or not numbers:
        return None
    try:
        amount = float(price_match.group(1).replace(",", ""))
    except ValueError:
        return None
    if amount <= 0:
        return None
    currency = price_match.group(2).upper()
    try:
        depart_day = date.fromisoformat(time_match.group(1))
        depart_clock = _parse_clock(time_match.group(2))
        arrive_day = (
            date.fromisoformat(time_match.group(3))
            if time_match.group(3)
            else depart_day
        )
        arrive_clock = _parse_clock(time_match.group(4))
    except ValueError:
        return None
    depart_time = datetime.combine(depart_day, depart_clock)
    arrive_time = datetime.combine(arrive_day, arrive_clock)
    if time_match.group(3) is None and arrive_time <= depart_time:
        arrive_time += timedelta(days=1)

    duration_match = _DURATION_RE.search(block)
    duration = int(duration_match.group(1)) if duration_match else None
    airline_match = _AIRLINE_LINE_RE.search(body)
    airline_line = airline_match.group(1) if airline_match else ""
    airlines = _airline_codes(airline_line, numbers)
    origin, destination = airports[0], airports[-1]
    self_transfer = True if _SELF_TRANSFER_RE.search(block) else None
    notes = "self-transfer" if self_transfer else ""

    return Offer(
        source="trip",
        origin=origin,
        destination=destination,
        depart_date=depart_time.date(),
        price_usd=float(fx.to_usd(amount, currency)),
        price_original=amount,
        currency_original=currency,
        depart_time=depart_time,
        arrive_time=arrive_time,
        airlines=airlines,
        flight_numbers=numbers,
        stops=_stops(block, numbers, airports),
        duration_minutes=duration,
        link=_block_link(
            block,
            origin=origin,
            destination=destination,
            depart=depart_time.date(),
            adults=adults,
        ),
        cabin_bag_included=_cabin_bag(block),
        self_transfer=self_transfer,
        notes=notes,
    )


def parse_airline_markdown(
    markdown: str,
    *,
    fx: Any,
    adults: int,
    requested_from: str,
    requested_to: str,
    window_from: date,
    window_to: date,
) -> list[Offer]:
    """Parse TripGenie airline markdown into offers for one requested airport pair."""
    wanted_from = requested_from.upper()
    wanted_to = requested_to.upper()
    offers: list[Offer] = []
    for header, body in _iter_blocks(markdown):
        offer = _offer_from_block(header, body, fx=fx, adults=adults)
        if offer is None:
            continue
        if offer.origin != wanted_from or offer.destination != wanted_to:
            continue
        if offer.depart_date < window_from or offer.depart_date > window_to:
            continue
        offers.append(offer)
    offers.sort(key=lambda o: (o.price_usd, o.depart_time or datetime.min))
    return offers


def _cap_per_od(offers: list[Offer], n: int = _MAX_PER_OD) -> list[Offer]:
    buckets: dict[tuple[str, str], list[Offer]] = {}
    for offer in offers:
        buckets.setdefault((offer.origin, offer.destination), []).append(offer)
    out: list[Offer] = []
    for group in buckets.values():
        group.sort(key=lambda o: (o.price_usd, o.depart_date.isoformat()))
        out.extend(group[:n])
    return out


def _tasks(query: SearchQuery, dates: list[date]) -> list[tuple[str, str, date]]:
    tasks: list[tuple[str, str, date]] = []
    for origin in query.all_origins:
        for dest in query.all_destinations:
            for day in dates:
                tasks.append((origin.upper(), dest.upper(), day))
    return tasks


def _snippet(text: str, token: str) -> str:
    clean = text.replace(token, "<token>") if token else text
    clean = " ".join(clean.split())
    return clean[:180]


class TripSource(Source):
    """Trip.com flights via the TripGenie OpenClaw airline API.

    Each call is one city pair and one departure date. ``max_searches_per_run``
    walks a cursor across home and positioning routes so later runs cover the
    rest of the window. The activation code is ``TRIPGENIE_API_KEY``.
    """

    name: ClassVar[str] = "trip"

    def is_available(self, env: Any) -> tuple[bool, str]:
        if not (env.get(_ENV_KEY) or "").strip():
            return False, f"{_ENV_KEY} not set"
        return True, ""

    def _budget(self) -> int:
        try:
            return max(0, int(self.settings.get("max_searches_per_run", _DEFAULT_BUDGET)))
        except (TypeError, ValueError):
            return _DEFAULT_BUDGET

    def _concurrency(self) -> int:
        try:
            return max(1, int(self.settings.get("concurrency", _DEFAULT_CONCURRENCY)))
        except (TypeError, ValueError):
            return _DEFAULT_CONCURRENCY

    def _request_timeout(self) -> float:
        try:
            return float(
                self.settings.get("request_timeout_s", _DEFAULT_REQUEST_TIMEOUT_S)
            )
        except (TypeError, ValueError):
            return _DEFAULT_REQUEST_TIMEOUT_S

    async def search(self, query: SearchQuery, ctx: RunContext) -> list[Offer]:
        token = (ctx.env.get(_ENV_KEY) or "").strip()
        if not token:
            raise SourceError(f"{_ENV_KEY} not set")

        dates = query.dates(ctx.now.date())
        tasks = _tasks(query, dates)
        budget = self._budget()
        if not tasks or budget <= 0:
            return []

        start = int(ctx.state.get("cursor") or 0) % len(tasks)
        count = min(budget, len(tasks))
        chosen = [tasks[(start + i) % len(tasks)] for i in range(count)]
        ctx.state["cursor"] = (start + count) % len(tasks)

        sem = asyncio.Semaphore(self._concurrency())
        offers: list[Offer] = []
        failures = 0
        last_error = ""
        lock = asyncio.Lock()

        async def one(origin: str, dest: str, day: date) -> None:
            nonlocal failures, last_error
            async with sem:
                try:
                    markdown = await self._fetch(
                        ctx, token=token, origin=origin, dest=dest, day=day, query=query
                    )
                except _CallFailure as exc:
                    if exc.auth:
                        raise
                    ctx.log.warning(
                        "trip search failed %s→%s on %s: %s",
                        origin,
                        dest,
                        day.isoformat(),
                        exc,
                    )
                    async with lock:
                        failures += 1
                        last_error = str(exc)
                    return
            parsed = parse_airline_markdown(
                markdown,
                fx=ctx.fx,
                adults=query.adults,
                requested_from=origin,
                requested_to=dest,
                window_from=dates[0],
                window_to=dates[-1],
            )
            async with lock:
                offers.extend(parsed)

        try:
            await asyncio.gather(*(one(o, d, day) for o, d, day in chosen))
        except _CallFailure as exc:
            raise SourceError(str(exc)) from exc

        if failures == len(chosen) and not offers:
            detail = f": {last_error}" if last_error else ""
            raise SourceError(f"trip: all {failures} airline searches failed{detail}")
        return _cap_per_od(offers)

    async def _fetch(
        self,
        ctx: RunContext,
        *,
        token: str,
        origin: str,
        dest: str,
        day: date,
        query: SearchQuery,
    ) -> str:
        bags: list[str] = []
        bags.append(
            f"{query.cabin_bags} cabin bag" if query.cabin_bags else "no cabin bag"
        )
        bags.append(
            f"{query.checked_bags} checked bag"
            if query.checked_bags
            else "no checked bag"
        )
        payload = {
            "token": token,
            "departure": city_code(origin),
            "arrival": city_code(dest),
            "date": day.isoformat(),
            "flight_type": "0",
            "locale": "en-US",
            "query": (
                f"One-way flight from {origin} to {dest} on {day.isoformat()} "
                f"for {query.adults} adult(s), {', '.join(bags)}. "
                "List the cheapest options with flight number, total price and "
                "currency, local departure and arrival times, duration in minutes, "
                "airports with IATA codes, and airline IATA codes."
            ),
        }
        try:
            response = await ctx.http.post(
                TRIP_AIRLINE_URL,
                json=payload,
                headers={
                    "Accept": "application/json, text/markdown, text/plain, */*",
                    "User-Agent": "flightsearch/tripgenie",
                },
                timeout=httpx.Timeout(self._request_timeout()),
            )
        except httpx.HTTPError as exc:
            raise _CallFailure(f"trip: request failed: {exc}") from exc

        text = response.text or ""
        if response.status_code in {401, 403}:
            raise _CallFailure("trip: activation code rejected", auth=True)
        if response.status_code >= 400:
            raise _CallFailure(
                f"trip: airline HTTP {response.status_code}: {_snippet(text, token)}"
            )

        markdown = coerce_markdown(text)
        kind = classify_markdown(markdown)
        if kind == "auth":
            raise _CallFailure("trip: activation code rejected", auth=True)
        if kind == "error":
            raise _CallFailure(
                f"trip: airline response error: {_snippet(markdown, token)}"
            )
        return markdown
