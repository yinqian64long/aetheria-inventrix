from __future__ import annotations

import re
from datetime import date
from typing import Any, ClassVar

from flightsearch.bags import cabin_bag_included, checked_bag_included
from flightsearch.context import RunContext
from flightsearch.models import Offer
from flightsearch.sources.apify_routes import (
    DatedRouteSource,
    clock_on_date,
    depart_date_of,
    duration_minutes,
    iata,
    price_and_currency,
)

ACTOR_ID = "maximedupre~flight-scraper"
_VJ_RE = re.compile(r"\bvj\b|viet\s*jet", re.IGNORECASE)


def vietjet_link(origin: str, destination: str, depart: date, adults: int) -> str:
    return (
        "https://www.vietjetair.com/en/flight-search"
        f"?origin={origin}&destination={destination}"
        f"&departureDate={depart.isoformat()}&adults={max(1, int(adults))}&currency=USD"
    )


class VietjetSource(DatedRouteSource):
    """VietJet one-way fares via Apify ``maximedupre/flight-scraper``.

    The actor has no VietJet-only mode. The request prefers airline VJ and
    any itinerary that is not entirely VietJet is dropped.
    """

    name: ClassVar[str] = "vietjet"
    actor_id: ClassVar[str] = ACTOR_ID
    log_name: ClassVar[str] = "vietjet"

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
        return {
            "tripType": "oneWay",
            "origin": origin,
            "destination": destination,
            "departureDate": depart.isoformat(),
            "passengers": adults,
            "currency": currency,
            "cabinClass": "economy",
            "maxStops": "any",
            "preferredAirlines": ["VJ", "VietJet Air"],
            "sortBy": "cheapest",
            "includeBaggageEstimate": True,
            "maxResults": max_results,
        }

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
        if not isinstance(item, dict):
            return None
        if item.get("type") not in {None, "", "flight_itinerary"}:
            return None
        segments = [
            seg for seg in (item.get("segments") or []) if isinstance(seg, dict)
        ]
        if not _is_vietjet(item, segments):
            return None
        priced = price_and_currency(item)
        if priced is None:
            return None
        price_original, currency = priced
        try:
            price_usd = ctx.fx.to_usd(price_original, currency)
        except Exception as exc:
            ctx.log.warning(
                "vietjet: fx failed for %s %s: %s", price_original, currency, exc
            )
            return None

        origin = iata(item.get("from") or item.get("origin"), fallback_origin)
        destination = iata(item.get("to") or item.get("destination"), fallback_dest)
        depart_date = depart_date_of(item, searched_date)
        if depart_date < window_from or depart_date > window_to:
            return None

        depart_time = clock_on_date(
            depart_date, item.get("departTime") or item.get("departureTime")
        )
        arrive_day = depart_date
        raw_arrive_date = item.get("arriveDate")
        if raw_arrive_date:
            try:
                arrive_day = date.fromisoformat(str(raw_arrive_date)[:10])
            except ValueError:
                arrive_day = depart_date
        arrive_time = clock_on_date(
            arrive_day, item.get("arriveTime") or item.get("arrivalTime")
        )
        airlines, flight_numbers = _airlines(item, segments)
        stops = item.get("stops")
        try:
            stops = int(stops) if stops is not None and stops != "" else None
        except (TypeError, ValueError):
            stops = None
        minutes = item.get("durationMinutes")
        if not isinstance(minutes, int):
            minutes = duration_minutes(item.get("duration"))
        baggage = item.get("baggage")
        self_transfer = item.get("isSelfTransfer")
        link = _link(
            item,
            origin=origin,
            destination=destination,
            depart=depart_date,
            adults=adults,
        )
        return Offer(
            source=self.name,
            origin=origin,
            destination=destination,
            depart_date=depart_date,
            price_usd=round(float(price_usd), 2),
            price_original=price_original,
            currency_original=currency,
            depart_time=depart_time,
            arrive_time=arrive_time,
            airlines=airlines,
            flight_numbers=flight_numbers,
            stops=stops,
            duration_minutes=minutes,
            link=link,
            cabin_bag_included=cabin_bag_included(baggage) if baggage is not None else None,
            checked_bag_included=checked_bag_included(baggage) if baggage is not None else None,
            self_transfer=bool(self_transfer) if self_transfer is not None else None,
        )


def _is_vietjet(item: dict[str, Any], segments: list[dict[str, Any]]) -> bool:
    if segments:
        return all(_segment_is_vietjet(seg) for seg in segments)
    return _text_is_vietjet(item.get("airline"))


def _segment_is_vietjet(segment: dict[str, Any]) -> bool:
    return any(
        _text_is_vietjet(segment.get(key))
        for key in ("airlineCode", "airline", "carrier", "flightNumber", "flightNo")
    )


def _text_is_vietjet(value: Any) -> bool:
    if not value:
        return False
    return _VJ_RE.search(str(value)) is not None


def _airlines(
    item: dict[str, Any], segments: list[dict[str, Any]]
) -> tuple[list[str], list[str]]:
    airlines: list[str] = []
    numbers: list[str] = []
    raw = item.get("airline")
    if raw:
        name = str(raw).strip()
        if name:
            airlines.append(name)
    for seg in segments:
        airline = seg.get("airline") or seg.get("airlineCode")
        if airline:
            name = str(airline).strip()
            if name and name not in airlines:
                airlines.append(name)
        code = seg.get("flightNumber") or seg.get("flightNo")
        if code:
            fn = str(code).replace(" ", "").upper()
            if fn and fn not in numbers:
                numbers.append(fn)
    return airlines, numbers


def _link(
    item: dict[str, Any],
    *,
    origin: str,
    destination: str,
    depart: date,
    adults: int,
) -> str:
    links = item.get("links")
    if isinstance(links, dict):
        for key in ("vietjet", "book", "booking"):
            raw = links.get(key)
            if isinstance(raw, str) and "vietjetair.com" in raw:
                return raw.split()[0]
    return vietjet_link(origin, destination, depart, adults)
