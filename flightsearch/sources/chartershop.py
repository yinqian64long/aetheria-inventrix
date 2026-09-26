from __future__ import annotations

import json
import re
from datetime import date, datetime, time, timedelta
from typing import Any, ClassVar
from urllib.parse import urlencode

import httpx
from bs4 import BeautifulSoup

from flightsearch.context import RunContext
from flightsearch.models import Offer, SearchQuery
from flightsearch.sources.base import Source, SourceError

CHARTER_NOTE = (
    "charter: Thai CAA may only allow this if you flew out on the same charter"
)

_UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
)
_BASE = "https://chartershop.pl"
_TH_PL_URL = f"{_BASE}/aviatickets/TH/PL"
_MIN_PRICES_URL = (
    f"{_BASE}/index.php/?option=com_charters&view=charters"
    "&format=json&cmd=adv-min-prices"
)
_TIMEOUT = 30.0
_MAX_PER_OD = 20
# 1 schedule page + up to 6 OD min-price calls keeps us well under 10 requests.
_MAX_PRICE_ROUTES = 6

_TH_AIRPORTS = {
    "BKK",
    "DMK",
    "HKT",
    "CNX",
    "KBV",
    "UTP",
    "CEI",
    "HDY",
    "KKC",
    "NST",
    "PHS",
    "URT",
    "TDX",
    "UTH",
}
_PL_AIRPORTS = {
    "WAW",
    "WMI",
    "KTW",
    "KRK",
    "GDN",
    "WRO",
    "POZ",
    "RZE",
    "BZG",
    "SZZ",
    "LCJ",
}

# Polish weekday abbreviations → Python weekday (Mon=0).
_WEEKDAYS = {
    "pon": 0,
    "wt": 1,
    "śr": 2,
    "sr": 2,
    "czw": 3,
    "pt": 4,
    "sob": 5,
    "nd": 6,
    "niedz": 6,
}

_CHALLENGE_MARKERS = (
    "just a moment",
    "cf-challenge",
    "cf-turnstile",
    "sprawdzenie bezpieczeństwa",
    "charters-verify-container",
    "attention required",
)


def _headers_html() -> dict[str, str]:
    return {
        "User-Agent": _UA,
        "Accept": "text/html,application/xhtml+xml",
        "Accept-Language": "pl-PL,pl;q=0.9,en;q=0.8",
        "Referer": _BASE + "/",
    }


def _headers_xhr() -> dict[str, str]:
    return {
        "User-Agent": _UA,
        "Accept": "application/json, text/javascript, */*; q=0.01",
        "Accept-Language": "pl-PL,pl;q=0.9,en;q=0.8",
        "Referer": _TH_PL_URL,
        "X-Requested-With": "XMLHttpRequest",
        "Origin": _BASE,
        "Content-Type": "application/x-www-form-urlencoded; charset=UTF-8",
    }


def _looks_challenged(status: int, body: str) -> bool:
    if status in {401, 403, 429, 503}:
        return True
    low = body[:5000].lower()
    return any(marker in low for marker in _CHALLENGE_MARKERS)


def _parse_hhmm(value: str) -> time | None:
    text = (value or "").strip()
    if ":" not in text:
        return None
    # Arrival may be "19:35+1"
    text = text.split("+", 1)[0].strip()
    parts = text.split(":")
    try:
        return time(int(parts[0]), int(parts[1]))
    except (TypeError, ValueError, IndexError):
        return None


def _combine(day: date, clock: time | None) -> datetime | None:
    if clock is None:
        return None
    return datetime(day.year, day.month, day.day, clock.hour, clock.minute)


def _normalize_flight_number(raw: str) -> str:
    return re.sub(r"\s+", "", (raw or "").upper())


