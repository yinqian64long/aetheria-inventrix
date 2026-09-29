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
from flightsearch.sources.trip import (
    TRIP_AIRLINE_URL,
    TripSource,
    city_code,
    classify_markdown,
    coerce_markdown,
    parse_airline_markdown,
)
from flightsearch.sources import load_source

FIXTURE = Path(__file__).resolve().parent / "fixtures" / "trip" / "cnx_waw.md"


class FakeFx:
    def __init__(self, rate: float = 0.14) -> None:
        self.rate = rate
        self.calls: list[tuple[float, str]] = []

    def to_usd(self, amount: float, currency: str) -> float:
        self.calls.append((amount, currency.upper()))
        if currency.upper() == "USD":
            return float(amount)
        return round(float(amount) * self.rate, 2)


def _query(**overrides: Any) -> SearchQuery:
    base = dict(
        home_origin="CNX",
        positioning_origins=("BKK",),
        destinations=("WAW",),
        extra_destinations=(),
        date_from=date(2026, 10, 27),
        date_to=date(2026, 10, 28),
        adults=1,
        cabin_bags=1,
        checked_bags=0,
        max_total_usd=250.0,
        near_miss_usd=300.0,
        currency="USD",
    )
    base.update(overrides)
    return SearchQuery(**base)


def _ctx(
    *,
    http: httpx.AsyncClient | None = None,
    fx: FakeFx | None = None,
    state: dict | None = None,
    env: dict | None = None,
) -> RunContext:
    return RunContext(
        http=http or httpx.AsyncClient(),
        fx=fx or FakeFx(),
        state=state if state is not None else {},
        log=logging.getLogger("test.trip"),
        now=datetime(2026, 9, 29, 12, 0, tzinfo=timezone.utc),
        env=env if env is not None else {"TRIPGENIE_API_KEY": "test-activation-code"},
    )


def test_city_code_maps_metro_airports() -> None:
    assert city_code("dmk") == "BKK"
    assert city_code("WMI") == "WAW"
    assert city_code("CNX") == "CNX"


def test_load_source_trip() -> None:
    src = load_source("trip", {"max_searches_per_run": 4})
    assert isinstance(src, TripSource)
    assert src.name == "trip"


def test_is_available_requires_activation_code() -> None:
    src = TripSource()
    ok, reason = src.is_available({})
    assert ok is False
    assert "TRIPGENIE_API_KEY" in reason
    ok, reason = src.is_available({"TRIPGENIE_API_KEY": "  "})
    assert ok is False
    ok, reason = src.is_available({"TRIPGENIE_API_KEY": "abc"})
    assert ok is True


def test_parse_markdown_maps_fields_and_drops_other_airports() -> None:
    text = FIXTURE.read_text(encoding="utf-8")
    fx = FakeFx()
    offers = parse_airline_markdown(
        text,
        fx=fx,
        adults=1,
        requested_from="CNX",
        requested_to="WAW",
        window_from=date(2026, 10, 20),
        window_to=date(2026, 11, 1),
    )
    assert [o.flight_numbers[0] for o in offers] == ["EY427", "HU7601"]
    ey = offers[0]
    assert ey.source == "trip"
    assert ey.origin == "CNX"
    assert ey.destination == "WAW"
    assert ey.depart_date == date(2026, 10, 27)
    assert ey.depart_time == datetime(2026, 10, 27, 9, 10)
    assert ey.arrive_time == datetime(2026, 10, 28, 6, 20)
    assert ey.price_usd == 210.0
    assert ey.price_original == 210.0
    assert ey.currency_original == "USD"
    assert ey.airlines == ["EY"]
    assert ey.flight_numbers == ["EY427", "EY159"]
    assert ey.stops == 1
    assert ey.duration_minutes == 1270
    assert ey.cabin_bag_included is True
    assert ey.self_transfer is None
    assert ey.link and "dcity=cnx" in ey.link and "acity=waw" in ey.link

    hu = offers[1]
    assert hu.arrive_time == datetime(2026, 10, 28, 6, 40)
    assert hu.price_original == 4600.0
    assert hu.currency_original == "CNY"
    assert hu.price_usd == round(4600 * 0.14, 2)
    assert hu.stops == 0
    assert (4600.0, "CNY") in fx.calls


def test_coerce_json_string_and_classify() -> None:
    raw = json.dumps(FIXTURE.read_text(encoding="utf-8"))
    text = coerce_markdown(raw)
    assert "**Flight No:" in text
    assert classify_markdown(text) == "ok"
    assert classify_markdown("invalid token") == "auth"
    assert classify_markdown("Sorry, no flights found") == "empty"
    assert classify_markdown('{"message":"upstream timeout"}') == "error"


