from __future__ import annotations

import asyncio
import re
from dataclasses import dataclass
from datetime import date, datetime
from typing import Any, ClassVar
from urllib.parse import urljoin

from flightsearch.context import RunContext
from flightsearch.mcp_client import McpSession
from flightsearch.models import Offer, SearchQuery
from flightsearch.sources.base import Source, SourceError

SKIPLAGGED_MCP_URL = "https://mcp.skiplagged.com/mcp"
_CALENDAR_TOOL = "sk_flex_departure_calendar"
_SEARCH_TOOL = "sk_flights_search"
_CONCURRENCY = 2
_TIMEOUT = 90.0
_RETRIES = 2
_MAX_PER_OD = 20
_DEFAULT_MAX_DETAIL = 12
_BASE_URL = "https://skiplagged.com"
_CALL_SPACING_S = 1.25

_PRICE_RE = re.compile(
    r"^\|\s*(\d{4}-\d{2}-\d{2})\s*\|\s*[^|]*\|\s*\$?\s*([\d,]+(?:\.\d+)?)\s*\|"
)
_LINK_RE = re.compile(r"\[([^\]]*)\]\((https?://[^)]+)\)")
_FLIGHT_ROW_RE = re.compile(
    r"^\|\s*\$?\s*([\d,]+(?:\.\d+)?)\s*"
    r"\|\s*([^|]*?)\s*"
    r"\|\s*([^|]*?)\s*"
    r"\|\s*([^|]*?)\s*"
    r"\|\s*([^|]*?)\s*"
    r"\|\s*(.*?)\s*"
    r"\|\s*(.*?)\s*\|?\s*$"
)
_LEG_RE = re.compile(
    r"([A-Z]{3})\s*→\s*([A-Z]{3})\s*\("
    r"([^→)]+?)\s*→\s*([^)]+?)\)"
)
_DURATION_RE = re.compile(
    r"(?:(\d+)\s*h(?:ours?)?)?\s*(?:(\d+)\s*m(?:in(?:utes?)?)?)?",
    re.IGNORECASE,
)
_STOPS_RE = re.compile(r"(\d+)\s*stops?", re.IGNORECASE)
_TRIP_RE = re.compile(r"#trip=([A-Za-z0-9\-]+)")


@dataclass(frozen=True)
class CalendarCell:
    origin: str
    destination: str
    depart_date: date
    price_usd: float
    price_original: float
    currency: str
    search_url: str | None = None


def _parse_local_dt(value: str | None) -> datetime | None:
    if not value:
        return None
    text = str(value).strip()
    if text.endswith("Z"):
        text = text[:-1]
    if "+" in text[10:] or (text.count("-") > 2 and "T" in text):
        for sep in ("+", "-"):
            idx = text.find(sep, 10)
            if idx != -1:
                text = text[:idx]
                break
    try:
        return datetime.fromisoformat(text)
    except ValueError:
        return None


def _absolute_url(link: str | None) -> str | None:
    if not link:
        return None
    text = str(link).strip()
    if not text:
        return None
    if text.startswith("http://") or text.startswith("https://"):
        return text
    return urljoin(_BASE_URL, text)


def _split_names(value: Any) -> list[str]:
    if value is None:
        return []
    if isinstance(value, list):
        out: list[str] = []
        for item in value:
            out.extend(_split_names(item))
        return out
    text = str(value).strip()
    if not text:
        return []
    parts = re.split(r"\s*,\s*", text)
    return [p for p in parts if p]


def parse_calendar_markdown(
    text: str,
    *,
    date_from: date,
    date_to: date,
) -> tuple[list[tuple[date, float]], str | None]:
    """Parse flex-calendar markdown tables into (date, price) rows."""
    cells: list[tuple[date, float]] = []
    seen: set[date] = set()
    for line in (text or "").splitlines():
        m = _PRICE_RE.match(line.strip())
        if not m:
            continue
        try:
            d = date.fromisoformat(m.group(1))
            price = float(m.group(2).replace(",", ""))
        except ValueError:
            continue
        if d < date_from or d > date_to:
            continue
        if d in seen:
            continue
        seen.add(d)
        cells.append((d, price))
    link: str | None = None
    for m in _LINK_RE.finditer(text or ""):
        link = m.group(2)
        break
    cells.sort(key=lambda x: x[0])
    return cells, link


