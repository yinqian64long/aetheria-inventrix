from __future__ import annotations

import json
import re
from datetime import date, datetime, time, timedelta
from typing import Any, ClassVar
from urllib.parse import urlencode

import httpx

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
_BASE = "https://www.itaka.pl/bilety-lotnicze/"
_TIMEOUT = 40.0
_MAX_PAGES = 10
_MAX_PER_OD = 20

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
    "PRG",
    "RZE",
    "BZG",
    "SZZ",
    "LCJ",
}

_NEXT_F_RE = re.compile(
    r'self\.__next_f\.push\(\[1,"((?:\\.|[^"\\])*)"\]\)</script>',
    re.DOTALL,
)
_CHALLENGE_MARKERS = (
    "just a moment",
    "cf-challenge",
    "cf-turnstile",
    "attention required",
    "enable javascript and cookies",
)


def _headers() -> dict[str, str]:
    return {
        "User-Agent": _UA,
        "Accept": "text/html,application/xhtml+xml",
        "Accept-Language": "pl-PL,pl;q=0.9,en;q=0.8",
        "Referer": "https://www.itaka.pl/",
    }


def _looks_challenged(status: int, body: str) -> bool:
    if status in {401, 403, 429, 503}:
        return True
    low = body[:5000].lower()
    return any(marker in low for marker in _CHALLENGE_MARKERS)


def _parse_iso_date(value: Any) -> date | None:
    if value is None:
        return None
    text = str(value).strip()
    if not text:
        return None
    try:
        return date.fromisoformat(text[:10])
    except ValueError:
        return None


def _parse_hhmm(value: Any) -> time | None:
    if value is None:
        return None
    text = str(value).strip()
    if not text or ":" not in text:
        return None
    parts = text.split(":")
    try:
        return time(int(parts[0]), int(parts[1]))
    except (TypeError, ValueError, IndexError):
        return None


def _combine(day: date, clock: time | None) -> datetime | None:
    if clock is None:
        return None
    return datetime(day.year, day.month, day.day, clock.hour, clock.minute)


def _parse_price_pln(value: Any) -> float | None:
    if value is None:
        return None
    if isinstance(value, (int, float)):
        return float(value)
    text = str(value)
    # Keep digits only — handles "5 996 zł", NBSP, Mojibake, etc.
    digits = re.sub(r"[^\d]", "", text)
    if not digits:
        return None
    try:
        return float(digits)
    except ValueError:
        return None


def _unescape_next_chunk(raw: str) -> str:
    # Chunks are JS string literals; unicode_escape recovers UTF-8 sequences.
    try:
        return raw.encode("utf-8").decode("unicode_escape")
    except UnicodeDecodeError:
        return raw


def extract_flights_payload(html: str) -> dict[str, Any]:
    """Pull the flightsList JSON object out of Next.js RSC `__next_f` chunks."""
    chunks = _NEXT_F_RE.findall(html)
    if not chunks:
        # Broader fallback used during recon.
        chunks = re.findall(
            r'self\.__next_f\.push\(\[1,"(.*?)"]\)</script>', html, re.DOTALL
        )
    if not chunks:
        raise SourceError(
            "itaka: page missing self.__next_f flight chunks (layout/CF changed?)"
        )
    text = "".join(_unescape_next_chunk(chunk) for chunk in chunks)
    marker = '{"flightsList":'
    idx = text.find(marker)
    if idx < 0:
        # Some builds key the list differently — still treat as layout break
        # when the page clearly rendered without flight data.
        if "bilety-lotnicze" in html and "flightsList" not in text:
            raise SourceError(
                "itaka: __next_f payload missing flightsList key (layout changed?)"
            )
        return {"flightsList": [], "pagesCount": 0}

    blob = text[idx:]
    depth = 0
    end = None
    for i, ch in enumerate(blob):
        if ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                end = i + 1
                break
    if end is None:
        raise SourceError("itaka: flightsList JSON is truncated")
    try:
        payload = json.loads(blob[:end])
    except json.JSONDecodeError as exc:
        raise SourceError(f"itaka: flightsList JSON is invalid: {exc}") from exc
    if not isinstance(payload, dict) or "flightsList" not in payload:
        raise SourceError("itaka: flights payload missing flightsList")
    return payload


def _leg_airports(leg: dict[str, Any]) -> tuple[str, str, date | None, time | None, time | None, dict[str, Any]]:
    dep = leg.get("departure") or {}
    arr = leg.get("arrival") or {}
    if not isinstance(dep, dict):
        dep = {}
    if not isinstance(arr, dict):
        arr = {}
    origin = str(dep.get("airportCode") or dep.get("airportIata") or "").upper()
    dest = str(arr.get("airportCode") or arr.get("airportIata") or "").upper()
    day = _parse_iso_date(dep.get("dateIso") or leg.get("dateIso"))
    dep_t = _parse_hhmm(dep.get("time"))
    arr_t = _parse_hhmm(arr.get("time"))
    return origin, dest, day, dep_t, arr_t, dep


def _duration_minutes(leg: dict[str, Any]) -> int | None:
    duration = leg.get("duration")
    if isinstance(duration, dict):
        try:
            hours = int(duration.get("hours") or 0)
            mins = int(duration.get("minutes") or 0)
            return hours * 60 + mins
        except (TypeError, ValueError):
            return None
    return None


