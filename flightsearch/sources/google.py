from __future__ import annotations

import asyncio
import random
from dataclasses import dataclass
from datetime import date, datetime
from typing import Any, ClassVar
from urllib.parse import quote_plus

from flightsearch.context import FxConverter, RunContext
from flightsearch.models import Offer, SearchQuery
from flightsearch.sources.base import Source, SourceError

_MAX_PER_OD = 20
_DEFAULT_DETAIL = 10
_DEFAULT_CONCURRENCY = 2
_CURRENCY = "USD"
_LANGUAGE = "en"
_COUNTRY = "US"
_EMPTY_MSG = "google flights returned empty payloads (blocked or API change)"
_BAGS_NOTE = "bags not verified (Google)"
_STATE_PARITY = "date_parity"
_JITTER_S = (0.3, 0.8)


class EmptyGooglePayload(Exception):
    """Google answered HTTP 200 with no shopping/calendar payload."""


@dataclass(frozen=True)
class _DateCell:
    origin: str
    destination: str
    depart_date: date
    price: float
    currency: str


def _iata(value: Any) -> str:
    name = getattr(value, "name", None)
    return (name or str(value)).removeprefix("_")


def _join_notes(*parts: str) -> str:
    return "; ".join(part for part in parts if part)


def _as_bool(value: Any) -> bool | None:
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return value > 0
    if isinstance(value, str):
        lowered = value.strip().lower()
        if lowered in {"1", "true", "yes", "included"}:
            return True
        if lowered in {"0", "false", "no", "excluded"}:
            return False
    return None


def _explicit_cabin_bag(result: Any) -> bool | None:
    """Return carry-on inclusion only when the result itself states it."""
    if result is None:
        return None
    for key in (
        "cabin_bag_included",
        "carry_on_included",
        "hand_bag_included",
        "carry_on",
        "cabin_bag",
    ):
        found = _as_bool(getattr(result, key, None))
        if found is not None:
            return found
    bags = getattr(result, "bags", None) or getattr(result, "baggage", None)
    if bags is None:
        return None
    if isinstance(bags, dict):
        items = bags
    else:
        items = {
            key: getattr(bags, key, None)
            for key in (
                "cabin_bag_included",
                "carry_on",
                "carry_on_included",
                "cabin",
                "hand",
                "cabin_bag",
                "hand_bag",
            )
        }
    for key in (
        "cabin_bag_included",
        "carry_on_included",
        "carry_on",
        "cabin",
        "hand",
        "cabin_bag",
        "hand_bag",
    ):
        found = _as_bool(items.get(key) if isinstance(items, dict) else None)
        if found is not None:
            return found
    return None


def plan_date_jobs(
    query: SearchQuery, dates: list[date], parity: int
) -> list[tuple[str, str, date]]:
    """Home origin: every dest × every date. Positioning: main dests, every other date."""
    jobs: list[tuple[str, str, date]] = []
    for dest in query.all_destinations:
        for day in dates:
            jobs.append((query.home_origin, dest, day))
    pos_dates = [day for i, day in enumerate(dates) if i % 2 == parity]
    for origin in query.positioning_origins:
        for dest in query.destinations:
            for day in pos_dates:
                jobs.append((origin, dest, day))
    return jobs


async def _jitter(lo: float, hi: float) -> None:
    if hi <= 0:
        return
    await asyncio.sleep(random.uniform(lo, max(lo, hi)))


def _is_empty_payload_error(exc: BaseException) -> bool:
    if type(exc).__name__ in {"SearchRejectedError", "SearchParseError"}:
        return True
    text = str(exc).lower()
    return any(
        marker in text
        for marker in (
            "error 13",
            "wrb.fr",
            "no ds:1",
            "declined the request",
            "empty payload",
            "every date in the range failed",
            "no date in the range could be priced",
        )
    )


def _airport(code: str) -> Any:
    from fli.models import Airport

    return Airport[code.upper()]