def parse_calendar_response(
    data: Any,
    *,
    origin: str,
    destination: str,
    date_from: date,
    date_to: date,
    fx: Any,
) -> list[CalendarCell]:
    text = ""
    search_url: str | None = None
    if isinstance(data, str):
        text = data
    elif isinstance(data, dict):
        if isinstance(data.get("text"), str):
            text = data["text"]
        search_url = data.get("searchUrl") or data.get("url")
        # Structured day entries if the API ever returns JSON.
        for key in ("days", "dates", "calendar", "fares", "results"):
            rows = data.get(key)
            if not isinstance(rows, list):
                continue
            cells: list[CalendarCell] = []
            for row in rows:
                if not isinstance(row, dict):
                    continue
                raw_date = row.get("date") or row.get("departure") or row.get("departureDate")
                raw_price = row.get("price")
                amount: float | None = None
                currency = "USD"
                if isinstance(raw_price, dict):
                    try:
                        amount = float(raw_price.get("amount"))
                    except (TypeError, ValueError):
                        amount = None
                    currency = str(raw_price.get("currency") or "USD").upper()
                else:
                    try:
                        amount = float(raw_price)
                    except (TypeError, ValueError):
                        amount = None
                if raw_date is None or amount is None:
                    continue
                try:
                    d = date.fromisoformat(str(raw_date)[:10])
                except ValueError:
                    continue
                if d < date_from or d > date_to:
                    continue
                cells.append(
                    CalendarCell(
                        origin=origin.upper(),
                        destination=destination.upper(),
                        depart_date=d,
                        price_usd=float(fx.to_usd(amount, currency)),
                        price_original=amount,
                        currency=currency,
                        search_url=_absolute_url(search_url),
                    )
                )
            if cells:
                return cells

    parsed, md_link = parse_calendar_markdown(text, date_from=date_from, date_to=date_to)
    url = _absolute_url(search_url or md_link)
    return [
        CalendarCell(
            origin=origin.upper(),
            destination=destination.upper(),
            depart_date=d,
            price_usd=float(fx.to_usd(price, "USD")),
            price_original=price,
            currency="USD",
            search_url=url,
        )
        for d, price in parsed
    ]


def select_detail_cells(
    cells: list[CalendarCell],
    *,
    max_detail: int,
    near_miss_usd: float,
) -> list[CalendarCell]:
    """Pick up to max_detail cheapest cells with price ≤ near_miss_usd."""
    eligible = [c for c in cells if c.price_usd <= near_miss_usd]
    eligible.sort(key=lambda c: (c.price_usd, c.depart_date.isoformat(), c.origin, c.destination))
    # Deduplicate by (origin, dest, date) keeping cheapest.
    picked: list[CalendarCell] = []
    seen: set[tuple[str, str, date]] = set()
    for cell in eligible:
        key = (cell.origin, cell.destination, cell.depart_date)
        if key in seen:
            continue
        seen.add(key)
        picked.append(cell)
        if len(picked) >= max_detail:
            break
    return picked