def _bag_note(leg: dict[str, Any]) -> tuple[bool | None, str]:
    """Return (cabin_bag_included, notes).

    ``luggage.included`` is a generic baggage flag (often checked) — do not treat
    it as cabin/hand baggage. Only ``handWeight`` / cabin-specific fields count.
    """
    luggage = leg.get("luggage")
    if not isinstance(luggage, dict):
        return None, ""
    details = luggage.get("details") or {}
    bits: list[str] = []
    cabin_included: bool | None = None
    if isinstance(details, dict):
        hand = details.get("handWeight")
        registered = details.get("registeredWeight")
        if hand is not None:
            bits.append(f"cabin bag {hand} kg included")
            cabin_included = True
        if registered is not None:
            bits.append(f"checked bag {registered} kg included")
    # Generic "included" is not cabin evidence — note only when no weights given.
    if not bits and luggage.get("included") is True:
        bits.append("baggage included")
    return cabin_included, "; ".join(bits)


def offers_from_payload(
    payload: dict[str, Any],
    *,
    query: SearchQuery,
    fx: Any,
) -> list[Offer]:
    flights = payload.get("flightsList") or []
    if not isinstance(flights, list):
        raise SourceError("itaka: flightsList is not a list")

    wanted_dests = {d.upper() for d in query.all_destinations} or set(_PL_AIRPORTS)
    out: list[Offer] = []

    for row in flights:
        if not isinstance(row, dict):
            continue
        leg = row.get("departureFlight") or {}
        if not isinstance(leg, dict):
            continue
        origin, dest, day, dep_t, arr_t, dep_node = _leg_airports(leg)
        if origin not in _TH_AIRPORTS:
            continue
        if dest not in _PL_AIRPORTS:
            continue
        if wanted_dests and dest not in wanted_dests:
            continue
        if day is None or day < query.date_from or day > query.date_to:
            continue
        price_pln = _parse_price_pln(row.get("pricePerPerson") or row.get("flightPrice"))
        if price_pln is None:
            continue
        currency = str(row.get("currency") or "PLN").upper()
        carrier_info = dep_node.get("flightCarrierInfo")
        airline = ""
        if isinstance(carrier_info, dict):
            airline = str(carrier_info.get("name") or "").strip()
        flight_no = str(
            dep_node.get("flightNumber") or leg.get("flightNumber") or ""
        ).strip().upper().replace(" ", "")
        depart_dt = _combine(day, dep_t)
        arrive_day = _parse_iso_date((leg.get("arrival") or {}).get("dateIso")) or day
        arrive_dt = _combine(arrive_day, arr_t)
        if depart_dt and arrive_dt and arrive_dt <= depart_dt:
            arrive_dt = arrive_dt + timedelta(days=1)
        cabin_included, bag_bits = _bag_note(leg)
        notes = [CHARTER_NOTE]
        if bag_bits:
            notes.append(bag_bits)
        link = row.get("url")
        if isinstance(link, str) and link.startswith("/"):
            link = "https://www.itaka.pl" + link
        stops = leg.get("numberOfStops")
        try:
            stops_i = int(stops) if stops is not None else 0
        except (TypeError, ValueError):
            stops_i = 0
        out.append(
            Offer(
                source="itaka",
                origin=origin,
                destination=dest,
                depart_date=day,
                price_usd=float(fx.to_usd(price_pln, currency)),
                price_original=price_pln,
                currency_original=currency,
                depart_time=depart_dt,
                arrive_time=arrive_dt,
                airlines=[airline] if airline else [],
                flight_numbers=[flight_no] if flight_no else [],
                stops=stops_i,
                duration_minutes=_duration_minutes(leg),
                link=link if isinstance(link, str) else _BASE,
                cabin_bag_included=cabin_included,
                notes="; ".join(notes),
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


def _list_url(query: SearchQuery, page: int) -> str:
    params = {
        "flightType": "oneWayTicket",
        "adultsNumber": str(query.adults or 1),
        "dateFrom": query.date_from.isoformat(),
        "dateTo": query.date_to.isoformat(),
        "page": str(page),
    }
    return f"{_BASE}?{urlencode(params)}"


class ItakaSource(Source):
    name: ClassVar[str] = "itaka"

    async def search(self, query: SearchQuery, ctx: RunContext) -> list[Offer]:
        offers: list[Offer] = []
        pages_total = 1
        page = 1
        while page <= min(pages_total, _MAX_PAGES):
            payload = await self._fetch_page(query, ctx, page=page)
            pages_raw = payload.get("pagesCount")
            try:
                pages_total = max(1, int(pages_raw or 1))
            except (TypeError, ValueError):
                pages_total = 1
            offers.extend(offers_from_payload(payload, query=query, fx=ctx.fx))
            flights = payload.get("flightsList") or []
            if not flights:
                break
            page += 1
        return _dedupe_cap(offers)

    async def _fetch_page(
        self, query: SearchQuery, ctx: RunContext, *, page: int
    ) -> dict[str, Any]:
        url = _list_url(query, page)
        try:
            resp = await ctx.http.get(url, headers=_headers(), timeout=_TIMEOUT)
        except httpx.HTTPError as exc:
            raise SourceError(f"itaka: request failed (page {page}): {exc}") from exc
        body = resp.text
        if _looks_challenged(resp.status_code, body):
            raise SourceError(
                f"itaka: blocked or challenged (HTTP {resp.status_code})"
            )
        if resp.status_code >= 400:
            raise SourceError(f"itaka: HTTP {resp.status_code} on page {page}")
        return extract_flights_payload(body)