def google_flights_url(
    origin: str,
    destination: str,
    depart_date: date,
    *,
    currency: str = _CURRENCY,
) -> str:
    """Public Google Flights URL for a one-way route/date."""
    try:
        from fli.core.builders import build_flight_segments
        from fli.models import FlightSearchFilters, MaxStops, PassengerInfo
        from fli.search._tfs import build_tfs, page_url

        segs, trip_type = build_flight_segments(
            _airport(origin), _airport(destination), depart_date.isoformat()
        )
        filters = FlightSearchFilters(
            trip_type=trip_type,
            passenger_info=PassengerInfo(adults=1),
            flight_segments=segs,
            stops=MaxStops.ANY,
        )
        return page_url(
            build_tfs(filters),
            currency=currency,
            language=_LANGUAGE,
            country=_COUNTRY,
        )
    except Exception:
        q = quote_plus(f"One way {origin} to {destination} on {depart_date.isoformat()}")
        return (
            f"https://www.google.com/travel/flights?hl={_LANGUAGE}"
            f"&gl={_COUNTRY}&curr={currency}&q={q}"
        )


def _date_filters(
    origin: str,
    destination: str,
    date_from: date,
    date_to: date,
    query: SearchQuery,
) -> Any:
    from fli.core.builders import build_date_search_segments
    from fli.models import DateSearchFilters, MaxStops, PassengerInfo

    segs, trip_type = build_date_search_segments(
        _airport(origin), _airport(destination), date_from.isoformat()
    )
    return DateSearchFilters(
        trip_type=trip_type,
        passenger_info=PassengerInfo(adults=int(query.adults)),
        flight_segments=segs,
        stops=MaxStops.ANY,
        from_date=date_from.isoformat(),
        to_date=date_to.isoformat(),
    )


def _flight_filters(origin: str, destination: str, depart: date, query: SearchQuery) -> Any:
    from fli.core.builders import build_flight_segments
    from fli.models import FlightSearchFilters, MaxStops, PassengerInfo, SortBy

    segs, trip_type = build_flight_segments(
        _airport(origin), _airport(destination), depart.isoformat()
    )
    return FlightSearchFilters(
        trip_type=trip_type,
        passenger_info=PassengerInfo(adults=int(query.adults)),
        flight_segments=segs,
        stops=MaxStops.ANY,
        sort_by=SortBy.CHEAPEST,
    )


def _cell_date(row: Any) -> date:
    raw = row.date[0] if isinstance(row.date, tuple) else row.date
    if isinstance(raw, datetime):
        return raw.date()
    if isinstance(raw, date):
        return raw
    return date.fromisoformat(str(raw)[:10])


def search_date_prices(
    origin: str,
    destination: str,
    date_from: date,
    date_to: date,
    query: SearchQuery,
) -> list[_DateCell]:
    """Sync date-range search. Fresh client per call (not thread-safe)."""
    from fli.search import SearchDates

    filters = _date_filters(origin, destination, date_from, date_to, query)
    try:
        rows = SearchDates().search(
            filters, currency=_CURRENCY, language=_LANGUAGE, country=_COUNTRY
        )
    except Exception as exc:
        if _is_empty_payload_error(exc):
            raise EmptyGooglePayload(str(exc)) from exc
        raise
    if not rows:
        return []
    cells: list[_DateCell] = []
    for row in rows:
        price = getattr(row, "price", None)
        if price is None:
            continue
        day = _cell_date(row)
        if day < date_from or day > date_to:
            continue
        cells.append(
            _DateCell(
                origin=origin,
                destination=destination,
                depart_date=day,
                price=float(price),
                currency=str(getattr(row, "currency", None) or _CURRENCY),
            )
        )
    return cells


def search_flight_results(
    origin: str,
    destination: str,
    depart: date,
    query: SearchQuery,
    *,
    top_n: int = 5,
) -> list[Any]:
    """Sync itinerary search. Fresh client per call (not thread-safe)."""
    from fli.search import SearchFlights

    filters = _flight_filters(origin, destination, depart, query)
    try:
        results = SearchFlights().search(
            filters,
            top_n=top_n,
            currency=_CURRENCY,
            language=_LANGUAGE,
            country=_COUNTRY,
        )
    except Exception as exc:
        if _is_empty_payload_error(exc):
            raise EmptyGooglePayload(str(exc)) from exc
        raise
    if not results:
        return []
    out: list[Any] = []
    for row in results:
        if isinstance(row, tuple):
            row = row[0]
        out.append(row)
    return out


