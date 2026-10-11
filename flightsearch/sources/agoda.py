from __future__ import annotations

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

ACTOR_ID = "romy~agoda-flight-scraper"
BLASPAT_ACTOR = "blaspat~agoda-flights-scraper"


def agoda_link(origin: str, destination: str, depart: date, adults: int) -> str:
    return (
        "https://www.agoda.com/flights/results"
        f"?departureFrom={origin}&arrivalTo={destination}"
        f"&departDate={depart.isoformat()}&searchType=1"
        f"&cabinType=Economy&adults={max(1, int(adults))}"
    )


class AgodaSource(DatedRouteSource):
    """Agoda one-way fares via Apify ``romy/agoda-flight-scraper``.

    ``actor`` can be switched to ``blaspat/agoda-flights-scraper``. Both
    accept an IATA pair and one departure date.
    """

    name: ClassVar[str] = "agoda"
    actor_id: ClassVar[str] = ACTOR_ID
    log_name: ClassVar[str] = "agoda"

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
        if "blaspat" in self.actor():
            return {
                "origin": origin,
                "destination": destination,
                "departDate": depart.isoformat(),
                "cabin": "Economy",
                "adults": adults,
                "sort": "cheapest",
                "currency": currency,
                "maxResults": max_results,
            }
        return {
            "origin": origin,
            "destination": destination,
            "departDate": depart.isoformat(),
            "adults": adults,
            "children": 0,
            "infants": 0,
            "cabin": "Economy",
            "currency": currency,
            "locale": "en-us",
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
        priced = price_and_currency(item)
        if priced is None:
            return None
        price_original, currency = priced
        try:
            price_usd = ctx.fx.to_usd(price_original, currency)
        except Exception as exc:
            ctx.log.warning("agoda: fx failed for %s %s: %s", price_original, currency, exc)
            return None

        segments = _segments(item)
        origin = iata(
            item.get("search_origin") or item.get("origin") or item.get("from"),
            fallback_origin,
        )
        destination = iata(
            item.get("search_destination") or item.get("destination") or item.get("to"),
            fallback_dest,
        )
        if segments:
            origin = iata(_endpoint(segments[0], "from"), origin)
            destination = iata(_endpoint(segments[-1], "to"), destination)

        depart_date = depart_date_of(item, searched_date)
        if depart_date < window_from or depart_date > window_to:
            return None

        depart_time = _segment_time(segments, item, depart_date, depart=True)
        arrive_time = _segment_time(segments, item, depart_date, depart=False)
        airlines, flight_numbers = _airlines(item, segments)
        stops = _stops(item, segments, flight_numbers)
        minutes = _duration(item)
        baggage = _baggage_blobs(item, segments)
        cabin = cabin_bag_included(baggage) if baggage else None
        checked = checked_bag_included(baggage) if baggage else None
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
            cabin_bag_included=cabin,
            checked_bag_included=checked,
        )


def _segments(item: dict[str, Any]) -> list[dict[str, Any]]:
    for key in ("segments",):
        raw = item.get(key)
        if isinstance(raw, list):
            return [seg for seg in raw if isinstance(seg, dict)]
    for key in ("outbound", "outboundSlice"):
        nested = item.get(key)
        if isinstance(nested, dict) and isinstance(nested.get("segments"), list):
            return [seg for seg in nested["segments"] if isinstance(seg, dict)]
    legs = item.get("legs")
    if isinstance(legs, list) and legs:
        first = legs[0]
        if isinstance(first, dict) and isinstance(first.get("segments"), list):
            return [seg for seg in first["segments"] if isinstance(seg, dict)]
    return []


def _endpoint(segment: dict[str, Any], end: str) -> Any:
    if end == "from":
        return (
            segment.get("origin")
            or segment.get("originCode")
            or segment.get("from")
            or segment.get("departureAirport")
        )
    return (
        segment.get("destination")
        or segment.get("destinationCode")
        or segment.get("to")
        or segment.get("arrivalAirport")
    )


