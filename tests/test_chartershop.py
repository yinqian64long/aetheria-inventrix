from __future__ import annotations

import json
import logging
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any

import httpx
import pytest
import respx

from flightsearch.context import RunContext
from flightsearch.models import SearchQuery
from flightsearch.sources.base import SourceError
from flightsearch.sources.chartershop import (
    ChartershopSource,
    offers_from_schedule_and_prices,
    parse_min_prices,
    parse_schedule,
)

FIXTURES = Path(__file__).resolve().parent / "fixtures" / "chartershop"


class FakeFx:
    def __init__(self, rate: float = 0.25) -> None:
        self.rate = rate
        self.calls: list[tuple[float, str]] = []

    def to_usd(self, amount: float, currency: str) -> float:
        self.calls.append((float(amount), currency.upper()))
        if currency.upper() == "USD":
            return float(amount)
        return round(float(amount) * self.rate, 2)


def _load(name: str) -> Any:
    path = FIXTURES / name
    if path.suffix == ".json":
        return json.loads(path.read_text(encoding="utf-8"))
    return path.read_text(encoding="utf-8")


def _query(**overrides: Any) -> SearchQuery:
    base: dict[str, Any] = dict(
        home_origin="CNX",
        positioning_origins=("BKK", "DMK", "HKT"),
        destinations=("WAW", "KRK", "GDN", "KTW", "WRO", "POZ", "PRG", "IST", "BUD"),
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


def _ctx(fx: FakeFx | None = None) -> RunContext:
    return RunContext(
        http=httpx.AsyncClient(),
        fx=fx or FakeFx(),
        state={},
        log=logging.getLogger("test.chartershop"),
        now=datetime(2026, 9, 25, 12, 0, tzinfo=timezone.utc),
        env={},
    )


def test_schedule_keeps_only_th_to_pl() -> None:
    rows = parse_schedule(_load("th_pl.html"))
    assert rows
    assert {r["origin"] for r in rows} <= {"BKK", "HKT", "KBV", "DMK", "CNX"}
    assert {r["destination"] for r in rows} <= {
        "WAW",
        "KTW",
        "POZ",
        "KRK",
        "GDN",
        "WRO",
        "PRG",
        "IST",
        "BUD",
        "WMI",
    }
    assert not any(r["origin"] in {"WAW", "KTW", "POZ"} for r in rows)


def test_window_empty_prices_yield_no_offers() -> None:
    schedule = parse_schedule(_load("th_pl.html"))
    empty = parse_min_prices(_load("min_prices_empty.json"))
    offers = offers_from_schedule_and_prices(
        schedule,
        {("HKT", "KTW"): empty, ("BKK", "WAW"): empty},
        query=_query(),
        fx=FakeFx(),
    )
    assert offers == []


def test_later_saturday_hkt_ktw_priced() -> None:
    schedule = parse_schedule(_load("th_pl.html"))
    prices = parse_min_prices(_load("min_prices_hkt_ktw.json"))
    fx = FakeFx()
    offers = offers_from_schedule_and_prices(
        schedule,
        {("HKT", "KTW"): prices},
        query=_query(date_from=date(2026, 11, 1), date_to=date(2026, 11, 30)),
        fx=fx,
    )
    assert offers
    assert all(o.origin == "HKT" and o.destination == "KTW" for o in offers)
    assert all(o.depart_date.weekday() == 5 for o in offers)
    assert all(o.depart_time is not None for o in offers)
    assert all("charter" in o.notes.lower() for o in offers)
    assert offers[0].price_usd == round(offers[0].price_original * 0.25, 2)
    assert fx.calls
    # Schedule pages never expose cabin-bag evidence.
    assert all(o.cabin_bag_included is None for o in offers)


@pytest.mark.asyncio
@respx.mock
async def test_search_empty_min_prices() -> None:
    respx.get("https://chartershop.pl/aviatickets/TH/PL").mock(
        return_value=httpx.Response(200, text=_load("th_pl.html"))
    )
    respx.post(url__regex=r".*adv-min-prices.*").mock(
        return_value=httpx.Response(200, json=_load("min_prices_empty.json"))
    )
    ctx = _ctx()
    try:
        offers = await ChartershopSource().search(_query(), ctx)
    finally:
        await ctx.http.aclose()
    assert offers == []
    assert 2 <= respx.calls.call_count <= 7


@pytest.mark.asyncio
@respx.mock
async def test_turnstile_raises() -> None:
    respx.get("https://chartershop.pl/aviatickets/TH/PL").mock(
        return_value=httpx.Response(200, text=_load("turnstile.html"))
    )
    ctx = _ctx()
    try:
        with pytest.raises(SourceError):
            await ChartershopSource().search(_query(), ctx)
    finally:
        await ctx.http.aclose()


@pytest.mark.live
@pytest.mark.asyncio
async def test_live_window_search_completes() -> None:
    ctx = _ctx()
    try:
        offers = await ChartershopSource().search(_query(), ctx)
    finally:
        await ctx.http.aclose()
    assert isinstance(offers, list)