def _flight_number(leg: Any) -> str:
    airline = _iata(getattr(leg, "airline", ""))
    number = str(getattr(leg, "flight_number", "") or "").strip()
    if not number:
        return airline
    if airline and number.upper().startswith(airline.upper()):
        return number
    return f"{airline}{number}" if airline else number


def flight_to_offer(
    result: Any,
    *,
    origin: str,
    destination: str,
    depart_date: date,
    query: SearchQuery,
    fx: FxConverter,
    link: str | None,
) -> Offer | None:
    price = getattr(result, "price", None)
    if price is None:
        return None
    currency = str(getattr(result, "currency", None) or _CURRENCY)
    legs = list(getattr(result, "legs", None) or [])
    first = legs[0] if legs else None
    last = legs[-1] if legs else None
    depart_time = getattr(first, "departure_datetime", None) if first else None
    arrive_time = getattr(last, "arrival_datetime", None) if last else None
    if isinstance(depart_time, datetime):
        depart_time = depart_time.replace(tzinfo=None)
    if isinstance(arrive_time, datetime):
        arrive_time = arrive_time.replace(tzinfo=None)
    airlines: list[str] = []
    for leg in legs:
        code = _iata(getattr(leg, "airline", ""))
        if code and code not in airlines:
            airlines.append(code)
    notes = "mixed cabin" if getattr(result, "mixed_cabin", None) else ""
    cabin_bag = _explicit_cabin_bag(result)
    if cabin_bag is None:
        notes = _join_notes(notes, _BAGS_NOTE)
    return Offer(
        source="google",
        origin=origin,
        destination=destination,
        depart_date=depart_date,
        price_usd=float(fx.to_usd(float(price), currency)),
        price_original=float(price),
        currency_original=currency,
        depart_time=depart_time,
        arrive_time=arrive_time,
        airlines=airlines,
        flight_numbers=[_flight_number(leg) for leg in legs],
        stops=getattr(result, "stops", None),
        duration_minutes=getattr(result, "duration", None),
        link=link,
        cabin_bag_included=cabin_bag,
        self_transfer=getattr(result, "self_transfer", None),
        notes=notes,
    )


def calendar_offer(
    cell: _DateCell,
    *,
    query: SearchQuery,
    fx: FxConverter,
    link: str | None,
) -> Offer:
    return Offer(
        source="google",
        origin=cell.origin,
        destination=cell.destination,
        depart_date=cell.depart_date,
        price_usd=float(fx.to_usd(cell.price, cell.currency)),
        price_original=cell.price,
        currency_original=cell.currency,
        depart_time=None,
        arrive_time=None,
        link=link,
        cabin_bag_included=None,
        notes=_join_notes("calendar fare", _BAGS_NOTE),
    )


def _cap_per_od(offers: list[Offer]) -> list[Offer]:
    grouped: dict[tuple[str, str], list[Offer]] = {}
    for offer in offers:
        grouped.setdefault((offer.origin, offer.destination), []).append(offer)
    out: list[Offer] = []
    for group in grouped.values():
        group.sort(
            key=lambda o: (
                o.price_usd,
                o.depart_date.isoformat(),
                o.depart_time.isoformat() if o.depart_time else "",
            )
        )
        out.extend(group[:_MAX_PER_OD])
    return out