def sample_fallback_cells(
    *,
    origins: list[str],
    destinations: list[str],
    dates: list[date],
    max_detail: int,
) -> list[CalendarCell]:
    """Evenly sample (O, D, date) when the calendar phase fails entirely."""
    if not origins or not destinations or not dates or max_detail <= 0:
        return []
    # Prefer a spread of dates rather than only the first day.
    if len(dates) <= max_detail:
        date_sample = list(dates)
    else:
        step = max(1, len(dates) / max_detail)
        idxs = sorted({min(len(dates) - 1, int(i * step)) for i in range(max_detail)})
        date_sample = [dates[i] for i in idxs]

    cells: list[CalendarCell] = []
    # Round-robin origins/dests across sampled dates.
    pairs = [(o, d) for o in origins for d in destinations]
    for i, dep in enumerate(date_sample):
        origin, dest = pairs[i % len(pairs)]
        cells.append(
            CalendarCell(
                origin=origin.upper(),
                destination=dest.upper(),
                depart_date=dep,
                price_usd=0.0,
                price_original=0.0,
                currency="USD",
            )
        )
        if len(cells) >= max_detail:
            break
    # Fill remaining slots with more OD pairs on the middle date.
    mid = dates[len(dates) // 2]
    for origin, dest in pairs:
        if len(cells) >= max_detail:
            break
        key = (origin.upper(), dest.upper(), mid)
        if any((c.origin, c.destination, c.depart_date) == key for c in cells):
            continue
        cells.append(
            CalendarCell(
                origin=origin.upper(),
                destination=dest.upper(),
                depart_date=mid,
                price_usd=0.0,
                price_original=0.0,
                currency="USD",
            )
        )
    return cells[:max_detail]


def _flight_airlines(flight: dict[str, Any]) -> list[str]:
    for key in ("airlines", "airline", "carriers", "operatingAirlines"):
        names = _split_names(flight.get(key))
        if names:
            return names
    legs = flight.get("legs") or flight.get("segments") or flight.get("flights") or []
    out: list[str] = []
    if isinstance(legs, list):
        for leg in legs:
            if not isinstance(leg, dict):
                continue
            for key in ("airline", "airlineName", "carrier", "marketingAirline"):
                for name in _split_names(leg.get(key)):
                    if name not in out:
                        out.append(name)
    return out


def _flight_numbers(flight: dict[str, Any]) -> list[str]:
    for key in ("flightNumbers", "flight_numbers", "flightNumber"):
        nums = _split_names(flight.get(key))
        if nums:
            return [n.upper().replace(" ", "") for n in nums]
    legs = flight.get("legs") or flight.get("segments") or flight.get("flights") or []
    out: list[str] = []
    if isinstance(legs, list):
        for leg in legs:
            if not isinstance(leg, dict):
                continue
            fn = leg.get("flightNumber") or leg.get("flight_number") or leg.get("number")
            carrier = (
                leg.get("airlineCode")
                or leg.get("carrier")
                or leg.get("marketingAirlineCode")
                or ""
            )
            if fn is None:
                continue
            text = str(fn).strip().upper().replace(" ", "")
            code = str(carrier).strip().upper()
            if code and not text.startswith(code):
                text = f"{code}{text}"
            if text and text not in out:
                out.append(text)
    return out


def _endpoint(flight: dict[str, Any], side: str) -> tuple[str | None, datetime | None]:
    block = flight.get(side) or flight.get(f"{side}Airport") or {}
    airport: str | None = None
    when: datetime | None = None
    if isinstance(block, dict):
        airport = block.get("airport") or block.get("code") or block.get("iata")
        when = _parse_local_dt(block.get("dateTime") or block.get("time") or block.get("at"))
    if not airport:
        airport = flight.get(f"{side}Airport") or flight.get(
            "from" if side == "departure" else "to"
        )
    if when is None:
        when = _parse_local_dt(
            flight.get(f"{side}Time")
            or flight.get(f"{side}DateTime")
            or flight.get("fromDateTime" if side == "departure" else "toDateTime")
        )
    return (str(airport).upper() if airport else None), when


def _stops(flight: dict[str, Any]) -> int | None:
    layovers = flight.get("layovers")
    if isinstance(layovers, int):
        return layovers
    if isinstance(layovers, list):
        return len(layovers)
    stops = flight.get("stops")
    if isinstance(stops, int):
        return stops
    attrs = {str(a).lower() for a in (flight.get("attributes") or [])}
    if "nonstop" in attrs or "non-stop" in attrs:
        return 0
    if "one-stop" in attrs or "1-stop" in attrs:
        return 1
    return None


def _duration_minutes(flight: dict[str, Any]) -> int | None:
    for key in ("durationMinutes", "duration_minutes", "totalDurationMinutes"):
        val = flight.get(key)
        if val is not None:
            try:
                return int(val)
            except (TypeError, ValueError):
                pass
    duration = flight.get("duration")
    if isinstance(duration, (int, float)):
        # Heuristic: values > 1000 are likely seconds.
        return int(duration // 60) if duration > 1000 else int(duration)
    if isinstance(duration, dict):
        mins = duration.get("minutes") or duration.get("totalMinutes")
        if mins is not None:
            try:
                return int(mins)
            except (TypeError, ValueError):
                return None
    return None


def _self_transfer(flight: dict[str, Any]) -> bool | None:
    attrs = {str(a).lower() for a in (flight.get("attributes") or [])}
    if "virtual-interline" in attrs or "virtual_interline" in attrs:
        return True
    if "self-transfer" in attrs or "self_transfer" in attrs:
        return True
    if "standard" in attrs or "nonstop" in attrs:
        return False
    flag = flight.get("isVirtualInterline") or flight.get("virtualInterline")
    if isinstance(flag, bool):
        return flag
    return None


def _parse_duration_text(text: str) -> int | None:
    m = _DURATION_RE.search((text or "").strip())
    if not m or (m.group(1) is None and m.group(2) is None):
        return None
    hours = int(m.group(1) or 0)
    mins = int(m.group(2) or 0)
    return hours * 60 + mins


def _parse_stops_text(text: str) -> int | None:
    raw = (text or "").strip().lower()
    if not raw or raw in {"—", "-", "–"}:
        return None
    if "nonstop" in raw or "non-stop" in raw or raw == "direct":
        return 0
    m = _STOPS_RE.search(raw)
    if m:
        return int(m.group(1))
    return None


def _legs_from_segments(segments_html: str) -> list[dict[str, Any]]:
    text = (segments_html or "").replace("<br/>", "\n").replace("<br>", "\n")
    legs: list[dict[str, Any]] = []
    for m in _LEG_RE.finditer(text):
        legs.append(
            {
                "from": m.group(1),
                "to": m.group(2),
                "departure": _parse_local_dt(m.group(3).strip()),
                "arrival": _parse_local_dt(m.group(4).strip()),
            }
        )
    return legs


def _flight_numbers_from_link(link: str | None) -> list[str]:
    if not link:
        return []
    m = _TRIP_RE.search(link)
    if not m:
        return []
    return [p for p in m.group(1).split("-") if p]


def _airport_change_self_transfer(legs: list[dict[str, Any]]) -> bool:
    for i in range(len(legs) - 1):
        arrive = str(legs[i].get("to") or "").upper()
        depart = str(legs[i + 1].get("from") or "").upper()
        if arrive and depart and arrive != depart:
            return True
    return False


def parse_flights_markdown(
    text: str,
    *,
    requested_origin: str,
    requested_destination: str,
    depart_date: date,
    fx: Any,
    source: str = "skiplagged",
) -> list[Offer]:
    """Parse Skiplagged MCP markdown flight tables into offers."""
    offers: list[Offer] = []
    req_origin = requested_origin.upper()
    req_dest = requested_destination.upper()
    for line in (text or "").splitlines():
        stripped = line.strip()
        if not stripped.startswith("|"):
            continue
        if "Price" in stripped and "Duration" in stripped:
            continue
        if re.match(r"^\|\s*-+", stripped):
            continue
        m = _FLIGHT_ROW_RE.match(stripped)
        if not m:
            continue
        try:
            amount = float(m.group(1).replace(",", ""))
        except ValueError:
            continue
        duration_txt = m.group(2).strip()
        stops_txt = m.group(3).strip()
        type_txt = m.group(4).strip()
        airlines_txt = m.group(5).strip()
        segments_txt = m.group(6).strip()
        booking_txt = m.group(7).strip()

        link = None
        for lm in _LINK_RE.finditer(booking_txt):
            link = lm.group(2)
            break
        link = _absolute_url(link)

        legs = _legs_from_segments(segments_txt)
        origin = str(legs[0]["from"]) if legs else req_origin
        actual_dest = str(legs[-1]["to"]) if legs else req_dest
        depart_time = legs[0]["departure"] if legs else None
        arrive_time = legs[-1]["arrival"] if legs else None

        type_l = type_txt.lower()
        self_xfer: bool | None = None
        if "virtual" in type_l:
            self_xfer = True
        elif _airport_change_self_transfer(legs):
            self_xfer = True
        elif type_l in {"standard", "nonstop", "non-stop"}:
            self_xfer = False

        notes: list[str] = []
        hidden = "hidden" in type_l or actual_dest != req_dest
        if hidden and actual_dest != req_dest:
            # Keep real final airport; flag the risk.
            notes.append("hidden-city")
            notes.append(f"searched {req_dest}")
        elif "hidden" in type_l:
            notes.append("hidden-city")
        if self_xfer:
            notes.append("virtual-interline")

        dep_date = depart_time.date() if isinstance(depart_time, datetime) else depart_date
        offers.append(
            Offer(
                source=source,
                origin=origin,
                destination=actual_dest,
                depart_date=dep_date,
                price_usd=float(fx.to_usd(amount, "USD")),
                price_original=amount,
                currency_original="USD",
                depart_time=depart_time if isinstance(depart_time, datetime) else None,
                arrive_time=arrive_time if isinstance(arrive_time, datetime) else None,
                airlines=_split_names(airlines_txt),
                flight_numbers=_flight_numbers_from_link(link),
                stops=_parse_stops_text(stops_txt),
                duration_minutes=_parse_duration_text(duration_txt),
                link=link,
                cabin_bag_included=None,
                self_transfer=self_xfer,
                notes="; ".join(notes),
            )
        )
    offers.sort(key=lambda o: (o.price_usd, o.depart_date.isoformat()))
    return offers


def flight_to_offer(
    flight: dict[str, Any],
    *,
    requested_origin: str,
    requested_destination: str,
    depart_date: date,
    fx: Any,
    source: str = "skiplagged",
) -> Offer | None:
    price_block = flight.get("price") or {}
    if isinstance(price_block, dict):
        try:
            amount = float(price_block.get("amount"))
        except (TypeError, ValueError):
            return None
        currency = str(price_block.get("currency") or "USD").upper()
    else:
        try:
            amount = float(price_block)
        except (TypeError, ValueError):
            return None
        currency = "USD"

    origin, depart_time = _endpoint(flight, "departure")
    destination, arrive_time = _endpoint(flight, "arrival")
    origin = origin or requested_origin.upper()
    actual_dest = destination or requested_destination.upper()
    req_dest = requested_destination.upper()

    notes: list[str] = []
    if actual_dest != req_dest:
        # Keep the real final airport; flag hidden-city risk explicitly.
        notes.append("hidden-city")
        notes.append(f"searched {req_dest}")

    self_xfer = _self_transfer(flight)
    if self_xfer:
        notes.append("virtual-interline")

    dep_date = depart_time.date() if depart_time else depart_date
    return Offer(
        source=source,
        origin=origin,
        destination=actual_dest,
        depart_date=dep_date,
        price_usd=float(fx.to_usd(amount, currency)),
        price_original=amount,
        currency_original=currency,
        depart_time=depart_time,
        arrive_time=arrive_time,
        airlines=_flight_airlines(flight),
        flight_numbers=_flight_numbers(flight),
        stops=_stops(flight),
        duration_minutes=_duration_minutes(flight),
        link=_absolute_url(flight.get("deepLink") or flight.get("deeplink") or flight.get("url")),
        cabin_bag_included=None,
        self_transfer=self_xfer,
        notes="; ".join(notes),
    )


def parse_flights_response(
    data: Any,
    *,
    requested_origin: str,
    requested_destination: str,
    depart_date: date,
    fx: Any,
    source: str = "skiplagged",
) -> list[Offer]:
    text = ""
    if isinstance(data, str):
        text = data
    elif isinstance(data, dict):
        if isinstance(data.get("text"), str):
            text = data["text"]
        offers: list[Offer] = []
        for flight in data.get("flights") or []:
            if not isinstance(flight, dict):
                continue
            offer = flight_to_offer(
                flight,
                requested_origin=requested_origin,
                requested_destination=requested_destination,
                depart_date=depart_date,
                fx=fx,
                source=source,
            )
            if offer is not None:
                offers.append(offer)
        if offers:
            offers.sort(key=lambda o: (o.price_usd, o.depart_date.isoformat()))
            return offers
    else:
        return []

    if text:
        return parse_flights_markdown(
            text,
            requested_origin=requested_origin,
            requested_destination=requested_destination,
            depart_date=depart_date,
            fx=fx,
            source=source,
        )
    return []


def calendar_cell_to_offer(cell: CalendarCell, *, source: str = "skiplagged") -> Offer:
    return Offer(
        source=source,
        origin=cell.origin,
        destination=cell.destination,
        depart_date=cell.depart_date,
        price_usd=cell.price_usd,
        price_original=cell.price_original,
        currency_original=cell.currency,
        depart_time=None,
        arrive_time=None,
        link=cell.search_url,
        cabin_bag_included=None,
        notes="calendar fare",
    )


def _cap_per_od(offers: list[Offer], limit: int = _MAX_PER_OD) -> list[Offer]:
    by_od: dict[tuple[str, str], list[Offer]] = {}
    for offer in offers:
        by_od.setdefault((offer.origin, offer.destination), []).append(offer)
    out: list[Offer] = []
    for group in by_od.values():
        group.sort(key=lambda o: (o.price_usd, o.depart_date.isoformat()))
        out.extend(group[:limit])
    out.sort(key=lambda o: (o.price_usd, o.depart_date.isoformat(), o.origin, o.destination))
    return out


_CALENDAR_FAIL_STOP = 4


class _PacedSession:
    """Serialize call *starts* so Skiplagged sees spacing, not a handshake stampede."""

    def __init__(self, session: McpSession) -> None:
        self.session = session
        self._lock = asyncio.Lock()
        self._next_call_at = 0.0

    async def call_tool(self, tool: str, args: dict[str, Any], *, retries: int = _RETRIES) -> Any:
        async with self._lock:
            now = asyncio.get_running_loop().time()
            wait = self._next_call_at - now
            if wait > 0:
                await asyncio.sleep(wait)
            self._next_call_at = asyncio.get_running_loop().time() + _CALL_SPACING_S
        return await self.session.call_tool(tool, args, retries=retries)


class SkiplaggedSource(Source):
    name: ClassVar[str] = "skiplagged"

    def _max_detail(self) -> int:
        raw = self.settings.get("max_detail_searches", _DEFAULT_MAX_DETAIL)
        try:
            return max(0, int(raw))
        except (TypeError, ValueError):
            return _DEFAULT_MAX_DETAIL

    def _destinations(self, query: SearchQuery) -> list[str]:
        # Skip WMI (extra_destinations) — calendar×detail budget is tight and
        # WMI rarely beats the main Polish airports on long-haul from TH.
        return [d.upper() for d in query.destinations]

    async def search(self, query: SearchQuery, ctx: RunContext) -> list[Offer]:
        dates = query.dates(ctx.now.date())
        if not dates:
            return []

        origins = [o.upper() for o in query.all_origins]
        destinations = self._destinations(query)
        if not origins or not destinations:
            return []

        date_from, date_to = dates[0], dates[-1]
        # Anchor flex calendar near the middle of the window so returned
        # nearby days cover the full search range.
        anchor = dates[len(dates) // 2]
        max_detail = self._max_detail()
        sem = asyncio.Semaphore(_CONCURRENCY)
        lock = asyncio.Lock()
        cells: list[CalendarCell] = []
        calendar_failures = 0
        calendar_skipped = 0
        calendar_successes = 0
        calendar_streak = 0
        calendar_abort = False
        last_calendar_error = ""
        calendar_pairs = [(o, d) for o in origins for d in destinations]

        async def one_calendar(paced: _PacedSession, origin: str, dest: str) -> None:
            nonlocal calendar_failures, calendar_skipped, calendar_successes
            nonlocal calendar_streak, calendar_abort, last_calendar_error
            async with lock:
                if calendar_abort:
                    calendar_skipped += 1
                    return
            args = {
                "origin": origin,
                "destination": dest,
                "departureDate": anchor.isoformat(),
                "sort": "price",
                "adults": int(query.adults),
            }
            async with sem:
                async with lock:
                    if calendar_abort:
                        calendar_skipped += 1
                        return
                try:
                    data = await paced.call_tool(_CALENDAR_TOOL, args, retries=_RETRIES)
                except Exception as exc:
                    ctx.log.warning(
                        "skiplagged calendar failed %s→%s: %s", origin, dest, exc
                    )
                    async with lock:
                        calendar_failures += 1
                        calendar_streak += 1
                        last_calendar_error = str(exc)
                        if calendar_streak >= _CALENDAR_FAIL_STOP:
                            calendar_abort = True
                    return
            parsed = parse_calendar_response(
                data,
                origin=origin,
                destination=dest,
                date_from=date_from,
                date_to=date_to,
                fx=ctx.fx,
            )
            async with lock:
                calendar_successes += 1
                calendar_streak = 0
                cells.extend(parsed)

        async with McpSession(
            SKIPLAGGED_MCP_URL, timeout=_TIMEOUT, legacy=True
        ) as session:
            paced = _PacedSession(session)
            await asyncio.gather(
                *(one_calendar(paced, o, d) for o, d in calendar_pairs)
            )

            calendar_worked = bool(cells)
            if calendar_worked:
                detail_cells = select_detail_cells(
                    cells,
                    max_detail=max_detail,
                    near_miss_usd=float(query.near_miss_usd),
                )
            elif calendar_failures:
                raise SourceError(
                    "skiplagged: calendar phase failed "
                    f"({calendar_failures} errors, {calendar_skipped} skipped "
                    f"after consecutive failures); {last_calendar_error}"
                )
            else:
                ctx.log.warning(
                    "skiplagged calendar phase empty (%s pairs); sampling fallback",
                    len(calendar_pairs),
                )
                detail_cells = sample_fallback_cells(
                    origins=origins,
                    destinations=destinations,
                    dates=dates,
                    max_detail=max_detail,
                )

            offers: list[Offer] = []
            detail_failures = 0
            detail_success = 0
            detail_streak = 0
            detail_abort = False
            detailed_keys: set[tuple[str, str, date]] = set()

            async def one_detail(cell: CalendarCell) -> None:
                nonlocal detail_failures, detail_success, detail_streak, detail_abort
                async with lock:
                    if detail_abort:
                        return
                args = {
                    "origin": cell.origin,
                    "destination": cell.destination,
                    "departureDate": cell.depart_date.isoformat(),
                    "sort": "price",
                    "limit": 5,
                    "includeVirtualInterlining": True,
                    "adults": int(query.adults),
                }
                async with sem:
                    async with lock:
                        if detail_abort:
                            return
                    try:
                        data = await paced.call_tool(
                            _SEARCH_TOOL, args, retries=_RETRIES
                        )
                    except Exception as exc:
                        ctx.log.warning(
                            "skiplagged detail failed %s→%s %s: %s",
                            cell.origin,
                            cell.destination,
                            cell.depart_date.isoformat(),
                            exc,
                        )
                        async with lock:
                            detail_failures += 1
                            detail_streak += 1
                            if detail_streak >= _CALENDAR_FAIL_STOP:
                                detail_abort = True
                        return
                parsed = parse_flights_response(
                    data,
                    requested_origin=cell.origin,
                    requested_destination=cell.destination,
                    depart_date=cell.depart_date,
                    fx=ctx.fx,
                    source=self.name,
                )
                async with lock:
                    detail_success += 1
                    detail_streak = 0
                    detailed_keys.add(
                        (cell.origin, cell.destination, cell.depart_date)
                    )
                    offers.extend(parsed)

            if detail_cells:
                await asyncio.gather(*(one_detail(c) for c in detail_cells))

        # Calendar-only cells (no successful detail run) become timed-less offers.
        if calendar_worked:
            for cell in cells:
                key = (cell.origin, cell.destination, cell.depart_date)
                if key in detailed_keys:
                    continue
                if cell.price_usd <= 0:
                    continue
                offers.append(calendar_cell_to_offer(cell, source=self.name))

        if not offers:
            attempted = (len(calendar_pairs) - calendar_skipped) + len(detail_cells)
            failed = calendar_failures + detail_failures
            if attempted > 0 and failed >= attempted and detail_success == 0:
                raise SourceError(
                    f"skiplagged: all {failed} search calls failed"
                )
            return []

        return _cap_per_od(offers)
