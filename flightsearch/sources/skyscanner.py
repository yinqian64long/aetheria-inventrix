from __future__ import annotations

import math
import re
from collections.abc import Mapping
from datetime import date, datetime, timezone
from typing import Any, ClassVar

import httpx

from flightsearch.context import RunContext
from flightsearch.models import Offer, SearchQuery
from flightsearch.sources.base import Source, SourceError

ACTOR_ID = "makework36~flight-price-scraper"
APIFY_RUN_URL = (
    f"https://api.apify.com/v2/acts/{ACTOR_ID}/run-sync-get-dataset-items"
)
MAX_FLIGHTS = 30
MAX_OFFERS_PER_OD = 20

_DURATION_RE = re.compile(
    r"(?:(\d+)\s*h)?\s*(?:(\d+)\s*m)?",
    re.IGNORECASE,
)


class SkyscannerSource(Source):
    """Skyscanner-adjacent fares via Apify ``makework36/flight-price-scraper``.

    Uses IATA + ``departDate``/``departDateEnd`` so each actor run covers the
    full query date window for one home_origin→destination pair, staying
    within the daily Apify budget.
    """

    name: ClassVar[str] = "skyscanner"

    def is_available(self, env: Mapping[str, str]) -> tuple[bool, str]:
        if not (env.get("APIFY_TOKEN") or "").strip():
            return False, "APIFY_TOKEN not set"
        return True, ""

    async def search(self, query: SearchQuery, ctx: RunContext) -> list[Offer]:
        token = (ctx.env.get("APIFY_TOKEN") or "").strip()
        if not token:
            raise SourceError("APIFY_TOKEN not set")

        today = ctx.now.astimezone(timezone.utc).date()
        date_from = max(query.date_from, today)
        date_to = query.date_to
        if date_from > date_to:
            return []

        pairs = [(query.home_origin, dest) for dest in query.destinations]
        if not pairs:
            return []

        budget = self._runs_this_pipeline(ctx)
        if budget <= 0:
            ctx.log.info("skyscanner: daily actor-run budget exhausted")
            return []

        offers: list[Offer] = []
        failures = 0
        for _ in range(budget):
            origin, dest = self._next_pair(ctx, pairs)
            try:
                items = await self._run_actor(
                    ctx,
                    token=token,
                    origin=origin,
                    destination=dest,
                    date_from=date_from,
                    date_to=date_to,
                    adults=query.adults,
                    currency=query.currency,
                )
            except SourceError:
                raise
            except Exception as exc:
                failures += 1
                ctx.log.warning(
                    "skyscanner: actor run failed for %s→%s: %s",
                    origin,
                    dest,
                    exc,
                )
                continue

            for item in items:
                offer = self._parse_item(
                    item, ctx, fallback_origin=origin, fallback_dest=dest
                )
                if offer is not None:
                    offers.append(offer)

        if failures and failures == budget and not offers:
            raise SourceError(f"skyscanner: all {failures} actor run(s) failed")

        return self._cap_per_od(offers)

    def _runs_this_pipeline(self, ctx: RunContext) -> int:
        max_per_day = int(self.settings.get("max_runs_per_day", 6))
        day = ctx.now.astimezone(timezone.utc).date().isoformat()
        state = ctx.state
        if state.get("day") != day:
            state["day"] = day
            state["runs_today"] = 0
        if "cursor" not in state:
            state["cursor"] = 0

        remaining = max(0, max_per_day - int(state.get("runs_today", 0)))
        per_pipeline = max(1, math.ceil(max_per_day / 4))
        return min(per_pipeline, remaining)

    def _next_pair(
        self, ctx: RunContext, pairs: list[tuple[str, str]]
    ) -> tuple[str, str]:
        cursor = int(ctx.state.get("cursor", 0))
        pair = pairs[cursor % len(pairs)]
        ctx.state["cursor"] = cursor + 1
        ctx.state["runs_today"] = int(ctx.state.get("runs_today", 0)) + 1
        return pair

    async def _run_actor(
        self,
        ctx: RunContext,
        *,
        token: str,
        origin: str,
        destination: str,
        date_from: date,
        date_to: date,
        adults: int,
        currency: str,
    ) -> list[dict[str, Any]]:
        timeout_s = int(self.settings.get("timeout_s", 1200))
        payload = {
            "origin": origin,
            "destination": destination,
            "departDate": date_from.isoformat(),
            "departDateEnd": date_to.isoformat(),
            "adults": adults,
            "cabinClass": "ECONOMY",
            "currency": currency,
            "maxFlights": MAX_FLIGHTS,
        }
        headers = {
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
        }
        ctx.log.info(
            "skyscanner: Apify run %s→%s %s..%s (maxFlights=%s)",
            origin,
            destination,
            date_from.isoformat(),
            date_to.isoformat(),
            MAX_FLIGHTS,
        )
        try:
            resp = await ctx.http.post(
                APIFY_RUN_URL,
                params={"timeout": timeout_s},
                headers=headers,
                json=payload,
                timeout=httpx.Timeout(timeout_s + 30.0),
            )
        except httpx.HTTPError as exc:
            raise RuntimeError(f"HTTP error calling Apify: {exc}") from exc

        if resp.status_code == 402 or _is_insufficient_credit(resp):
            raise SourceError(
                "skyscanner: Apify insufficient credit / payment required "
                f"(HTTP {resp.status_code})"
            )
        if resp.status_code >= 400:
            raise RuntimeError(
                f"Apify HTTP {resp.status_code}: {_safe_body_snippet(resp)}"
            )

        data = resp.json()
        if isinstance(data, list):
            return [x for x in data if isinstance(x, dict)]
        if isinstance(data, dict) and isinstance(data.get("items"), list):
            return [x for x in data["items"] if isinstance(x, dict)]
        return []

    def _parse_item(
        self,
        item: dict[str, Any],
        ctx: RunContext,
        *,
        fallback_origin: str,
        fallback_dest: str,
    ) -> Offer | None:
        price_raw = item.get("bestPrice")
        if price_raw is None and isinstance(item.get("prices"), dict):
            prices = [
                v for v in item["prices"].values() if isinstance(v, (int, float))
            ]
            price_raw = min(prices) if prices else None
        if price_raw is None:
            return None
        try:
            price_original = float(price_raw)
        except (TypeError, ValueError):
            return None

        currency = str(item.get("currency") or "USD").upper()
        try:
            price_usd = ctx.fx.to_usd(price_original, currency)
        except Exception as exc:
            ctx.log.warning(
                "skyscanner: fx failed for %s %s: %s",
                price_original,
                currency,
                exc,
            )
            return None

        origin = _airport_code(item, "from", "origin") or fallback_origin
        destination = _airport_code(item, "to", "destination") or fallback_dest

        depart_date = _parse_depart_date(item)
        if depart_date is None:
            return None

        depart_time = _parse_local_dt(item.get("departTime"))
        arrive_time = _parse_local_dt(item.get("arriveTime"))

        airlines, flight_numbers = _airlines_and_flights(item)
        stops = item.get("stops")
        if stops is not None:
            try:
                stops = int(stops)
            except (TypeError, ValueError):
                stops = None

        duration_minutes = item.get("durationMinutes")
        if duration_minutes is None:
            duration_minutes = _parse_duration_minutes(item.get("duration"))
        elif not isinstance(duration_minutes, int):
            try:
                duration_minutes = int(duration_minutes)
            except (TypeError, ValueError):
                duration_minutes = None

        cabin_bag: bool | None = None
        baggage = item.get("baggage")
        if isinstance(baggage, dict) and "includedHandBags" in baggage:
            try:
                cabin_bag = int(baggage["includedHandBags"]) > 0
            except (TypeError, ValueError):
                cabin_bag = None

        self_transfer = item.get("isSelfTransfer")
        if self_transfer is not None:
            self_transfer = bool(self_transfer)

        link = _skyscanner_link(origin, destination, depart_date)
        notes = ""
        cheapest = item.get("cheapestSource")
        if cheapest:
            notes = f"cheapestSource={cheapest}"

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


