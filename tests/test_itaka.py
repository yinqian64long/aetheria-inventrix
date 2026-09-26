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
from flightsearch.sources.itaka import (
    ItakaSource,
    extract_flights_payload,
    offers_from_payload,
)

FIXTURES = Path(__file__).resolve().parent / "fixtures" / "itaka"


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
        destinations=("WAW", "KRK", "GDN", "KTW", "WRO", "POZ"),
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
        log=logging.getLogger("test.itaka"),
        now=datetime(2026, 9, 25, 12, 0, tzinfo=timezone.utc),
        env={},
    )


def _wrap_next_html(payload: dict[str, Any]) -> str:
    raw = json.dumps(payload, ensure_ascii=False)
    esc = raw.replace("\\", "\\\\").replace('"', '\\"')
    return (
        "<html><body>"
        f'<script>self.__next_f.push([1,"{esc}"])</script>'
        "</body></html>"
    )


def test_window_one_way_has_no_th_to_pl() -> None:
    offers = offers_from_payload(_load("window_flights.json"), query=_query(), fx=FakeFx())
    assert offers == []


def test_th_pl_sample_parses() -> None:
    fx = FakeFx()
    offers = offers_from_payload(
        _load("th_pl_sample.json"),
        query=_query(date_from=date(2026, 11, 1), date_to=date(2027, 3, 1)),
        fx=fx,
    )
    assert offers
    first = offers[0]
    assert first.source == "itaka"
    assert first.origin in {"BKK", "HKT", "KBV", "DMK", "CNX"}
    assert first.destination in {"WAW", "KTW", "KRK", "GDN", "WRO", "POZ", "WMI"}
    assert first.currency_original == "PLN"
    assert first.price_usd == round(first.price_original * 0.25, 2)
    assert first.depart_time is not None
    assert "charter" in first.notes.lower()
    assert first.link and first.link.startswith("https://")
    assert fx.calls
    # handWeight is explicit cabin baggage; checked weight stays in notes only.
    assert first.cabin_bag_included is True
    assert "cabin bag 8 kg included" in first.notes
    assert "checked bag 23 kg included" in first.notes


def test_checked_only_luggage_does_not_set_cabin_flag() -> None:
    from flightsearch.sources.itaka import _bag_note

    cabin, notes = _bag_note(
        {"luggage": {"included": True, "details": {"registeredWeight": 20}}}
    )
    assert cabin is None
    assert "checked bag 20 kg included" in notes
    assert "cabin" not in notes

    cabin2, notes2 = _bag_note({"luggage": {"included": True}})
    assert cabin2 is None
    assert notes2 == "baggage included"


def test_extract_from_next_html_fixtures() -> None:
    window_payload = extract_flights_payload(_load("window_next.html"))
    assert "flightsList" in window_payload
    sample_payload = extract_flights_payload(_load("th_pl_next.html"))
    offers = offers_from_payload(
        sample_payload,
        query=_query(date_from=date(2026, 11, 1), date_to=date(2027, 3, 1)),
        fx=FakeFx(),
    )
    assert offers


@pytest.mark.asyncio
@respx.mock
async def test_search_paginates_once_when_single_page() -> None:
    payload = {**_load("window_flights.json"), "pagesCount": 1}
    respx.get(url__regex=r"https://www\.itaka\.pl/bilety-lotnicze/.*").mock(
        return_value=httpx.Response(200, text=_wrap_next_html(payload))
    )
    ctx = _ctx()
    try:
        offers = await ItakaSource().search(_query(), ctx)
    finally:
        await ctx.http.aclose()
    assert offers == []
    assert respx.calls.call_count == 1


@pytest.mark.asyncio
@respx.mock
async def test_challenge_raises() -> None:
    respx.get(url__regex=r"https://www\.itaka\.pl/bilety-lotnicze/.*").mock(
        return_value=httpx.Response(403, text="Just a moment... cf-challenge")
    )
    ctx = _ctx()
    try:
        with pytest.raises(SourceError, match="blocked|challenged"):
            await ItakaSource().search(_query(), ctx)
    finally:
        await ctx.http.aclose()


@pytest.mark.live
@pytest.mark.asyncio
async def test_live_window_search_completes() -> None:
    ctx = _ctx()
    try:
        offers = await ItakaSource().search(_query(), ctx)
    finally:
        await ctx.http.aclose()
    assert isinstance(offers, list)
