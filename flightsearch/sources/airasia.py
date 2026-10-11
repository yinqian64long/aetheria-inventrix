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
    iata,
    price_and_currency,
)

ACTOR_ID = "mscraper~airasia-flights-scraper"


def airasia_link(origin: str, destination: str, depart: date, adults: int) -> str:
    return (
        "https://www.airasia.com/flights/search/"
        f"?origin={origin}&destination={destination}"
        f"&departDate={depart.strftime('%d/%m/%Y')}"
        f"&tripType=O&adult={max(1, int(adults))}&child=0&infant=0"
        "&locale=en-gb&currency=USD"
    )


class AirAsiaSource(DatedRouteSource):
    """AirAsia one-way fares via Apify ``mscraper/airasia-flights-scraper``."""

    name: ClassVar[str] = "airasia"
    actor_id: ClassVar[str] = ACTOR_ID
    log_name: ClassVar[str] = "airasia"

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
            "origin": origin,
            "destination": destination,
            "departureDate": depart.isoformat(),
            "adults": adults,
            "children": 0,
            "infants": 0,
            "currency": currency,
            "countryCode": "TH",
            "maxResults": min(30, max_results),
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
        if str(item.get("type") or "").upper() == "SEARCH_REPORT":
            return None
        priced = price_and_currency(item)
        if priced is None:
            return None
        price_original, currency = priced
        try:
            price_usd = ctx.fx.to_usd(price_original, currency)
        except Exception as exc:
            ctx.log.warning(
                "airasia: fx failed for %s %s: %s", price_original, currency, exc
            )
            return None

        segments = [
            seg for seg in (item.get("segments") or []) if isinstance(seg, dict)
        ]
        origin = iata(item.get("origin"), fallback_origin)
        destination = iata(item.get("destination"), fallback_dest)
        if segments:
            origin = iata(segments[0].get("origin") or segments[0].get("from"), origin)
            destination = iata(
                segments[-1].get("destination") or segments[-1].get("to"),
                destination,
            )
        depart_date = depart_date_of(item, searched_date)
        if depart_date < window_from or depart_date > window_to:
            return None

        depart_raw = item.get("departureTimeLocal") or item.get("departureTime")
        arrive_raw = item.get("arrivalTimeLocal") or item.get("arrivalTime")
        if segments:
            depart_raw = depart_raw or segments[0].get("departureTimeLocal") or segments[0].get(
                "departureTime"
            )
            arrive_raw = arrive_raw or segments[-1].get("arrivalTimeLocal") or segments[-1].get(
                "arrivalTime"
            )
        airlines, flight_numbers = _airlines(item, segments)
        stops = item.get("stops")
        try:
            stops = int(stops) if stops is not None and stops != "" else None
        except (TypeError, ValueError):
            stops = max(0, len(segments) - 1) if segments else None
        if stops is None and segments:
            stops = max(0, len(segments) - 1)
        seconds = item.get("durationSeconds")
        minutes = int(seconds) // 60 if isinstance(seconds, (int, float)) and not isinstance(seconds, bool) else None
        baggage = item.get("baggage")
        self_transfer = item.get("selfTransfer")
        if self_transfer is None:
            self_transfer = item.get("isSelfTransfer")
        link = item.get("searchUrl")
        if not isinstance(link, str) or "airasia.com" not in link:
            link = airasia_link(origin, destination, depart_date, adults)
        return Offer(
            source=self.name,
            origin=origin,
            destination=destination,
            depart_date=depart_date,
            price_usd=round(float(price_usd), 2),
            price_original=price_original,
            currency_original=currency,
            depart_time=clock_on_date(depart_date, depart_raw) if depart_raw else None,
            arrive_time=clock_on_date(depart_date, arrive_raw) if arrive_raw else None,
            airlines=airlines,
            flight_numbers=flight_numbers,
            stops=stops,
            duration_minutes=minutes,
            link=link,
            cabin_bag_included=cabin_bag_included(baggage) if baggage is not None else None,
            checked_bag_included=checked_bag_included(baggage) if baggage is not None else None,
            self_transfer=bool(self_transfer) if self_transfer is not None else None,
        )


def _airlines(
    item: dict[str, Any], segments: list[dict[str, Any]]
) -> tuple[list[str], list[str]]:
    airlines: list[str] = []
    numbers: list[str] = []
    raw = item.get("airlines")
    if isinstance(raw, list):
        for part in raw:
            name = str(part).strip()
            if name and name not in airlines:
                airlines.append(name)
    elif raw:
        name = str(raw).strip()
        if name:
            airlines.append(name)
    for seg in segments:
        airline = seg.get("airline") or seg.get("airlineName") or seg.get("carrier")
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
