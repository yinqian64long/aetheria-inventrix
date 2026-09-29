from __future__ import annotations

import asyncio
import math
import re
from collections.abc import Mapping
from datetime import date, datetime, timezone
from typing import Any, ClassVar

import httpx

from flightsearch.context import RunContext
from flightsearch.models import Offer, SearchQuery
from flightsearch.sources.base import Source, SourceError

ACTOR_ID = "lentic_clockss~trip-com-scraper"
MAX_OFFERS_PER_OD = 20
_DEFAULT_MAX_ROUTES_PER_DAY = 4
_DEFAULT_MAX_RESULTS = 8
_DEFAULT_DATE_TIMEOUT_S = 150
_DEFAULT_DATE_CONCURRENCY = 3
_BAGS_NOTE = "bags not verified (Trip.com)"

_DURATION_RE = re.compile(
    r"(?:(\d+)\s*h)?\s*(?:(\d+)\s*m)?",
    re.IGNORECASE,
)
_PRICE_RE = re.compile(r"(\d[\d,]*(?:\.\d+)?)")
_IATA_RE = re.compile(r"^[A-Za-z]{3}$")


def run_url(actor_id: str) -> str:
    return (
        "https://api.apify.com/v2/acts/"
        f"{actor_id}/run-sync-get-dataset-items"
    )


def route_pairs(query: SearchQuery) -> list[tuple[str, str]]:
    """Home origin covers every destination, including WMI.

    Positioning origins cover the main Polish airports only.
    """
    pairs: list[tuple[str, str]] = []
    seen: set[tuple[str, str]] = set()
    for origin, dest in (
        *((query.home_origin, dest) for dest in query.all_destinations),
        *(
            (origin, dest)
            for origin in query.positioning_origins
            for dest in query.destinations
        ),
    ):
        key = (origin.upper(), dest.upper())
        if key in seen or not key[0] or not key[1]:
            continue
        seen.add(key)
        pairs.append(key)
    return pairs


