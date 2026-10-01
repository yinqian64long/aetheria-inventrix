from __future__ import annotations

import asyncio
import json
import logging
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any

import httpx
import pytest

from flightsearch.context import RunContext
from flightsearch.mcp_client import call_mcp_tool, list_mcp_tools
from flightsearch.models import SearchQuery
from flightsearch.sources.base import SourceError
from flightsearch.sources import skiplagged as skip_mod
from flightsearch.sources.skiplagged import (
    SKIPLAGGED_MCP_URL,
    SkiplaggedSource,
    calendar_cell_to_offer,
    parse_calendar_response,
    parse_flights_response,
    select_detail_cells,
)

FIXTURES = Path(__file__).resolve().parent / "fixtures" / "skiplagged"


class FakeFx:
    def __init__(self, rate: float = 1.0) -> None:
        self.rate = rate
        self.calls: list[tuple[float, str]] = []

    def to_usd(self, amount: float, currency: str) -> float:
        self.calls.append((amount, currency.upper()))
        if currency.upper() == "USD":
            return float(amount)
        return round(float(amount) * self.rate, 2)


def _load(name: str) -> Any:
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


def _query(**overrides: Any) -> SearchQuery:
    base = dict(
        home_origin="CNX",
        positioning_origins=("BKK",),
        destinations=("WAW", "KRK"),
        extra_destinations=("WMI",),
        date_from=date(2026, 10, 20),
        date_to=date(2026, 11, 1),
        adults=1,
        cabin_bags=1,
        checked_bags=0,
        max_total_usd=250.0,
        near_miss_usd=300.0,
        currency="USD",
    )
    base.update(overrides)
    return SearchQuery(**base)


def _ctx(fx: FakeFx | None = None, now: datetime | None = None) -> RunContext:
    return RunContext(
        http=httpx.AsyncClient(),
        fx=fx or FakeFx(),
        state={},
        log=logging.getLogger("test.skiplagged"),
        now=now or datetime(2026, 9, 1, tzinfo=timezone.utc),
        env={},
    )


def test_parse_calendar_markdown_filters_window() -> None:
    data = _load("calendar_bkk_krk.json")
    cells = parse_calendar_response(
        data,
        origin="BKK",
        destination="KRK",
        date_from=date(2026, 10, 20),
        date_to=date(2026, 11, 1),
        fx=FakeFx(),
    )
    assert cells
    assert all(date(2026, 10, 20) <= c.depart_date <= date(2026, 11, 1) for c in cells)
    assert cells[0].origin == "BKK"
    assert cells[0].destination == "KRK"
    assert cells[0].currency == "USD"
    # Outside-window rows (10-18, 10-19, 11-02) dropped.
    assert date(2026, 10, 18) not in {c.depart_date for c in cells}
    assert date(2026, 11, 2) not in {c.depart_date for c in cells}
    cheap = next(c for c in cells if c.depart_date == date(2026, 10, 26))
    assert cheap.price_usd == 240.0
    assert cheap.search_url and cheap.search_url.startswith("https://")


def test_select_detail_cells_cap_and_near_miss() -> None:
    data = _load("calendar_bkk_krk.json")
    cells = parse_calendar_response(
        data,
        origin="BKK",
        destination="KRK",
        date_from=date(2026, 10, 20),
        date_to=date(2026, 11, 1),
        fx=FakeFx(),
    )
    # Inject a second OD so selection spans routes.
    cnx = parse_calendar_response(
        _load("calendar_cnx_waw.json"),
        origin="CNX",
        destination="WAW",
        date_from=date(2026, 10, 20),
        date_to=date(2026, 11, 1),
        fx=FakeFx(),
    )
    picked = select_detail_cells(
        cells + cnx, max_detail=5, near_miss_usd=300.0
    )
    assert len(picked) == 5
    assert all(c.price_usd <= 300.0 for c in picked)
    assert picked[0].price_usd <= picked[-1].price_usd
    # Cheapest fixture cell is CNX-WAW $220 on 2026-10-22.
    assert picked[0].price_usd == 220.0
    assert picked[0].origin == "CNX"
    assert picked[0].destination == "WAW"


def test_parse_flights_maps_offer_fields() -> None:
    data = _load("flights_bkk_krk.json")
    offers = parse_flights_response(
        data,
        requested_origin="BKK",
        requested_destination="KRK",
        depart_date=date(2026, 10, 25),
        fx=FakeFx(),
    )
    assert len(offers) == 2
    cheapest = offers[0]
    assert cheapest.source == "skiplagged"
    assert cheapest.origin == "BKK"
    assert cheapest.destination == "KRK"
    assert cheapest.depart_date == date(2026, 10, 25)
    assert cheapest.depart_time == datetime(2026, 10, 25, 9, 0)
    assert cheapest.arrive_time == datetime(2026, 10, 26, 11, 5)
    assert cheapest.price_usd == 348.0
    assert cheapest.airlines == ["Oman Air", "Ryanair"]
    assert cheapest.flight_numbers == ["WY818", "FR1234"]
    assert cheapest.stops == 2
    assert cheapest.self_transfer is True
    assert "virtual-interline" in cheapest.notes
    assert cheapest.link and cheapest.link.startswith("https://")
    assert cheapest.cabin_bag_included is None

    second = offers[1]
    assert second.self_transfer is False
    assert second.link and second.link.startswith("https://skiplagged.com/")


