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
_API = "https://r.pl/api/czartery/wyszukiwanie/v4.1/wyszukaj"
_REFERER = "https://r.pl/bilety-czarterowe"
_DEST_BASE = "https://r.pl/bilety-czarterowe/tajlandia"
_TIMEOUT = 30.0
_MAX_PER_OD = 20
_ADULT_DOB = "1990-01-01"

# Destination SSR pages expose TH→PL as przyloty. Search API currently lists BKK only.
_TH_REGIONS: tuple[tuple[str, str], ...] = (
    ("BKK", "bangkok"),
    ("HKT", "phuket"),
)
_PL_DEFAULT = ("WAW", "KRK", "GDN", "KTW", "WRO", "POZ", "WMI")

_NUXT_RE = re.compile(
    r'<script[^>]*\bid=["\']__NUXT_DATA__["\'][^>]*>(.*?)</script>',
    re.IGNORECASE | re.DOTALL,
)
_CHALLENGE_MARKERS = (
    "just a moment",
    "cf-challenge",
    "cf-turnstile",
    "attention required",
)


def _headers_json() -> dict[str, str]:
    return {
        "User-Agent": _UA,
        "Accept": "application/json",
        "Accept-Language": "pl-PL,pl;q=0.9,en;q=0.8",
        "Referer": _REFERER,
    }


def _headers_html() -> dict[str, str]:
    return {
        "User-Agent": _UA,
        "Accept": "text/html,application/xhtml+xml",
        "Accept-Language": "pl-PL,pl;q=0.9,en;q=0.8",
        "Referer": _REFERER,
    }


def _looks_challenged(status: int, body: str) -> bool:
    if status in {401, 403, 429, 503}:
        return True
    low = body[:4000].lower()
    return any(marker in low for marker in _CHALLENGE_MARKERS)


def _parse_iso_date(value: Any) -> date | None:
    if value is None:
        return None
    text = str(value).strip()
    if not text:
        return None
    if "T" in text:
        text = text.split("T", 1)[0]
    try:
        return date.fromisoformat(text[:10])
    except ValueError:
        return None


def _parse_hhmm(value: Any) -> time | None:
    if value is None:
        return None
    text = str(value).strip()
    if not text:
        return None
    parts = text.split(":")
    try:
        hour = int(parts[0])
        minute = int(parts[1]) if len(parts) > 1 else 0
        return time(hour, minute)
    except (TypeError, ValueError, IndexError):
        return None


def _combine(day: date, clock: time | None) -> datetime | None:
    if clock is None:
        return None
    return datetime(day.year, day.month, day.day, clock.hour, clock.minute)


def _parse_duration_minutes(value: Any) -> int | None:
    if value is None:
        return None
    text = str(value).strip()
    if ":" not in text:
        return None
    try:
        hours_s, mins_s = text.split(":", 1)
        return int(hours_s) * 60 + int(mins_s)
    except ValueError:
        return None


def _flight_number(carrier: str | None, number: str | None) -> str | None:
    code = (carrier or "").strip().upper()
    num = (number or "").strip().upper().replace(" ", "")
    if not num and not code:
        return None
    if not num:
        return code or None
    # NumerLotu is often already a marketing code (e.g. WFL7610).
    if any(ch.isalpha() for ch in num):
        return num
    if code:
        return f"{code}{num}"
    return num


def resolve_nuxt(
    data: list[Any],
    idx: Any,
    *,
    depth: int = 0,
    seen: set[int] | None = None,
) -> Any:
    """Resolve a Vue/Nuxt payload index into a plain Python value."""
    if seen is None:
        seen = set()
    if not isinstance(idx, int) or not (0 <= idx < len(data)) or depth > 24:
        return idx
    if idx in seen:
        return None
    seen.add(idx)
    value = data[idx]
    if isinstance(value, list) and len(value) == 2 and value[0] in {
        "ShallowReactive",
        "Reactive",
        "Ref",
        "EmptyRef",
    }:
        return resolve_nuxt(data, value[1], depth=depth + 1, seen=seen)
    if isinstance(value, dict):
        return {
            key: resolve_nuxt(data, val, depth=depth + 1, seen=set(seen))
            if isinstance(val, int)
            else val
            for key, val in value.items()
        }
    if isinstance(value, list) and value and all(isinstance(x, int) for x in value):
        return [
            resolve_nuxt(data, item, depth=depth + 1, seen=set(seen))
            for item in value
        ]
    return value


def extract_nuxt_payload(html: str) -> list[Any]:
    match = _NUXT_RE.search(html)
    if not match:
        raise SourceError(
            "rpl: destination page missing __NUXT_DATA__ (layout changed?)"
        )
    try:
        data = json.loads(match.group(1))
    except json.JSONDecodeError as exc:
        raise SourceError(f"rpl: invalid __NUXT_DATA__ JSON: {exc}") from exc
    if not isinstance(data, list) or len(data) < 4:
        raise SourceError("rpl: unexpected __NUXT_DATA__ shape")
    return data


