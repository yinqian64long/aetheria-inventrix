from __future__ import annotations

import json
import logging
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any

import httpx
import pytest

from flightsearch.context import RunContext
from flightsearch.models import SearchQuery
from flightsearch.sources.base import SourceError
from flightsearch.sources import kiwi as kiwi_mod
from flightsearch.sources.kiwi import KiwiSource, fetch_hops, parse_search_response

FIXTURES = Path(__file__).resolve().parent / "fixtures" / "kiwi"


class FakeFx:
    def __init__(self, rate: float = 1.1) -> None:
        self.rate = rate
        self.calls: list[tuple[float, str]] = []

    def to_usd(self, amount: float, currency: str) -> float:
        self.calls.append((amount, currency.upper()))
        if currency.upper() == "USD":
            return float(amount)
        return round(float(amount) * self.rate, 2)


def _load(name: str) -> dict[str, Any]:
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


def _query(**overrides: Any) -> SearchQuery:
    base = dict(
        home_origin="CNX",
        positioning_origins=("BKK", "DMK", "HKT"),
        destinations=("WAW",),
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
        log=logging.getLogger("test.kiwi"),
        now=now or datetime(2026, 9, 1, tzinfo=timezone.utc),
        env={},
    )


def test_parse_search_maps_offer_fields() -> None:
    data = _load("cnx_waw_range.json")
    fx = FakeFx()
    query = _query()
    offers = parse_search_response(
        data, query=query, fx=fx, requested_from="CNX", requested_to="WAW"
    )
    assert offers
    cheapest = offers[0]
    assert cheapest.source == "kiwi"
    assert cheapest.origin == "CNX"
    assert cheapest.destination == "WAW"
    assert cheapest.depart_date == date(2026, 10, 27)
    assert cheapest.depart_time == datetime(2026, 10, 27, 9, 10)
    assert cheapest.arrive_time == datetime(2026, 10, 28, 6, 20)
    assert cheapest.price_usd == 198.0
    assert cheapest.price_original == 198.0
    assert cheapest.currency_original == "USD"
    assert cheapest.airlines == ["EY"]
    assert cheapest.flight_numbers == ["EY427", "EY159"]
    assert cheapest.stops == 1
    assert cheapest.duration_minutes == 97800 // 60
    assert cheapest.link and cheapest.link.startswith("https://")
    assert cheapest.cabin_bag_included is True
    assert cheapest.self_transfer is False
    assert fx.calls and fx.calls[0] == (198.0, "USD")


def test_parse_self_transfer_and_airport_change_notes() -> None:
    data = _load("cnx_wmi_range.json")
    fx = FakeFx()
    offers = parse_search_response(
        data, query=_query(), fx=fx, requested_from="CNX", requested_to="WMI"
    )
    assert offers
    self_x = next(o for o in offers if o.self_transfer)
    assert "self-transfer" in self_x.notes
    changed = [o for o in offers if "airport change" in o.notes]
    assert changed


def test_usd_conversion_via_fx() -> None:
    data = _load("cnx_waw_eur.json")
    fx = FakeFx(rate=1.1)
    offers = parse_search_response(
        data, query=_query(), fx=fx, requested_from="CNX", requested_to="WAW"
    )
    assert offers
    assert offers[0].currency_original == "EUR"
    assert offers[0].price_original == 180.0
    assert offers[0].price_usd == 198.0
    assert fx.calls[0] == (180.0, "EUR")


def test_top_20_cap() -> None:
    data = _load("many_itineraries.json")
    offers = parse_search_response(
        data, query=_query(), fx=FakeFx(), requested_from="CNX", requested_to="WAW"
    )
    assert len(offers) == 20
    assert offers[0].price_usd == 100.0
    assert offers[-1].price_usd == 119.0


@pytest.mark.asyncio
async def test_search_empty_dates() -> None:
    src = KiwiSource()
    query = _query(date_from=date(2026, 10, 20), date_to=date(2026, 10, 22))
    ctx = _ctx(now=datetime(2026, 11, 10, tzinfo=timezone.utc))
    assert await src.search(query, ctx) == []


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

    monkeypatch.setattr(kiwi_mod, "McpSession", FakeSession)