def test_parse_flights_markdown_maps_fields() -> None:
    data = _load("flights_bkk_krk_md.json")
    offers = parse_flights_response(
        data,
        requested_origin="BKK",
        requested_destination="KRK",
        depart_date=date(2026, 10, 25),
        fx=FakeFx(),
    )
    assert len(offers) == 2
    cheapest = offers[0]
    assert cheapest.price_usd == 348.0
    assert cheapest.origin == "BKK"
    assert cheapest.destination == "KRK"
    assert cheapest.depart_time == datetime(2026, 10, 25, 9, 0)
    assert cheapest.arrive_time == datetime(2026, 10, 26, 11, 5)
    assert cheapest.airlines == ["Oman Air", "Ryanair"]
    assert cheapest.flight_numbers == ["WY818", "WY143", "FR4463"]
    assert cheapest.stops == 2
    assert cheapest.duration_minutes == 32 * 60 + 5
    assert cheapest.self_transfer is True
    assert cheapest.link and "#trip=" in cheapest.link

    # Second row changes airport LHR→LTN → self-transfer even without explicit type.
    second = offers[1]
    assert second.self_transfer is True
    assert second.flight_numbers == ["WY816", "WY103", "FR1813"]


def test_hidden_city_flag_sets_actual_destination() -> None:
    data = _load("flights_hidden_city.json")
    offers = parse_flights_response(
        data,
        requested_origin="CNX",
        requested_destination="WAW",
        depart_date=date(2026, 10, 22),
        fx=FakeFx(),
    )
    assert len(offers) == 1
    offer = offers[0]
    # Keep real final arrival; flag the risk.
    assert offer.destination == "BUD"
    assert "hidden-city" in offer.notes
    assert "searched WAW" in offer.notes


def test_hidden_city_markdown() -> None:
    offers = parse_flights_response(
        _load("flights_hidden_city_md.json"),
        requested_origin="CNX",
        requested_destination="WAW",
        depart_date=date(2026, 10, 22),
        fx=FakeFx(),
    )
    assert len(offers) == 1
    assert offers[0].destination == "BUD"
    assert "hidden-city" in offers[0].notes
    assert offers[0].flight_numbers == ["SL602", "W62201"]

def _patch_session(monkeypatch: pytest.MonkeyPatch, handler: Any) -> None:
    class FakeSession:
        def __init__(self, *args: Any, **kwargs: Any) -> None:
            self.args = args
            self.kwargs = kwargs

        async def __aenter__(self) -> FakeSession:
            return self

        async def __aexit__(self, *args: Any) -> None:
            return None

        async def call_tool(self, tool: str, args: dict, retries: int = 2) -> Any:
            return await handler(tool, args)

    monkeypatch.setattr(skip_mod, "McpSession", FakeSession)
    monkeypatch.setattr(skip_mod, "_CALL_SPACING_S", 0.0)


def test_calendar_only_offer_has_no_depart_time() -> None:
    cells = parse_calendar_response(
        _load("calendar_bkk_krk.json"),
        origin="BKK",
        destination="KRK",
        date_from=date(2026, 10, 20),
        date_to=date(2026, 11, 1),
        fx=FakeFx(),
    )
    offer = calendar_cell_to_offer(cells[0])
    assert offer.depart_time is None
    assert offer.notes == "calendar fare"
    assert offer.price_usd == cells[0].price_usd