def test_self_transfer_note() -> None:
    text = """
**Flight No: SL602 / W62201**
- Price: Total 199 USD
- Time: 2026-10-22 08:00 - 2026-10-22 18:30, Duration 630 minutes
- Airport: Chiang Mai (CNX) → Warsaw (WAW)
- Airline: Thai Lion Air (SL), Wizz Air (W6)
- self-transfer, separate tickets
"""
    offers = parse_airline_markdown(
        text,
        fx=FakeFx(),
        adults=1,
        requested_from="CNX",
        requested_to="WAW",
        window_from=date(2026, 10, 20),
        window_to=date(2026, 11, 1),
    )
    assert len(offers) == 1
    assert offers[0].self_transfer is True
    assert offers[0].notes == "self-transfer"
    assert offers[0].airlines == ["SL", "W6"]
    assert offers[0].stops == 1


@pytest.mark.asyncio
@respx.mock
async def test_search_sends_city_codes_and_advances_cursor(
    caplog: pytest.LogCaptureFixture,
) -> None:
    secret = "test-activation-code"
    route = respx.post(TRIP_AIRLINE_URL).mock(
        return_value=httpx.Response(200, text="placeholder")
    )
    src = TripSource({"max_searches_per_run": 1, "concurrency": 1})
    state: dict = {}
    query = _query(
        home_origin="DMK",
        positioning_origins=(),
        destinations=("WMI",),
        date_from=date(2026, 10, 27),
        date_to=date(2026, 10, 28),
    )
    dmk = """
**Flight No: LO123**
- Price: Total 180 USD
- Time: 2026-10-27 11:00 - 2026-10-27 18:00, Duration 420 minutes
- Airport: Don Mueang (DMK) → Warsaw Modlin (WMI)
- Airline: LOT (LO)
- Book https://www.trip.com/flights/showfarefirst?dcity=dmk&acity=wmi&ddate=2026-10-27
"""
    route.return_value = httpx.Response(200, text=dmk)
    async with httpx.AsyncClient() as http:
        with caplog.at_level(logging.DEBUG):
            offers = await src.search(query, _ctx(http=http, state=state, env={
                "TRIPGENIE_API_KEY": secret,
            }))
    assert route.call_count == 1
    body = json.loads(route.calls[0].request.content.decode())
    assert body["token"] == secret
    assert body["departure"] == "BKK"
    assert body["arrival"] == "WAW"
    assert body["date"] == "2026-10-27"
    assert body["flight_type"] == "0"
    assert "DMK" in body["query"] and "WMI" in body["query"]
    assert secret not in str(route.calls[0].request.url)
    assert state["cursor"] == 1
    assert len(offers) == 1
    assert offers[0].origin == "DMK"
    assert offers[0].destination == "WMI"
    assert offers[0].link.startswith("https://www.trip.com/flights/showfarefirst?dcity=dmk")
    assert secret not in caplog.text

    route.reset()
    route.return_value = httpx.Response(200, text="Sorry, no flights found")
    async with httpx.AsyncClient() as http:
        second = await src.search(query, _ctx(http=http, state=state))
    assert route.call_count == 1
    body = json.loads(route.calls[0].request.content.decode())
    assert body["date"] == "2026-10-28"
    assert second == []
    assert state["cursor"] == 0


@pytest.mark.asyncio
@respx.mock
async def test_auth_failure_raises_without_leaking_token(
    caplog: pytest.LogCaptureFixture,
) -> None:
    secret = "super-secret-activation"
    respx.post(TRIP_AIRLINE_URL).mock(
        return_value=httpx.Response(200, text="invalid token, please reapply")
    )
    src = TripSource({"max_searches_per_run": 3})
    async with httpx.AsyncClient() as http:
        with caplog.at_level(logging.WARNING):
            with pytest.raises(SourceError, match="activation code rejected"):
                await src.search(
                    _query(),
                    _ctx(http=http, env={"TRIPGENIE_API_KEY": secret}),
                )
    assert secret not in caplog.text


@pytest.mark.asyncio
@respx.mock
async def test_http_failures_raise_source_error() -> None:
    respx.post(TRIP_AIRLINE_URL).mock(
        return_value=httpx.Response(404, text="Not Found! domain not routed")
    )
    src = TripSource({"max_searches_per_run": 2, "concurrency": 1})
    async with httpx.AsyncClient() as http:
        with pytest.raises(SourceError, match="all 2 airline searches failed"):
            await src.search(_query(), _ctx(http=http))


@pytest.mark.asyncio
@respx.mock
async def test_partial_failure_keeps_successful_offers() -> None:
    markdown = FIXTURE.read_text(encoding="utf-8")
    calls = {"n": 0}

    def responder(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        if calls["n"] == 1:
            return httpx.Response(502, text="bad gateway")
        return httpx.Response(200, text=markdown)

    respx.post(TRIP_AIRLINE_URL).mock(side_effect=responder)
    src = TripSource({"max_searches_per_run": 2, "concurrency": 1})
    async with httpx.AsyncClient() as http:
        offers = await src.search(_query(), _ctx(http=http))
    assert offers
    assert offers[0].origin == "CNX"
    assert offers[0].destination == "WAW"
