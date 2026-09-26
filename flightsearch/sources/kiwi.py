from __future__ import annotations

import asyncio
from datetime import date, datetime, timedelta
from typing import Any, ClassVar

from flightsearch.context import RunContext
from flightsearch.mcp_client import McpSession
from flightsearch.models import Offer, SearchQuery
from flightsearch.sources.base import Source, SourceError

KIWI_MCP_URL = "https://mcp.kiwi.com"
_TOOL = "search-flight"
_MAX_PER_OD = 20
_CONCURRENCY = 4


def _fmt_date(d: date) -> str:
    return d.strftime("%d/%m/%Y")


def _parse_local_dt(value: str | None) -> datetime | None:
    if not value:
        return None
    text = str(value).strip()
    if text.endswith("Z"):
        text = text[:-1]
    if "+" in text[10:] or (text.count("-") > 2 and "T" in text):
        # strip trailing timezone offset if present
        for sep in ("+", "-"):
            idx = text.find(sep, 10)
            if idx != -1:
                text = text[:idx]
                break
    try:
        return datetime.fromisoformat(text)
    except ValueError:
        return None


def _ticket_bases(itinerary_id: str) -> set[str]:
    bases: set[str] = set()
    for part in (itinerary_id or "").split("|"):
        part = part.strip()
        if not part:
            continue
        bases.add(part.rsplit("_", 1)[0] if "_" in part else part)
    return bases


def _airport_changes(segments: list[dict[str, Any]]) -> list[tuple[str, str]]:
    changes: list[tuple[str, str]] = []
    for i in range(len(segments) - 1):
        arrive = str(segments[i].get("to") or "").upper()
        depart = str(segments[i + 1].get("from") or "").upper()
        if arrive and depart and arrive != depart:
            changes.append((arrive, depart))
    return changes


def _detect_self_transfer(
    itinerary_id: str, segments: list[dict[str, Any]]
) -> bool:
    if len(_ticket_bases(itinerary_id)) > 1:
        return True
    if _airport_changes(segments):
        return True
    return False


def _build_notes(
    *,
    destination: str,
    requested_to: str,
    self_transfer: bool,
    airport_changes: list[tuple[str, str]],
) -> str:
    notes: list[str] = []
    dest = destination.upper()
    req = requested_to.upper()
    if dest != req:
        notes.append(f"arrives {dest}")
    if self_transfer:
        notes.append("self-transfer")
    for a, b in airport_changes:
        notes.append(f"airport change at {a}→{b}")
    return "; ".join(notes)


def _bag_arrays(query: SearchQuery) -> tuple[list[int], list[int]]:
    hand = max(0, min(1, int(query.cabin_bags)))
    hold = max(0, min(2, int(query.checked_bags)))
    n = max(1, int(query.adults))
    return [hand] * n, [hold] * n


def _cabin_included(baggage: dict[str, Any] | None, query: SearchQuery) -> bool | None:
    if not isinstance(baggage, dict):
        return None
    cabin = int(baggage.get("cabinBag") or 0)
    needed = max(0, int(query.cabin_bags)) * max(1, int(query.adults))
    if needed <= 0:
        return True
    return cabin >= needed