def _segment_time(
    segments: list[dict[str, Any]],
    item: dict[str, Any],
    day: date,
    *,
    depart: bool,
) -> Any:
    if segments:
        seg = segments[0] if depart else segments[-1]
        keys = (
            ("departureTime", "departDateTime", "departure")
            if depart
            else ("arrivalTime", "arriveDateTime", "arrival")
        )
        for key in keys:
            if seg.get(key):
                return clock_on_date(day, seg[key])
    key = "departureTime" if depart else "arrivalTime"
    raw = item.get(key) or item.get("departTime" if depart else "arriveTime")
    if raw:
        return clock_on_date(day, raw)
    return None


def _airlines(
    item: dict[str, Any], segments: list[dict[str, Any]]
) -> tuple[list[str], list[str]]:
    airlines: list[str] = []
    numbers: list[str] = []
    for seg in segments:
        airline = (
            seg.get("airlineName")
            or seg.get("airline")
            or seg.get("carrier")
            or seg.get("airlineCode")
        )
        if isinstance(airline, dict):
            airline = airline.get("name") or airline.get("code")
        if airline:
            name = str(airline).strip()
            if name and name not in airlines:
                airlines.append(name)
        code = seg.get("flightNumber") or seg.get("flightNo")
        if code:
            fn = str(code).replace(" ", "").upper()
            if fn and fn not in numbers:
                numbers.append(fn)
    if not airlines:
        raw = item.get("airline") or item.get("ticketingAirline") or item.get("airlines")
        if isinstance(raw, list):
            parts = [str(part) for part in raw]
        elif raw:
            parts = [str(raw)]
        else:
            parts = []
        for part in parts:
            name = part.strip()
            if name and name not in airlines:
                airlines.append(name)
    if not numbers:
        raw = item.get("flightNumber") or item.get("flightNo")
        if raw:
            fn = str(raw).replace(" ", "").upper()
            if fn:
                numbers.append(fn)
    return airlines, numbers


def _stops(
    item: dict[str, Any],
    segments: list[dict[str, Any]],
    flight_numbers: list[str],
) -> int | None:
    for key in ("stops", "totalStops", "stopCount"):
        raw = item.get(key)
        if raw is None or raw == "":
            continue
        try:
            return int(raw)
        except (TypeError, ValueError):
            continue
    if segments:
        return max(0, len(segments) - 1)
    if len(flight_numbers) > 1:
        return len(flight_numbers) - 1
    return None


def _duration(item: dict[str, Any]) -> int | None:
    for key in ("durationMinutes", "totalDurationMinutes"):
        raw = item.get(key)
        if isinstance(raw, (int, float)) and not isinstance(raw, bool):
            return int(raw)
    nested = item.get("outbound")
    if isinstance(nested, dict):
        raw = nested.get("durationMinutes")
        if isinstance(raw, (int, float)) and not isinstance(raw, bool):
            return int(raw)
    return duration_minutes(item.get("duration") or item.get("durationText"))


def _baggage_blobs(
    item: dict[str, Any], segments: list[dict[str, Any]]
) -> list[Any]:
    blobs: list[Any] = []
    for key in ("baggage", "baggageAllowance"):
        if item.get(key) is not None:
            blobs.append(item[key])
    offer = item.get("bestOffer")
    if isinstance(offer, dict) and offer.get("baggage") is not None:
        blobs.append(offer["baggage"])
    for seg in segments:
        for key in ("baggage", "baggageAllowance"):
            if seg.get(key) is not None:
                blobs.append(seg[key])
    return blobs


def _link(
    item: dict[str, Any],
    *,
    origin: str,
    destination: str,
    depart: date,
    adults: int,
) -> str:
    for key in ("url", "link", "bookingUrl", "shareableUrl"):
        raw = item.get(key)
        if isinstance(raw, str) and "agoda.com" in raw:
            return raw.split()[0]
    offer = item.get("bestOffer")
    if isinstance(offer, dict):
        raw = offer.get("bookingUrl") or offer.get("url")
        if isinstance(raw, str) and "agoda.com" in raw:
            return raw.split()[0]
    return agoda_link(origin, destination, depart, adults)