def extract_przyloty(html: str) -> list[dict[str, Any]]:
    data = extract_nuxt_payload(html)
    keys = data[3]
    if not isinstance(keys, dict) or "przyloty" not in keys:
        raise SourceError(
            "rpl: __NUXT_DATA__ missing przyloty key (layout changed?)"
        )
    resolved = resolve_nuxt(data, keys["przyloty"])
    if resolved is None:
        return []
    if not isinstance(resolved, list):
        raise SourceError("rpl: przyloty is not a list")
    return [row for row in resolved if isinstance(row, dict)]


def _bag_note(ticket: dict[str, Any]) -> str:
    bits: list[str] = []
    cabin = ticket.get("BagazPodreczny")
    checked = ticket.get("BagazRejestrowany")
    if cabin is not None:
        bits.append(f"cabin bag {cabin} kg included")
    if checked is not None:
        bits.append(f"checked bag {checked} kg included")
    return "; ".join(bits)


def _dest_link(
    region_slug: str,
    *,
    day: date | None = None,
    ticket_id: str | None = None,
) -> str:
    params: dict[str, str] = {"oneWay": "true"}
    if day is not None:
        params["data"] = day.isoformat()
    if ticket_id:
        params["idPrzylot"] = ticket_id
    return f"{_DEST_BASE}/{region_slug}?{urlencode(params)}"


def offers_from_search(
    payload: dict[str, Any],
    *,
    query: SearchQuery,
    fx: Any,
    origin: str = "BKK",
) -> list[Offer]:
    if "Destynacje" not in payload:
        raise SourceError("rpl: search JSON missing Destynacje key")
    dests = payload.get("Destynacje") or []
    if not isinstance(dests, list):
        raise SourceError("rpl: Destynacje is not a list")

    wanted = {code.upper() for code in query.all_destinations} or set(_PL_DEFAULT)
    out: list[Offer] = []

    for row in dests:
        if not isinstance(row, dict):
            continue
        dest = str(row.get("Klucz") or "").upper()
        if not dest or dest not in wanted:
            continue
        day = _parse_iso_date(row.get("TerminWyjazdu"))
        if day is None or day < query.date_from or day > query.date_to:
            continue
        price = row.get("Cena")
        if price is None:
            continue
        try:
            price_pln = float(price)
        except (TypeError, ValueError):
            continue
        brand = ""
        layer = row.get("DataLayer")
        if isinstance(layer, dict):
            brand = str(layer.get("brand") or "").strip()
        # Search API "Bagaz" is checked-bag allowance (kg), not cabin/hand baggage.
        checked = row.get("Bagaz")
        notes = [CHARTER_NOTE]
        if checked is not None:
            notes.append(f"checked bag {checked} kg included")
        region = "bangkok" if origin == "BKK" else "phuket"
        out.append(
            Offer(
                source="rpl",
                origin=origin,
                destination=dest,
                depart_date=day,
                price_usd=float(fx.to_usd(price_pln, "PLN")),
                price_original=price_pln,
                currency_original="PLN",
                airlines=[brand] if brand else [],
                flight_numbers=[],
                stops=0,
                link=_dest_link(region, day=day),
                cabin_bag_included=None,
                notes="; ".join(notes),
            )
        )
    return out