class TripSource(Source):
    """Trip.com one-way fares via Apify ``lentic_clockss/trip-com-scraper``.

    Each pipeline run takes the next origin-destination pair and searches
    every remaining day in the travel window. The actor accepts one departure
    date per run, so those days run together under ``APIFY_TOKEN``. The daily
    cap counts routes, not individual dates.
    """

    name: ClassVar[str] = "trip"

    def is_available(self, env: Mapping[str, str]) -> tuple[bool, str]:
        if not (env.get("APIFY_TOKEN") or "").strip():
            return False, "APIFY_TOKEN not set"
        return True, ""

    async def search(self, query: SearchQuery, ctx: RunContext) -> list[Offer]:
        token = (ctx.env.get("APIFY_TOKEN") or "").strip()
        if not token:
            raise SourceError("APIFY_TOKEN not set")

        today = ctx.now.astimezone(timezone.utc).date()
        dates = query.dates(today)
        pairs = route_pairs(query)
        if not dates or not pairs:
            return []

        budget = self._routes_this_pipeline(ctx)
        if budget <= 0:
            ctx.log.info("trip: daily route budget exhausted")
            return []

        offers: list[Offer] = []
        failures = 0
        for _ in range(budget):
            origin, dest = self._next_pair(ctx, pairs)
            try:
                items_by_date = await self._search_route(
                    ctx,
                    token=token,
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
                ctx.log.warning(
                    "trip: route failed for %s→%s: %s", origin, dest, exc
                )
                continue

            route_items = 0
            for day, items in items_by_date:
                if isinstance(items, Exception):
                    ctx.log.warning(
                        "trip: %s→%s %s failed: %s",
                        origin,
                        dest,
                        day.isoformat(),
                        items,
                    )
                    continue
                route_items += 1
                for item in items:
                    offer = self._parse_item(
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
            raise SourceError(f"trip: all {failures} route(s) failed")
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
        origin: str,
        destination: str,
        depart: date,
        adults: int,
        currency: str,
    ) -> list[dict[str, Any]]:
        timeout_s = int(self.settings.get("date_timeout_s", _DEFAULT_DATE_TIMEOUT_S))
        max_results = int(self.settings.get("max_results", _DEFAULT_MAX_RESULTS))
        actor = _actor_id(str(self.settings.get("actor") or ACTOR_ID))
        payload = {
            "service": "flights",
            "domain": "www.trip.com",
            "origin": origin,
            "destination": destination,
            "departureDate": depart.isoformat(),
            "tripType": "oneway",
            "adults": max(1, int(adults)),
            "children": 0,
            "maxResults": max(1, max_results),
            "currency": currency,
            "locale": "en_US",
            "includeSponsored": False,
            "enrichDetails": False,
        }
        headers = {
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
        }
        ctx.log.info(
            "trip: Apify run %s→%s %s (maxResults=%s)",
            origin,
            destination,
            depart.isoformat(),
            payload["maxResults"],
        )
        try:
            resp = await ctx.http.post(
                run_url(actor),
                params={"timeout": timeout_s},
                headers=headers,
                json=payload,
                timeout=httpx.Timeout(timeout_s + 30.0),
            )
        except httpx.HTTPError as exc:
            raise RuntimeError(f"HTTP error calling Apify: {exc}") from exc

        if resp.status_code == 402 or _is_insufficient_credit(resp):
            raise SourceError(
                "trip: Apify insufficient credit / payment required "
                f"(HTTP {resp.status_code})"
            )
        if resp.status_code >= 400:
            raise RuntimeError(
                f"Apify HTTP {resp.status_code}: {_safe_body_snippet(resp)}"
            )
        return _dataset_items(resp.json())

    def _parse_item(
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
        if item.get("sponsored") is True:
            return None
        service = str(item.get("service") or "").strip().lower()
        if service and service not in {"flights", "flight"}:
            return None

        priced = _price_and_currency(item)
        if priced is None:
            return None
        price_original, currency = priced
        try:
            price_usd = ctx.fx.to_usd(price_original, currency)
        except Exception as exc:
            ctx.log.warning(
                "trip: fx failed for %s %s: %s", price_original, currency, exc
            )
            return None

        origin = _iata(item.get("origin") or item.get("from"), fallback_origin)
        destination = _iata(
            item.get("destination") or item.get("to"), fallback_dest
        )
        depart_date = _depart_date(item) or searched_date
        if depart_date < window_from or depart_date > window_to:
            return None

        depart_time = _parse_local_dt(
            item.get("departureTime") or item.get("departTime")
        )
        arrive_time = _parse_local_dt(
            item.get("arrivalTime") or item.get("arriveTime")
        )
        airlines, flight_numbers = _airlines_and_flights(item)
        stops = _stops(item, flight_numbers)
        duration_minutes = _duration_minutes(item)
        cabin_bag = _cabin_bag(item)
        self_transfer = _self_transfer(item)
        link = _link(
            item,
            origin=origin,
            destination=destination,
            depart=depart_date,
            adults=adults,
            currency=currency,
        )
        notes = "" if cabin_bag is not None else _BAGS_NOTE

        return Offer(
            source=self.name,
            origin=origin,
            destination=destination,
            depart_date=depart_date,
            price_usd=round(price_usd, 2),
            price_original=price_original,
            currency_original=currency,
            depart_time=depart_time,
            arrive_time=arrive_time,
            airlines=airlines,
            flight_numbers=flight_numbers,
            stops=stops,
            duration_minutes=duration_minutes,
            link=link,
            cabin_bag_included=cabin_bag,
            self_transfer=self_transfer,
            notes=notes,
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


def _actor_id(value: str) -> str:
    text = value.strip()
    if not text:
        return ACTOR_ID
    return text.replace("/", "~")


def _dataset_items(data: Any) -> list[dict[str, Any]]:
    if isinstance(data, list):
        return [x for x in data if isinstance(x, dict)]
    if isinstance(data, dict) and isinstance(data.get("items"), list):
        return [x for x in data["items"] if isinstance(x, dict)]
    return []


def _price_and_currency(item: dict[str, Any]) -> tuple[float, str] | None:
    currency = str(item.get("currency") or "").strip().upper()
    raw = item.get("price")
    if raw is None:
        raw = item.get("bestPrice") or item.get("totalPrice")
    if isinstance(raw, dict):
        if not currency:
            currency = str(raw.get("currency") or "").strip().upper()
        raw = raw.get("amount")
        if raw is None:
            raw = raw.get("value")
    amount = _as_amount(raw)
    if amount is None:
        display = item.get("priceDisplay")
        amount = _as_amount(display)
        if amount is not None and not currency:
            text = str(display).upper()
            if "US$" in text or text.startswith("$"):
                currency = "USD"
            elif "€" in text:
                currency = "EUR"
    if amount is None:
        return None
    return amount, currency or "USD"


def _as_amount(value: Any) -> float | None:
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


def _iata(value: Any, fallback: str) -> str:
    if isinstance(value, dict):
        value = value.get("airport") or value.get("code") or value.get("iata")
    if isinstance(value, str) and _IATA_RE.match(value.strip()):
        return value.strip().upper()
    return fallback


def _depart_date(item: dict[str, Any]) -> date | None:
    for key in ("departureDate", "departDate", "date"):
        raw = item.get(key)
        if not raw:
            continue
        try:
            return date.fromisoformat(str(raw)[:10])
        except ValueError:
            continue
    dt = _parse_local_dt(item.get("departureTime") or item.get("departTime"))
    if dt is not None:
        return dt.date()
    return None


def _parse_local_dt(value: Any) -> datetime | None:
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


def _duration_minutes(item: dict[str, Any]) -> int | None:
    raw = item.get("durationMinutes")
    if isinstance(raw, (int, float)) and not isinstance(raw, bool):
        return int(raw)
    text = item.get("duration") or item.get("durationText")
    if text is None:
        return None
    if isinstance(text, (int, float)) and not isinstance(text, bool):
        return int(text)
    match = _DURATION_RE.search(str(text).strip())
    if not match or (match.group(1) is None and match.group(2) is None):
        return None
    return int(match.group(1) or 0) * 60 + int(match.group(2) or 0)


def _segments(item: dict[str, Any]) -> list[dict[str, Any]]:
    raw = item.get("segments") or item.get("legs")
    if not isinstance(raw, list):
        return []
    return [seg for seg in raw if isinstance(seg, dict)]


def _airlines_and_flights(item: dict[str, Any]) -> tuple[list[str], list[str]]:
    airlines: list[str] = []
    flight_numbers: list[str] = []
    for seg in _segments(item):
        airline = seg.get("airline") or seg.get("airlineName") or seg.get("carrier")
        if airline:
            name = str(airline).strip()
            if name and name not in airlines:
                airlines.append(name)
        code = seg.get("flightNumber") or seg.get("flightNo") or seg.get("flightCode")
        if code:
            fn = str(code).replace(" ", "").upper()
            if fn and fn not in flight_numbers:
                flight_numbers.append(fn)
    if not airlines:
        raw = item.get("airline") or item.get("airlines")
        if isinstance(raw, list):
            parts = [str(part) for part in raw]
        elif raw:
            parts = re.split(r"\s*\+\s*|,", str(raw))
        else:
            parts = []
        for part in parts:
            name = part.strip()
            if name and name not in airlines:
                airlines.append(name)
    raw_numbers = item.get("flightNumber") or item.get("flightNo") or item.get(
        "flightNumbers"
    )
    if isinstance(raw_numbers, list):
        number_parts = [str(part) for part in raw_numbers]
    elif raw_numbers:
        number_parts = re.split(r"\s*[+,/]\s*", str(raw_numbers))
    else:
        number_parts = []
    for part in number_parts:
        fn = part.replace(" ", "").upper()
        if fn and fn not in flight_numbers:
            flight_numbers.append(fn)
    return airlines, flight_numbers


def _stops(item: dict[str, Any], flight_numbers: list[str]) -> int | None:
    for key in ("stops", "stopCount", "numberOfStops"):
        raw = item.get(key)
        if raw is None or raw == "":
            continue
        try:
            return int(raw)
        except (TypeError, ValueError):
            continue
    segs = _segments(item)
    if segs:
        return max(0, len(segs) - 1)
    if len(flight_numbers) > 1:
        return len(flight_numbers) - 1
    return None


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


def _cabin_bag(item: dict[str, Any]) -> bool | None:
    for key in ("cabinBagIncluded", "carryOnIncluded", "cabin_bag_included"):
        found = _as_bool(item.get(key))
        if found is not None:
            return found
    baggage = item.get("baggage")
    if isinstance(baggage, dict):
        for key in ("cabinBag", "includedHandBags", "carryOn", "cabin"):
            found = _as_bool(baggage.get(key))
            if found is not None:
                return found
    return None


def _self_transfer(item: dict[str, Any]) -> bool | None:
    for key in ("selfTransfer", "isSelfTransfer", "separateTickets"):
        found = _as_bool(item.get(key))
        if found is not None:
            return found
    return None


def _link(
    item: dict[str, Any],
    *,
    origin: str,
    destination: str,
    depart: date,
    adults: int,
    currency: str,
) -> str:
    raw = item.get("url") or item.get("link")
    if isinstance(raw, str) and "trip.com" in raw and "/flights" in raw:
        return raw.split()[0]
    query = (
        f"dcity={origin.lower()}&acity={destination.lower()}"
        f"&ddate={depart.isoformat()}&triptype=ow&class=y"
        f"&quantity={max(1, int(adults))}&locale=en-US&curr={currency}"
    )
    return f"https://www.trip.com/flights/showfarefirst?{query}"


def _is_insufficient_credit(resp: httpx.Response) -> bool:
    text = (resp.text or "").lower()
    needles = (
        "insufficient credit",
        "insufficient permissions",
        "payment required",
        "not enough credit",
        "monthly usage",
    )
    return any(n in text for n in needles)


def _safe_body_snippet(resp: httpx.Response, limit: int = 200) -> str:
    text = (resp.text or "").replace("\n", " ").strip()
    return text[:limit]