class GoogleFlightsSource(Source):
    name: ClassVar[str] = "google"

    async def search(self, query: SearchQuery, ctx: RunContext) -> list[Offer]:
        dates = query.dates(ctx.now.date())
        if not dates:
            return []

        parity = int(ctx.state.get(_STATE_PARITY) or 0) % 2
        jobs = plan_date_jobs(query, dates, parity)
        ctx.state[_STATE_PARITY] = 1 - parity
        if not jobs:
            return []

        max_detail = int(self.settings.get("max_detail_searches", _DEFAULT_DETAIL))
        concurrency = max(1, int(self.settings.get("concurrency", _DEFAULT_CONCURRENCY)))
        jitter = self.settings.get("jitter_s", _JITTER_S)
        jitter_lo, jitter_hi = float(jitter[0]), float(jitter[1])
        sem = asyncio.Semaphore(concurrency)

        cells: list[_DateCell] = []
        empty_payloads = 0
        date_failures = 0
        lock = asyncio.Lock()

        async def one_dates(origin: str, dest: str, day: date) -> None:
            nonlocal empty_payloads, date_failures
            async with sem:
                await _jitter(jitter_lo, jitter_hi)
                try:
                    found = await asyncio.to_thread(
                        search_date_prices, origin, dest, day, day, query
                    )
                except EmptyGooglePayload as exc:
                    ctx.log.warning(
                        "google date search empty payload %s→%s %s: %s",
                        origin,
                        dest,
                        day,
                        exc,
                    )
                    async with lock:
                        empty_payloads += 1
                    return
                except Exception as exc:
                    ctx.log.warning(
                        "google date search failed %s→%s %s: %s",
                        origin,
                        dest,
                        day,
                        exc,
                    )
                    async with lock:
                        date_failures += 1
                    return
            async with lock:
                cells.extend(found)

        await asyncio.gather(*(one_dates(o, d, day) for o, d, day in jobs))

        if not cells:
            if empty_payloads or date_failures == 0:
                raise SourceError(_EMPTY_MSG)
            raise SourceError(f"google flights: all {len(jobs)} date searches failed")

        ranked: list[tuple[float, _DateCell]] = []
        for cell in cells:
            ranked.append((float(ctx.fx.to_usd(cell.price, cell.currency)), cell))
        ranked.sort(
            key=lambda item: (
                item[0],
                item[1].depart_date.isoformat(),
                item[1].origin,
                item[1].destination,
            )
        )
        eligible = [cell for usd, cell in ranked if usd <= query.near_miss_usd]
        to_detail = eligible[:max_detail]
        detailed_keys = {
            (cell.origin, cell.destination, cell.depart_date) for cell in to_detail
        }

        detail_offers: list[Offer] = []
        detail_empty = 0

        async def one_detail(cell: _DateCell) -> None:
            nonlocal detail_empty
            link = google_flights_url(cell.origin, cell.destination, cell.depart_date)
            async with sem:
                await _jitter(jitter_lo, jitter_hi)
                try:
                    results = await asyncio.to_thread(
                        search_flight_results,
                        cell.origin,
                        cell.destination,
                        cell.depart_date,
                        query,
                    )
                except EmptyGooglePayload as exc:
                    ctx.log.warning(
                        "google detail empty payload %s→%s %s: %s",
                        cell.origin,
                        cell.destination,
                        cell.depart_date,
                        exc,
                    )
                    async with lock:
                        detail_empty += 1
                    return
                except Exception as exc:
                    ctx.log.warning(
                        "google detail failed %s→%s %s: %s",
                        cell.origin,
                        cell.destination,
                        cell.depart_date,
                        exc,
                    )
                    return
            parsed = [
                offer
                for result in results
                if (
                    offer := flight_to_offer(
                        result,
                        origin=cell.origin,
                        destination=cell.destination,
                        depart_date=cell.depart_date,
                        query=query,
                        fx=ctx.fx,
                        link=link,
                    )
                )
                is not None
            ]
            parsed.sort(key=lambda o: (o.price_usd, o.depart_time or datetime.min))
            async with lock:
                detail_offers.extend(parsed[:_MAX_PER_OD])

        if to_detail:
            await asyncio.gather(*(one_detail(cell) for cell in to_detail))

        calendar_offers: list[Offer] = []
        for _usd, cell in ranked:
            key = (cell.origin, cell.destination, cell.depart_date)
            if key in detailed_keys:
                continue
            calendar_offers.append(
                calendar_offer(
                    cell,
                    query=query,
                    fx=ctx.fx,
                    link=google_flights_url(cell.origin, cell.destination, cell.depart_date),
                )
            )

        if (
            to_detail
            and not detail_offers
            and not calendar_offers
            and detail_empty == len(to_detail)
        ):
            raise SourceError(_EMPTY_MSG)

        return _cap_per_od(detail_offers + calendar_offers)