def itinerary_to_offer(
    itinerary: dict[str, Any],
    *,
    currency: str,
    query: SearchQuery,
    fx: Any,
    requested_from: str,
    requested_to: str,
    source: str = "kiwi",
) -> Offer | None:
    outbound = itinerary.get("outbound")
    if not isinstance(outbound, dict):
        return None
    segments = list(outbound.get("segments") or [])
    if not segments:
        return None

    origin = str(segments[0].get("from") or outbound.get("from") or requested_from).upper()
    destination = str(
        segments[-1].get("to") or outbound.get("to") or requested_to
    ).upper()
    depart_time = _parse_local_dt(outbound.get("departureTime") or segments[0].get("departureTime"))
    arrive_time = _parse_local_dt(outbound.get("arrivalTime") or segments[-1].get("arrivalTime"))
    if depart_time is None:
        return None

    airlines: list[str] = []
    flight_numbers: list[str] = []
    for seg in segments:
        carrier = str(seg.get("carrier") or "").strip().upper()
        if carrier and carrier not in airlines:
            airlines.append(carrier)
        fn = str(seg.get("flightNumber") or "").strip().upper()
        if not fn and carrier:
            raw_num = seg.get("number")
            if raw_num is not None:
                fn = f"{carrier}{raw_num}".upper()
        if fn and fn not in flight_numbers:
            flight_numbers.append(fn)

    price_original = float(itinerary.get("price") or 0.0)
    cur = (currency or "USD").upper()
    price_usd = float(fx.to_usd(price_original, cur))

    stops = outbound.get("stops")
    if stops is None:
        stops = max(0, len(segments) - 1)

    duration_seconds = outbound.get("durationSeconds")
    if duration_seconds is None:
        duration_seconds = itinerary.get("totalDurationSeconds")
    duration_minutes = (
        int(duration_seconds) // 60 if duration_seconds is not None else None
    )

    airport_changes = _airport_changes(segments)
    self_transfer = _detect_self_transfer(str(itinerary.get("id") or ""), segments)
    notes = _build_notes(
        destination=destination,
        requested_to=requested_to,
        self_transfer=self_transfer,
        airport_changes=airport_changes,
    )

    return Offer(
        source=source,
        origin=origin,
        destination=destination,
        depart_date=depart_time.date(),
        price_usd=price_usd,
        price_original=price_original,
        currency_original=cur,
        depart_time=depart_time,
        arrive_time=arrive_time,
        airlines=airlines,
        flight_numbers=flight_numbers,
        stops=int(stops) if stops is not None else None,
        duration_minutes=duration_minutes,
        link=itinerary.get("bookingUrl"),
        cabin_bag_included=_cabin_included(itinerary.get("baggage"), query),
        self_transfer=self_transfer,
        notes=notes,
    )


def parse_search_response(
    data: Any,
    *,
    query: SearchQuery,
    fx: Any,
    requested_from: str,
    requested_to: str,
    source: str = "kiwi",
) -> list[Offer]:
    if not isinstance(data, dict):
        return []
    currency = str(data.get("currency") or query.currency or "USD")
    offers: list[Offer] = []
    for itinerary in data.get("itineraries") or []:
        if not isinstance(itinerary, dict):
            continue
        offer = itinerary_to_offer(
            itinerary,
            currency=currency,
            query=query,
            fx=fx,
            requested_from=requested_from,
            requested_to=requested_to,
            source=source,
        )
        if offer is not None:
            offers.append(offer)
    offers.sort(key=lambda o: (o.price_usd, o.depart_date.isoformat()))
    return offers[:_MAX_PER_OD]


def _search_args(
    *,
    fly_from: str,
    fly_to: str,
    date_from: date,
    date_to: date | None,
    query: SearchQuery,
    max_sector_stopovers: int | None = None,
) -> dict[str, Any]:
    hand, hold = _bag_arrays(query)
    args: dict[str, Any] = {
        "flyFrom": fly_from,
        "flyTo": fly_to,
        "departureDate": _fmt_date(date_from),
        "adults": int(query.adults),
        "adults_hand_bags": hand,
        "adults_hold_bags": hold,
        "currency": "USD",
        "sort": "price",
        "locale": "en",
    }
    if date_to is not None and date_to != date_from:
        args["departureDateTo"] = _fmt_date(date_to)
    if max_sector_stopovers is not None:
        args["max_sector_stopovers"] = max_sector_stopovers
    return args