@pytest.mark.asyncio
async def test_partial_failure_tolerance(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[tuple[str, str]] = []

    async def fake_call(tool: str, args: dict, **kwargs: Any) -> Any:
        origin, dest = args["flyFrom"], args["flyTo"]
        calls.append((origin, dest))
        if dest == "WMI":
            raise RuntimeError("boom")
        return _load("cnx_waw_range.json")

    _patch_session(monkeypatch, fake_call)
    src = KiwiSource()
    query = _query(
        positioning_origins=(),
        destinations=("WAW",),
        extra_destinations=("WMI",),
    )
    ctx = _ctx()
    offers = await src.search(query, ctx)
    assert set(calls) == {("CNX", "WAW"), ("CNX", "WMI")}
    assert offers
    assert all(o.origin == "CNX" for o in offers)
    assert any(o.destination in {"WAW", "WMI"} for o in offers)


@pytest.mark.asyncio
async def test_all_fail_raises_source_error(monkeypatch: pytest.MonkeyPatch) -> None:
    async def fake_call(tool: str, args: dict, **kwargs: Any) -> Any:
        raise RuntimeError("down")

    _patch_session(monkeypatch, fake_call)
    src = KiwiSource()
    query = _query(positioning_origins=(), destinations=("WAW",), extra_destinations=())
    with pytest.raises(SourceError, match="all .* failed"):
        await src.search(query, _ctx())


@pytest.mark.asyncio
async def test_fetch_hops_date_span(monkeypatch: pytest.MonkeyPatch) -> None:
    seen: list[tuple[str, str, str | None]] = []

    async def fake_call(tool: str, args: dict, **kwargs: Any) -> Any:
        seen.append(
            (args["flyFrom"], args["flyTo"], args.get("departureDateTo"))
        )
        data = _load("cnx_bkk_hop.json")
        # Rewrite to match requested day so keep filter passes.
        day = datetime.strptime(args["departureDate"], "%d/%m/%Y").date()
        for it in data["itineraries"]:
            ob = it["outbound"]
            dep = datetime(day.year, day.month, day.day, 8, 0)
            arr = datetime(day.year, day.month, day.day, 9, 20)
            ob["departureTime"] = dep.isoformat()
            ob["arrivalTime"] = arr.isoformat()
            ob["from"] = "CNX"
            ob["to"] = args["flyTo"]
            for seg in ob["segments"]:
                seg["departureTime"] = dep.isoformat()
                seg["arrivalTime"] = arr.isoformat()
                seg["from"] = "CNX"
                seg["to"] = args["flyTo"]
        return data

    _patch_session(monkeypatch, fake_call)
    query = _query(
        positioning_origins=("BKK", "DMK"),
        destinations=("WAW",),
        extra_destinations=(),
        date_from=date(2026, 10, 20),
        date_to=date(2026, 10, 22),
    )
    offers = await fetch_hops(query, _ctx())
    # first search date - 1 .. last = 19..22 inclusive = 4 days × 2 dests
    assert len(seen) == 8
    assert all(s[0] == "CNX" for s in seen)
    assert {s[1] for s in seen} == {"BKK", "DMK"}
    assert all(s[2] is None for s in seen)  # per-date, no range
    assert offers
    assert all(o.depart_time and o.arrive_time for o in offers)
    assert min(o.depart_date for o in offers) == date(2026, 10, 19)
    assert max(o.depart_date for o in offers) == date(2026, 10, 22)


@pytest.mark.live
@pytest.mark.asyncio
async def test_live_search_cnx_waw() -> None:
    src = KiwiSource()
    query = _query(
        positioning_origins=(),
        destinations=("WAW",),
        extra_destinations=(),
    )
    ctx = _ctx()
    try:
        offers = await src.search(query, ctx)
    finally:
        await ctx.http.aclose()
    assert offers
    assert any(o.origin == "CNX" and o.destination in {"WAW", "WMI"} for o in offers)
    assert min(o.price_usd for o in offers) < 400


@pytest.mark.live
@pytest.mark.asyncio
async def test_live_fetch_hops() -> None:
    query = _query(
        positioning_origins=("BKK",),
        destinations=("WAW",),
        extra_destinations=(),
        date_from=date(2026, 10, 25),
        date_to=date(2026, 10, 27),
    )
    ctx = _ctx()
    try:
        offers = await fetch_hops(query, ctx)
    finally:
        await ctx.http.aclose()
    # 25-1=24 .. 27 → 4 days × BKK
    assert offers
    dates = {o.depart_date for o in offers}
    assert date(2026, 10, 25) in dates or date(2026, 10, 26) in dates
    assert all(o.origin == "CNX" for o in offers)
    assert all(o.depart_time and o.arrive_time for o in offers)
