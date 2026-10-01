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
from flightsearch.sources.rpl import (
    RplSource,
    extract_przyloty,
    offers_from_przyloty,
    offers_from_search,
)

FIXTURES = Path(__file__).resolve().parent / "fixtures" / "rpl"


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
        destinations=("WAW", "KRK", "GDN", "KTW", "WRO", "POZ", "PRG", "IST"),
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
        log=logging.getLogger("test.rpl"),
        now=datetime(2026, 9, 25, 12, 0, tzinfo=timezone.utc),
        env={},
    )


def test_search_window_fixture_empty() -> None:
    offers = offers_from_search(_load("search_window.json"), query=_query(), fx=FakeFx())
    assert offers == []


def test_search_later_parses_card() -> None:
    fx = FakeFx()
    offers = offers_from_search(
        _load("search_later.json"),
        query=_query(date_from=date(2026, 11, 1), date_to=date(2027, 3, 1)),
        fx=fx,
    )
    assert len(offers) == 1
    offer = offers[0]
    assert offer.source == "rpl"
    assert offer.origin == "BKK"
    assert offer.destination == "KTW"
    assert offer.depart_date == date(2027, 2, 21)
    assert offer.price_original == 9998.0
    assert offer.currency_original == "PLN"
    assert offer.price_usd == 2499.5
    assert "LOT" in offer.airlines[0]
    assert "charter" in offer.notes.lower()
    assert offer.link and offer.link.startswith("https://")
    assert fx.calls[0] == (9998.0, "PLN")
    # Search API has no cabin/hand field — must stay unknown.
    assert offer.cabin_bag_included is None


def test_search_bagaz_is_checked_not_cabin() -> None:
    """Search API ``Bagaz`` is checked-bag kg; must not set cabin_bag_included."""
    payload = _load("search_later.json")
    payload["Destynacje"][0]["Bagaz"] = 20
    offer = offers_from_search(
        payload,
        query=_query(date_from=date(2026, 11, 1), date_to=date(2027, 3, 1)),
        fx=FakeFx(),
    )[0]
    assert offer.cabin_bag_included is None
    assert "checked bag 20 kg included" in offer.notes


def test_przyloty_json_and_html() -> None:
    days = _load("przyloty_bangkok.json")
    offers = offers_from_przyloty(
        days,
        query=_query(date_from=date(2026, 11, 1), date_to=date(2026, 11, 30)),
        fx=FakeFx(),
        default_origin="BKK",
        region_slug="bangkok",
    )
    assert len(offers) == 2
    first = offers[0]
    assert first.origin == "BKK"
    assert first.destination == "POZ"
    assert first.depart_date == date(2026, 11, 21)
    assert first.flight_numbers
    assert first.depart_time is not None
    # BagazPodreczny is explicit cabin/hand baggage.
    assert first.cabin_bag_included is True
    assert "cabin bag 8 kg included" in first.notes
    assert "checked bag 23 kg included" in first.notes

    html_days = extract_przyloty(_load("dest_bangkok.html"))
    assert len(html_days) == 2


def test_window_filters_out_november_przyloty() -> None:
    offers = offers_from_przyloty(
        _load("przyloty_bangkok.json"),
        query=_query(),
        fx=FakeFx(),
        default_origin="BKK",
        region_slug="bangkok",
    )
    assert offers == []


@pytest.mark.asyncio
@respx.mock
async def test_search_hits_api_and_two_dest_pages() -> None:
    respx.get("https://r.pl/api/czartery/wyszukiwanie/v4.1/wyszukaj").mock(
        return_value=httpx.Response(200, json=_load("search_window.json"))
    )
    respx.get("https://r.pl/bilety-czarterowe/tajlandia/bangkok").mock(
        return_value=httpx.Response(200, text=_load("dest_bangkok.html"))
    )
    respx.get("https://r.pl/bilety-czarterowe/tajlandia/phuket").mock(
        return_value=httpx.Response(200, text=_load("dest_phuket.html"))
    )
    ctx = _ctx()
    try:
        offers = await RplSource().search(_query(), ctx)
    finally:
        await ctx.http.aclose()
    assert offers == []
    assert respx.calls.call_count == 3


@pytest.mark.asyncio
@respx.mock
async def test_challenge_raises() -> None:
    respx.get("https://r.pl/api/czartery/wyszukiwanie/v4.1/wyszukaj").mock(
        return_value=httpx.Response(403, text="Just a moment...")
    )
    ctx = _ctx()
    try:
        with pytest.raises(SourceError, match="blocked|challenged"):
            await RplSource().search(_query(), ctx)
    finally:
        await ctx.http.aclose()


@pytest.mark.live
@pytest.mark.asyncio
async def test_live_window_search_completes() -> None:
    ctx = _ctx()
    try:
        offers = await RplSource().search(_query(), ctx)
    finally:
        await ctx.http.aclose()
    assert isinstance(offers, list)
    assert offers == []