def offers_from_przyloty(
    days: list[dict[str, Any]],
    *,
    query: SearchQuery,
    fx: Any,
    default_origin: str,
    region_slug: str,
) -> list[Offer]:
    wanted = {code.upper() for code in query.all_destinations} or set(_PL_DEFAULT)
    out: list[Offer] = []

    for day_row in days:
        day = _parse_iso_date(day_row.get("Data"))
        if day is None or day < query.date_from or day > query.date_to:
            continue
        tickets = day_row.get("Bilety") or []
        if not isinstance(tickets, list):
            continue
        for ticket in tickets:
            if not isinstance(ticket, dict):
                continue
            wylot = ticket.get("Wylot") or {}
            przylot = ticket.get("Przylot") or {}
            if not isinstance(wylot, dict) or not isinstance(przylot, dict):
                continue
            origin = str(wylot.get("Iata") or default_origin).upper()
            dest = str(przylot.get("Iata") or "").upper()
            if not dest or dest not in wanted:
                continue
            price = ticket.get("Cena")
            if price is None:
                price = day_row.get("Cena")
            if price is None:
                continue
            try:
                price_pln = float(price)
            except (TypeError, ValueError):
                continue
            carrier_code = str(ticket.get("KodPrzewoznikaIata") or "").strip()
            carrier_name = str(ticket.get("NazwaPrzewoznika") or "").strip()
            number = str(ticket.get("NumerLotu") or "").strip()
            flight_no = _flight_number(carrier_code, number)
            depart_dt = _combine(day, _parse_hhmm(wylot.get("Godzina")))
            arrive_dt = _combine(day, _parse_hhmm(przylot.get("Godzina")))
            if depart_dt and arrive_dt and arrive_dt <= depart_dt:
                arrive_dt = arrive_dt + timedelta(days=1)
            bag_bits = _bag_note(ticket)
            notes = [CHARTER_NOTE]
            if bag_bits:
                notes.append(bag_bits)
            ticket_id = ticket.get("Id") or ticket.get("BindId")
            # BagazPodreczny = hand/cabin; BagazRejestrowany = checked (notes only).
            cabin = ticket.get("BagazPodreczny")
            out.append(
                Offer(
                    source="rpl",
                    origin=origin,
                    destination=dest,
                    depart_date=day,
                    price_usd=float(fx.to_usd(price_pln, "PLN")),
                    price_original=price_pln,
                    currency_original="PLN",
                    depart_time=depart_dt,
                    arrive_time=arrive_dt,
                    airlines=(
                        [carrier_name or carrier_code]
                        if (carrier_name or carrier_code)
                        else []
                    ),
                    flight_numbers=[flight_no] if flight_no else [],
                    stops=0,
                    duration_minutes=_parse_duration_minutes(
                        ticket.get("CzasLotu")
                    ),
                    link=_dest_link(
                        region_slug,
                        day=day,
                        ticket_id=str(ticket_id) if ticket_id else None,
                    ),
                    cabin_bag_included=True if cabin is not None else None,
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


class RplSource(Source):
    name: ClassVar[str] = "rpl"

    async def search(self, query: SearchQuery, ctx: RunContext) -> list[Offer]:
        pl_dests = [d.upper() for d in query.all_destinations] or list(_PL_DEFAULT)
        # 1 API search (BKK) + 2 destination SSR pages ≈ 3 requests.
        offers = await self._search_api(
            query, ctx, origin="BKK", destinations=pl_dests
        )
        for origin, slug in _TH_REGIONS:
            offers.extend(
                await self._search_destination(
                    query, ctx, origin=origin, slug=slug
                )
            )
        return _dedupe_cap(offers)

    async def _search_api(
        self,
        query: SearchQuery,
        ctx: RunContext,
        *,
        origin: str,
        destinations: list[str],
    ) -> list[Offer]:
        params: list[tuple[str, str]] = [
            ("oneWay", "true"),
            ("dataUrodzenia", _ADULT_DOB),
            ("sortowanie", "cena"),
            ("iataSkad", origin),
            ("dataWylotuMin", query.date_from.isoformat()),
            ("dataWylotuMax", query.date_to.isoformat()),
        ]
        for dest in destinations:
            params.append(("iataDokad", dest))
        try:
            resp = await ctx.http.get(
                _API,
                params=params,
                headers=_headers_json(),
                timeout=_TIMEOUT,
            )
        except httpx.HTTPError as exc:
            raise SourceError(f"rpl: search request failed: {exc}") from exc

        body = resp.text
        if _looks_challenged(resp.status_code, body):
            raise SourceError(
                f"rpl: blocked or challenged (HTTP {resp.status_code})"
            )
        if resp.status_code >= 400:
            raise SourceError(f"rpl: search HTTP {resp.status_code}")
        try:
            payload = resp.json()
        except json.JSONDecodeError as exc:
            raise SourceError(f"rpl: search response is not JSON: {exc}") from exc
        if not isinstance(payload, dict):
            raise SourceError("rpl: search JSON root is not an object")
        return offers_from_search(
            payload, query=query, fx=ctx.fx, origin=origin
        )

    async def _search_destination(
        self,
        query: SearchQuery,
        ctx: RunContext,
        *,
        origin: str,
        slug: str,
    ) -> list[Offer]:
        url = f"{_DEST_BASE}/{slug}"
        try:
            resp = await ctx.http.get(
                url, headers=_headers_html(), timeout=_TIMEOUT
            )
        except httpx.HTTPError as exc:
            raise SourceError(
                f"rpl: destination request failed ({slug}): {exc}"
            ) from exc
        body = resp.text
        if _looks_challenged(resp.status_code, body):
            raise SourceError(
                f"rpl: destination blocked ({slug}, HTTP {resp.status_code})"
            )
        if resp.status_code >= 400:
            raise SourceError(
                f"rpl: destination HTTP {resp.status_code} for {slug}"
            )
        days = extract_przyloty(body)
        return offers_from_przyloty(
            days,
            query=query,
            fx=ctx.fx,
            default_origin=origin,
            region_slug=slug,
        )