def parse_schedule(html: str) -> list[dict[str, Any]]:
    """Parse TH→PL schedule rows from the country-pair (or OD) page."""
    if "schedule__table" not in html and "schedule__airport" not in html:
        raise SourceError(
            "chartershop: schedule markup missing (layout changed?)"
        )
    soup = BeautifulSoup(html, "html.parser")
    rows: list[dict[str, Any]] = []
    for tr in soup.select("tr.tbody"):
        airports = [
            span.get_text(strip=True).upper()
            for span in tr.select("span.schedule__airport")
        ]
        if len(airports) < 2:
            continue
        origin, dest = airports[0], airports[1]
        if origin not in _TH_AIRPORTS or dest not in _PL_AIRPORTS:
            # Skip reverse legs (PL→TH) listed on the same table.
            continue
        cells = tr.find_all("td")
        if len(cells) < 6:
            continue
        weekday_raw = cells[0].get_text(" ", strip=True).lower()
        weekday = None
        for key, value in _WEEKDAYS.items():
            if weekday_raw.startswith(key):
                weekday = value
                break
        if weekday is None:
            continue
        flight_raw = cells[1].get_text(" ", strip=True).split()
        # First two tokens are usually "LO 6280"; plane type follows.
        flight_no = ""
        if len(flight_raw) >= 2 and re.match(r"^[A-Z0-9]{2}$", flight_raw[0], re.I):
            flight_no = _normalize_flight_number(flight_raw[0] + flight_raw[1])
        elif flight_raw:
            flight_no = _normalize_flight_number(flight_raw[0])
        img = cells[2].find("img")
        airline = ""
        if img is not None:
            airline = (
                img.get("title") or img.get("alt") or ""
            ).strip()
        dep_t = _parse_hhmm(cells[4].get_text(" ", strip=True))
        arr_t = _parse_hhmm(cells[5].get_text(" ", strip=True))
        arr_plus = 1 if "+1" in cells[5].get_text(" ", strip=True) else 0
        rows.append(
            {
                "origin": origin,
                "destination": dest,
                "weekday": weekday,
                "flight_number": flight_no,
                "airline": airline,
                "depart_time": dep_t,
                "arrive_time": arr_t,
                "arrive_day_offset": arr_plus,
            }
        )
    return rows


def parse_min_prices(payload: dict[str, Any] | list[Any]) -> dict[date, float]:
    """Normalize min-price calendar payload into {date: price_pln}."""
    raw: Any
    if isinstance(payload, dict):
        if "min_prices" in payload:
            raw = payload.get("min_prices")
        elif "minPrices" in payload:
            raw = payload.get("minPrices")
        else:
            # Some responses are the map itself.
            raw = payload
    else:
        raw = payload
    if raw is None:
        return {}
    # Live API returns [] when the route has no priced calendar.
    if isinstance(raw, list):
        if not raw:
            return {}
        raise SourceError("chartershop: min_prices list is unexpected")
    if not isinstance(raw, dict):
        raise SourceError("chartershop: min_prices is not an object")
    out: dict[date, float] = {}
    for key, value in raw.items():
        day = _parse_price_date(str(key))
        if day is None:
            continue
        try:
            price = float(value)
        except (TypeError, ValueError):
            continue
        out[day] = price
    return out


def _parse_price_date(text: str) -> date | None:
    text = text.strip()
    # dd.mm.yyyy
    m = re.match(r"^(\d{2})\.(\d{2})\.(\d{4})$", text)
    if m:
        d, mth, y = map(int, m.groups())
        try:
            return date(y, mth, d)
        except ValueError:
            return None
    try:
        return date.fromisoformat(text[:10])
    except ValueError:
        return None


def offers_from_schedule_and_prices(
    schedule: list[dict[str, Any]],
    prices_by_od: dict[tuple[str, str], dict[date, float]],
    *,
    query: SearchQuery,
    fx: Any,
) -> list[Offer]:
    wanted = {d.upper() for d in query.all_destinations} or set(_PL_AIRPORTS)
    out: list[Offer] = []
    # Index schedule by OD + weekday.
    by_od_wd: dict[tuple[str, str, int], list[dict[str, Any]]] = {}
    for row in schedule:
        key = (row["origin"], row["destination"], row["weekday"])
        by_od_wd.setdefault(key, []).append(row)

    for (origin, dest), price_map in prices_by_od.items():
        if dest not in wanted:
            continue
        for day, price_pln in price_map.items():
            if day < query.date_from or day > query.date_to:
                continue
            rows = by_od_wd.get((origin, dest, day.weekday())) or []
            if not rows:
                # Only emit priced dates that fall on a scheduled weekday.
                continue
            for row in rows:
                depart_dt = _combine(day, row.get("depart_time"))
                arrive_dt = None
                if row.get("arrive_time") is not None:
                    arrive_day = day + timedelta(days=int(row.get("arrive_day_offset") or 0))
                    arrive_dt = _combine(arrive_day, row.get("arrive_time"))
                flight_no = row.get("flight_number") or ""
                airline = row.get("airline") or ""
                out.append(
                    Offer(
                        source="chartershop",
                        origin=origin,
                        destination=dest,
                        depart_date=day,
                        price_usd=float(fx.to_usd(float(price_pln), "PLN")),
                        price_original=float(price_pln),
                        currency_original="PLN",
                        depart_time=depart_dt,
                        arrive_time=arrive_dt,
                        airlines=[airline] if airline else [],
                        flight_numbers=[flight_no] if flight_no else [],
                        stops=0,
                        link=f"{_BASE}/aviatickets/{origin}/{dest}",
                        cabin_bag_included=None,
                        notes=CHARTER_NOTE,
                    )
                )
    return out