def _airport_code(item: dict[str, Any], nested_key: str, flat_key: str) -> str | None:
    nested = item.get(nested_key)
    if isinstance(nested, dict):
        code = nested.get("airport") or nested.get("displayCode")
        if code:
            return str(code).upper()
    flat = item.get(flat_key)
    if flat:
        return str(flat).upper()
    return None


def _parse_depart_date(item: dict[str, Any]) -> date | None:
    raw = item.get("departDate")
    if raw:
        try:
            return date.fromisoformat(str(raw)[:10])
        except ValueError:
            pass
    dt = _parse_local_dt(item.get("departTime"))
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
    try:
        dt = datetime.fromisoformat(text)
    except ValueError:
        return None
    return dt.replace(tzinfo=None) if dt.tzinfo else dt


def _parse_duration_minutes(value: Any) -> int | None:
    if value is None:
        return None
    if isinstance(value, (int, float)):
        return int(value)
    text = str(value).strip()
    match = _DURATION_RE.search(text)
    if not match or (match.group(1) is None and match.group(2) is None):
        return None
    hours = int(match.group(1) or 0)
    minutes = int(match.group(2) or 0)
    return hours * 60 + minutes


def _airlines_and_flights(item: dict[str, Any]) -> tuple[list[str], list[str]]:
    airlines: list[str] = []
    flight_numbers: list[str] = []
    segments = item.get("segments")
    if isinstance(segments, list):
        for seg in segments:
            if not isinstance(seg, dict):
                continue
            airline = seg.get("airline") or seg.get("airlineCode")
            if airline:
                name = str(airline).strip()
                if name and name not in airlines:
                    airlines.append(name)
            code = seg.get("flightCode") or seg.get("flightNumber")
            if code:
                fn = str(code).replace(" ", "").upper()
                if fn and fn not in flight_numbers:
                    flight_numbers.append(fn)
    if not airlines:
        raw = item.get("airline")
        if raw:
            for part in re.split(r"\s*\+\s*|,", str(raw)):
                name = part.strip()
                if name and name not in airlines:
                    airlines.append(name)
    return airlines, flight_numbers


def _skyscanner_link(origin: str, destination: str, depart: date) -> str:
    yymmdd = depart.strftime("%y%m%d")
    return (
        "https://www.skyscanner.net/transport/flights/"
        f"{origin.lower()}/{destination.lower()}/{yymmdd}/"
    )


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