class KiwiSource(Source):
    name: ClassVar[str] = "kiwi"

    async def search(self, query: SearchQuery, ctx: RunContext) -> list[Offer]:
        dates = query.dates(ctx.now.date())
        if not dates:
            return []

        date_from, date_to = dates[0], dates[-1]
        pairs = [
            (origin, dest)
            for origin in query.all_origins
            for dest in query.all_destinations
        ]
        if not pairs:
            return []

        sem = asyncio.Semaphore(_CONCURRENCY)
        offers: list[Offer] = []
        failures = 0
        lock = asyncio.Lock()

        async def one(session: McpSession, origin: str, dest: str) -> None:
            nonlocal failures
            args = _search_args(
                fly_from=origin,
                fly_to=dest,
                date_from=date_from,
                date_to=date_to,
                query=query,
            )
            async with sem:
                try:
                    data = await session.call_tool(_TOOL, args, retries=2)
                except Exception as exc:
                    ctx.log.warning(
                        "kiwi search failed %s→%s: %s", origin, dest, exc
                    )
                    async with lock:
                        failures += 1
                    return
            parsed = parse_search_response(
                data,
                query=query,
                fx=ctx.fx,
                requested_from=origin,
                requested_to=dest,
                source=self.name,
            )
            async with lock:
                offers.extend(parsed)

        async with McpSession(KIWI_MCP_URL, timeout=120) as session:
            await asyncio.gather(*(one(session, o, d) for o, d in pairs))

        if failures == len(pairs):
            raise SourceError(f"kiwi: all {failures} search calls failed")
        return _top_n_per_od(offers)


def _top_n_per_od(offers: list[Offer], n: int = _MAX_PER_OD) -> list[Offer]:
    buckets: dict[tuple[str, str], list[Offer]] = {}
    for offer in offers:
        key = (offer.origin, offer.destination)
        buckets.setdefault(key, []).append(offer)
    out: list[Offer] = []
    for group in buckets.values():
        group.sort(key=lambda o: (o.price_usd, o.depart_date.isoformat()))
        out.extend(group[:n])
    return out


async def fetch_hops(query: SearchQuery, ctx: RunContext) -> list[Offer]:
    """CNX → each positioning origin with local depart/arrive times.

    Empirically, a single date-range call returns only a handful of calendar
    days (API caps ~15 itineraries, biased to cheapest days). Per-date calls
    are required for hop coverage.
    """
    dates = query.dates(ctx.now.date())
    if not dates:
        return []

    hop_start = dates[0] - timedelta(days=1)
    hop_end = dates[-1]
    day = hop_start
    hop_dates: list[date] = []
    while day <= hop_end:
        hop_dates.append(day)
        day += timedelta(days=1)

    dests = list(query.positioning_origins)
    if not dests:
        return []

    tasks = [(d, dest) for dest in dests for d in hop_dates]
    sem = asyncio.Semaphore(_CONCURRENCY)
    offers: list[Offer] = []
    failures = 0
    lock = asyncio.Lock()

    async def one(session: McpSession, dep: date, dest: str) -> None:
        nonlocal failures
        args = _search_args(
            fly_from=query.home_origin,
            fly_to=dest,
            date_from=dep,
            date_to=None,
            query=query,
            max_sector_stopovers=0,
        )
        async with sem:
            try:
                data = await session.call_tool(_TOOL, args, retries=2)
            except Exception as exc:
                ctx.log.warning(
                    "kiwi hop failed %s→%s on %s: %s",
                    query.home_origin,
                    dest,
                    dep.isoformat(),
                    exc,
                )
                async with lock:
                    failures += 1
                return
        parsed = parse_search_response(
            data,
            query=query,
            fx=ctx.fx,
            requested_from=query.home_origin,
            requested_to=dest,
            source="kiwi",
        )
        # Prefer results that actually serve the requested airport/day.
        kept = [
            o
            for o in parsed
            if o.depart_date == dep
            and o.depart_time is not None
            and o.arrive_time is not None
        ]
        async with lock:
            offers.extend(kept)

    async with McpSession(KIWI_MCP_URL, timeout=120) as session:
        await asyncio.gather(*(one(session, d, dest) for d, dest in tasks))

    if tasks and failures == len(tasks):
        raise SourceError(f"kiwi: all {failures} hop calls failed")

    offers.sort(key=lambda o: (o.destination, o.depart_date.isoformat(), o.price_usd))
    return offers