def _dedupe_cap(offers: list[Offer]) -> list[Offer]:
    offers = sorted(
        offers,
        key=lambda item: (
            item.price_usd,
            item.depart_date,
            item.origin,
            item.destination,
        ),
    )
    seen: set[str] = set()
    per_od: dict[tuple[str, str], int] = {}
    out: list[Offer] = []
    for offer in offers:
        key = offer.key()
        if key in seen:
            continue
        od = (offer.origin, offer.destination)
        if per_od.get(od, 0) >= _MAX_PER_OD:
            continue
        seen.add(key)
        per_od[od] = per_od.get(od, 0) + 1
        out.append(offer)
    return out


class ChartershopSource(Source):
    name: ClassVar[str] = "chartershop"

    async def search(self, query: SearchQuery, ctx: RunContext) -> list[Offer]:
        html = await self._fetch_schedule(ctx)
        schedule = parse_schedule(html)
        if not schedule:
            return []

        # Unique TH→PL ODs, preferring ones that match query destinations.
        wanted = {d.upper() for d in query.all_destinations} or set(_PL_AIRPORTS)
        ods: list[tuple[str, str]] = []
        seen: set[tuple[str, str]] = set()
        for row in schedule:
            od = (row["origin"], row["destination"])
            if od in seen:
                continue
            if row["destination"] not in wanted:
                continue
            seen.add(od)
            ods.append(od)
        ods = ods[:_MAX_PRICE_ROUTES]

        prices_by_od: dict[tuple[str, str], dict[date, float]] = {}
        for origin, dest in ods:
            prices_by_od[(origin, dest)] = await self._fetch_min_prices(
                ctx, origin=origin, dest=dest
            )

        return _dedupe_cap(
            offers_from_schedule_and_prices(
                schedule, prices_by_od, query=query, fx=ctx.fx
            )
        )

    async def _fetch_schedule(self, ctx: RunContext) -> str:
        try:
            resp = await ctx.http.get(
                _TH_PL_URL, headers=_headers_html(), timeout=_TIMEOUT
            )
        except httpx.HTTPError as exc:
            raise SourceError(f"chartershop: schedule request failed: {exc}") from exc
        body = resp.text
        if _looks_challenged(resp.status_code, body):
            raise SourceError(
                f"chartershop: blocked or challenged (HTTP {resp.status_code})"
            )
        if resp.status_code >= 400:
            raise SourceError(f"chartershop: schedule HTTP {resp.status_code}")
        return body

    async def _fetch_min_prices(
        self, ctx: RunContext, *, origin: str, dest: str
    ) -> dict[date, float]:
        data = {
            "from": origin,
            "to": dest,
            "ticket_type": "-1",
            "ticket_class": "0",
            "fare": "-1",
            "bus": "0",
            "departure": "",
            "return": "",
            "price": "",
            "nights": "",
            "currency": "pln",
        }
        try:
            resp = await ctx.http.post(
                _MIN_PRICES_URL,
                data=data,
                headers=_headers_xhr(),
                timeout=_TIMEOUT,
            )
        except httpx.HTTPError as exc:
            raise SourceError(
                f"chartershop: min-prices request failed ({origin}-{dest}): {exc}"
            ) from exc
        body = resp.text
        if _looks_challenged(resp.status_code, body):
            raise SourceError(
                f"chartershop: min-prices blocked ({origin}-{dest}, "
                f"HTTP {resp.status_code})"
            )
        if resp.status_code >= 400:
            raise SourceError(
                f"chartershop: min-prices HTTP {resp.status_code} for {origin}-{dest}"
            )
        try:
            payload = resp.json()
        except json.JSONDecodeError as exc:
            raise SourceError(
                f"chartershop: min-prices response is not JSON ({origin}-{dest}): {exc}"
            ) from exc
        return parse_min_prices(payload)