@pytest.mark.asyncio
async def test_search_partial_failure(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[tuple[str, dict[str, Any]]] = []

    async def fake_call(tool: str, args: dict, **kwargs: Any) -> Any:
        calls.append((tool, dict(args)))
        if tool == "sk_flex_departure_calendar":
            if args["destination"] == "WAW":
                raise RuntimeError("calendar down")
            return _load("calendar_bkk_krk.json")
        if tool == "sk_flights_search":
            if args["destination"] == "KRK" and args["departureDate"] == "2026-10-26":
                raise RuntimeError("detail down")
            return _load("flights_bkk_krk_md.json")
        raise AssertionError(f"unexpected tool {tool}")

    _patch_session(monkeypatch, fake_call)
    src = SkiplaggedSource({"max_detail_searches": 3})
    query = _query(
        positioning_origins=(),
        destinations=("WAW", "KRK"),
        extra_destinations=("WMI",),
    )
    offers = await src.search(query, _ctx())
    assert offers
    # WMI must not be queried (extra destination skipped).
    assert all(c[1].get("destination") != "WMI" for c in calls)
    # Calendar failure for WAW still allows KRK path to produce offers.
    assert any(o.destination == "KRK" for o in offers)
    # Calendar-only cells (detail failures / not selected) carry the note.
    assert any(o.notes == "calendar fare" for o in offers)


@pytest.mark.asyncio
async def test_search_all_fail_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    async def fake_call(tool: str, args: dict, **kwargs: Any) -> Any:
        raise RuntimeError("down")

    _patch_session(monkeypatch, fake_call)
    src = SkiplaggedSource({"max_detail_searches": 2})
    query = _query(
        positioning_origins=(),
        destinations=("WAW",),
        extra_destinations=(),
    )
    with pytest.raises(SourceError, match="calendar phase failed"):
        await src.search(query, _ctx())


@pytest.mark.asyncio
async def test_search_empty_dates() -> None:
    src = SkiplaggedSource()
    query = _query(date_from=date(2026, 10, 20), date_to=date(2026, 10, 22))
    ctx = _ctx(now=datetime(2026, 11, 10, tzinfo=timezone.utc))
    assert await src.search(query, ctx) == []


@pytest.mark.asyncio
async def test_calendar_errors_stop_early(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[tuple[str, str]] = []

    async def fake_call(tool: str, args: dict, **kwargs: Any) -> Any:
        calls.append((tool, str(args.get("destination") or "")))
        raise RuntimeError("HTTP 429: Too Many Requests")

    _patch_session(monkeypatch, fake_call)
    src = SkiplaggedSource({"max_detail_searches": 12})
    query = _query(
        positioning_origins=(),
        destinations=("WAW", "KRK", "GDN", "KTW", "WRO", "POZ", "PRG", "IST"),
        extra_destinations=(),
    )
    with pytest.raises(SourceError, match="calendar phase failed"):
        await src.search(query, _ctx())
    assert calls
    assert all(tool == "sk_flex_departure_calendar" for tool, _ in calls)
    assert len(calls) <= 6
    assert len(calls) >= 4


@pytest.mark.asyncio
async def test_empty_calendar_uses_fallback(monkeypatch: pytest.MonkeyPatch) -> None:
    detail_calls: list[dict[str, Any]] = []

    async def fake_call(tool: str, args: dict, **kwargs: Any) -> Any:
        if tool == "sk_flex_departure_calendar":
            return {"text": "no priced days"}
        if tool == "sk_flights_search":
            detail_calls.append(dict(args))
            return _load("flights_bkk_krk_md.json")
        raise AssertionError(tool)

    _patch_session(monkeypatch, fake_call)
    src = SkiplaggedSource({"max_detail_searches": 4})
    query = _query(
        positioning_origins=("BKK",),
        destinations=("KRK",),
        extra_destinations=(),
        date_from=date(2026, 10, 20),
        date_to=date(2026, 10, 25),
    )
    offers = await src.search(query, _ctx())
    assert offers
    assert detail_calls
    assert len(detail_calls) <= 4
    assert all(c["sort"] == "price" for c in detail_calls)
    assert all(c["limit"] == 5 for c in detail_calls)
    assert all(c["includeVirtualInterlining"] is True for c in detail_calls)


@pytest.mark.live
@pytest.mark.asyncio
async def test_live_list_and_one_search() -> None:
    tools = None
    last_exc: Exception | None = None
    try:
        tools = await list_mcp_tools(SKIPLAGGED_MCP_URL, legacy=True)
    except Exception as exc:  # noqa: BLE001 — live flake
        last_exc = exc
    if tools is None:
        pytest.skip(f"skiplagged list_tools unavailable: {last_exc}")
    names = {t["name"] for t in tools}
    assert "sk_flights_search" in names
    assert "sk_flex_departure_calendar" in names

    data = None
    for _ in range(3):
        try:
            data = await call_mcp_tool(
                SKIPLAGGED_MCP_URL,
                "sk_flights_search",
                {
                    "origin": "BKK",
                    "destination": "KRK",
                    "departureDate": "2026-10-25",
                    "sort": "price",
                    "limit": 3,
                    "includeVirtualInterlining": True,
                    "adults": 1,
                },
                timeout=90,
                retries=2,
                legacy=True,
            )
            break
        except Exception as exc:  # noqa: BLE001 — live flake
            last_exc = exc
            await asyncio.sleep(3)
    if data is None:
        pytest.skip(f"skiplagged search unavailable: {last_exc}")
    assert isinstance(data, dict)
    offers = parse_flights_response(
        data,
        requested_origin="BKK",
        requested_destination="KRK",
        depart_date=date(2026, 10, 25),
        fx=FakeFx(),
    )
    assert isinstance(offers, list)
    assert offers, "expected at least one live offer"
    for o in offers:
        assert o.source == "skiplagged"
        assert o.price_usd > 0
        assert o.link
        assert o.depart_time is not None