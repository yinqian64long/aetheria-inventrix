from __future__ import annotations

import asyncio
import math
import re
from abc import abstractmethod
from collections.abc import Mapping
from datetime import date, datetime, timezone
from typing import Any, ClassVar

import httpx

from flightsearch.context import RunContext
from flightsearch.models import Offer, SearchQuery
from flightsearch.sources.base import Source, SourceError

MAX_OFFERS_PER_OD = 20
_DEFAULT_MAX_ROUTES_PER_DAY = 4
_DEFAULT_MAX_RESULTS = 10
_DEFAULT_DATE_TIMEOUT_S = 240
_DEFAULT_DATE_CONCURRENCY = 2


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
        if not (env.get("APIFY_TOKEN") or "").strip():
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
        token = (ctx.env.get("APIFY_TOKEN") or "").strip()
        if not token:
            raise SourceError("APIFY_TOKEN not set")

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
            raise SourceError(f"{self.log_name}: all {failures} route(s) failed")
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
        actor = self.actor()
        payload = self.build_payload(
            origin=origin,
            destination=destination,
            depart=depart,
            adults=max(1, int(adults)),
            currency=currency,
            max_results=max(1, max_results),
        )
        headers = {
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
        }
        ctx.log.info(
            "%s: Apify run %s %s→%s %s (maxResults=%s)",
            self.log_name,
            actor,
            origin,
            destination,
            depart.isoformat(),
            payload.get("maxResults"),
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
                f"{self.log_name}: Apify insufficient credit / payment required "
                f"(HTTP {resp.status_code})"
            )
        if resp.status_code >= 400:
            raise RuntimeError(
                f"Apify HTTP {resp.status_code}: {_safe_body_snippet(resp)}"
            )
        return _dataset_items(resp.json())

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
