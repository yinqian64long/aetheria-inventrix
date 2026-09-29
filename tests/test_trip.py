from __future__ import annotations

import json
import logging
from datetime import date, datetime, timezone
from pathlib import Path

import httpx
import pytest
import respx

from flightsearch.context import RunContext
from flightsearch.models import SearchQuery
from flightsearch.sources import load_source
from flightsearch.sources.base import SourceError
from flightsearch.sources.trip import ACTOR_ID, TripSource, route_pairs, run_url

FIXTURE = Path(__file__).resolve().parent / "fixtures" / "trip" / "sample_dataset.json"
APIFY_RUN_URL = run_url(ACTOR_ID)


class _FakeFx:
    rates = {"USD": 1.0, "THB": 0.03}

    def to_usd(self, amount: float, currency: str) -> float:
        rate = self.rates.get(currency.upper())
        if rate is None:
            raise ValueError(f"unknown currency {currency}")
        return amount * rate


def _query(**overrides: object) -> SearchQuery:
    base: dict = dict(
        home_origin="CNX",
        positioning_origins=("BKK",),
        destinations=("WAW", "KRK"),
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
    state: dict | None = None,
    env: dict | None = None,
    now: datetime | None = None,
    http: httpx.AsyncClient | None = None,
) -> RunContext:
    return RunContext(
        http=http or httpx.AsyncClient(),
        fx=_FakeFx(),
        state=state if state is not None else {},
        log=logging.getLogger("test.trip"),
        now=now or datetime(2026, 9, 29, 12, 0, tzinfo=timezone.utc),
        env=env if env is not None else {"APIFY_TOKEN": "test-token-secret"},
    )


def test_load_source_trip() -> None:
    src = load_source("trip", {"max_routes_per_day": 4})
    assert isinstance(src, TripSource)
    assert src.name == "trip"


def test_is_available_requires_apify_token() -> None:
    src = TripSource()
    ok, reason = src.is_available({})
    assert ok is False
    assert "APIFY_TOKEN" in reason
    ok, reason = src.is_available({"APIFY_TOKEN": "  "})
    assert ok is False
    ok, reason = src.is_available({"APIFY_TOKEN": "tok"})
    assert ok is True
    assert reason == ""


def test_route_pairs_include_wmi_and_keep_dmk() -> None:
    query = _query(
        positioning_origins=("BKK", "DMK", "HKT"),
        destinations=("WAW", "KRK"),
        extra_destinations=("WMI",),
    )
    pairs = route_pairs(query)
    assert pairs[0] == ("CNX", "WAW")
    assert ("CNX", "WMI") in pairs
    assert ("DMK", "WAW") in pairs
    assert ("DMK", "WMI") not in pairs
    assert ("BKK", "KRK") in pairs


@pytest.mark.asyncio
@respx.mock
async def test_request_shape_and_parsing() -> None:
    items = json.loads(FIXTURE.read_text())
    route = respx.post(APIFY_RUN_URL).mock(
        return_value=httpx.Response(200, json=items)
    )
    src = TripSource({"max_routes_per_day": 4, "max_results": 8, "date_timeout_s": 90})
    state: dict = {}
    query = _query(destinations=("WAW",), positioning_origins=(), date_to=date(2026, 10, 27))
    async with httpx.AsyncClient() as http:
        offers = await src.search(query, _ctx(state=state, http=http))

    assert route.call_count == 1
    req = route.calls[0].request
    assert req.headers["Authorization"] == "Bearer test-token-secret"
    assert "token=" not in str(req.url)
    assert "test-token-secret" not in str(req.url)
    body = json.loads(req.content.decode())
    assert body["service"] == "flights"
    assert body["origin"] == "CNX"
    assert body["destination"] == "WAW"
    assert body["departureDate"] == "2026-10-27"
    assert body["tripType"] == "oneway"
    assert body["adults"] == 1
    assert body["currency"] == "USD"
    assert body["maxResults"] == 8
    assert body["includeSponsored"] is False
    assert body["enrichDetails"] is False
    assert req.url.params.get("timeout") == "90"
    assert state["routes_today"] == 1
    assert state["route_cursor"] == 1

    assert [o.flight_numbers[0] for o in offers] == ["EY427", "LO68"]
    ey = next(o for o in offers if o.flight_numbers == ["EY427"])
    assert ey.source == "trip"
    assert ey.origin == "CNX"
    assert ey.destination == "WAW"
    assert ey.depart_date == date(2026, 10, 27)
    assert ey.depart_time == datetime(2026, 10, 27, 9, 10)
    assert ey.arrive_time == datetime(2026, 10, 28, 6, 20)
    assert ey.price_usd == 210.0
    assert ey.airlines == ["Etihad"]
    assert ey.stops == 1
    assert ey.duration_minutes == 850
    assert ey.cabin_bag_included is True
    assert ey.self_transfer is False
    assert ey.notes == ""
    assert ey.link.startswith("https://www.trip.com/flights/showfarefirst?")

    lot = next(o for o in offers if o.flight_numbers == ["LO68"])
    assert lot.price_original == 9000
    assert lot.currency_original == "THB"
    assert lot.price_usd == pytest.approx(270.0)
    assert lot.stops == 0
    assert lot.self_transfer is True
    assert lot.cabin_bag_included is None
    assert lot.notes == "bags not verified (Trip.com)"
    assert "dcity=cnx" in lot.link
    assert "ddate=2026-10-27" in lot.link


@pytest.mark.asyncio
@respx.mock
async def test_window_fans_out_one_date_per_actor_run() -> None:
    respx.post(APIFY_RUN_URL).mock(return_value=httpx.Response(200, json=[]))
    src = TripSource({"max_routes_per_day": 4})
    async with httpx.AsyncClient() as http:
        await src.search(
            _query(destinations=("WAW",), positioning_origins=()),
            _ctx(http=http),
        )
    dates = sorted(
        json.loads(call.request.content.decode())["departureDate"]
        for call in respx.calls
    )
    assert dates == ["2026-10-27", "2026-10-28"]


@pytest.mark.asyncio
@respx.mock
async def test_budget_rotates_routes_and_resets_next_day() -> None:
    respx.post(APIFY_RUN_URL).mock(return_value=httpx.Response(200, json=[]))
    src = TripSource({"max_routes_per_day": 4})
    state: dict = {}
    query = _query()
    async with httpx.AsyncClient() as http:
        for expected in (1, 2, 3, 4):
            await src.search(
                query,
                _ctx(
                    state=state,
                    http=http,
                    now=datetime(2026, 9, 29, 10, 0, tzinfo=timezone.utc),
                ),
            )
            assert state["routes_today"] == expected
        assert state["route_cursor"] == 4
        assert respx.calls.call_count == 8

        before = respx.calls.call_count
        await src.search(
            query,
            _ctx(
                state=state,
                http=http,
                now=datetime(2026, 9, 29, 22, 0, tzinfo=timezone.utc),
            ),
        )
        assert respx.calls.call_count == before

        await src.search(
            query,
            _ctx(
                state=state,
                http=http,
                now=datetime(2026, 9, 30, 1, 0, tzinfo=timezone.utc),
            ),
        )
    assert state["day"] == "2026-09-30"
    assert state["routes_today"] == 1
    assert state["route_cursor"] == 5
    bodies = [
        json.loads(call.request.content.decode()) for call in respx.calls[-2:]
    ]
    assert {body["origin"] for body in bodies} == {"CNX"}
    assert {body["destination"] for body in bodies} == {"WAW"}


@pytest.mark.asyncio
@respx.mock
async def test_http_error_all_routes_raise() -> None:
    respx.post(APIFY_RUN_URL).mock(return_value=httpx.Response(500, text="boom"))
    src = TripSource({"max_routes_per_day": 4})
    async with httpx.AsyncClient() as http:
        with pytest.raises(SourceError, match="all 1 route"):
            await src.search(
                _query(destinations=("WAW",), positioning_origins=()),
                _ctx(http=http),
            )


@pytest.mark.asyncio
@respx.mock
async def test_402_insufficient_credit() -> None:
    respx.post(APIFY_RUN_URL).mock(
        return_value=httpx.Response(402, text="Payment required: insufficient credit")
    )
    src = TripSource({"max_routes_per_day": 4})
    async with httpx.AsyncClient() as http:
        with pytest.raises(SourceError, match="insufficient credit"):
            await src.search(_query(), _ctx(http=http))


@pytest.mark.asyncio
@respx.mock
async def test_partial_date_failure_keeps_offers() -> None:
    items = json.loads(FIXTURE.read_text())[:1]

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content.decode())
        if body["departureDate"] == "2026-10-27":
            return httpx.Response(503, text="unavailable")
        return httpx.Response(200, json=items)

    respx.post(APIFY_RUN_URL).mock(side_effect=handler)
    src = TripSource({"max_routes_per_day": 4})
    async with httpx.AsyncClient() as http:
        offers = await src.search(
            _query(destinations=("WAW",), positioning_origins=()),
            _ctx(http=http),
        )
    assert len(offers) == 1
    assert offers[0].flight_numbers == ["EY427"]
    assert offers[0].depart_date == date(2026, 10, 27)


@pytest.mark.asyncio
@respx.mock
async def test_token_never_logged(caplog: pytest.LogCaptureFixture) -> None:
    respx.post(APIFY_RUN_URL).mock(return_value=httpx.Response(200, json=[]))
    secret = "super-secret-apify-token-xyz"
    src = TripSource({"max_routes_per_day": 4})
    with caplog.at_level(logging.DEBUG, logger="test.trip"):
        async with httpx.AsyncClient() as http:
            await src.search(
                _query(destinations=("WAW",), positioning_origins=()),
                _ctx(http=http, env={"APIFY_TOKEN": secret}),
            )
    joined = "\n".join(record.message for record in caplog.records)
    assert secret not in joined


@pytest.mark.asyncio
@respx.mock
async def test_caps_twenty_per_route() -> None:
    items = [
        {
            "service": "flights",
            "origin": "CNX",
            "destination": "WAW",
            "departureDate": "2026-10-27",
            "price": 100 + i,
            "currency": "USD",
            "flightNumber": f"LO{i}",
        }
        for i in range(25)
    ]
    respx.post(APIFY_RUN_URL).mock(return_value=httpx.Response(200, json=items))
    src = TripSource({"max_routes_per_day": 4})
    async with httpx.AsyncClient() as http:
        offers = await src.search(
            _query(
                destinations=("WAW",),
                positioning_origins=(),
                date_to=date(2026, 10, 27),
            ),
            _ctx(http=http),
        )
    assert len(offers) == 20
    assert offers[0].price_usd == 100
    assert offers[-1].price_usd == 119


@pytest.mark.asyncio
async def test_window_already_over_makes_no_requests() -> None:
    src = TripSource()
    async with httpx.AsyncClient() as http:
        offers = await src.search(
            _query(),
            _ctx(http=http, now=datetime(2026, 12, 1, tzinfo=timezone.utc)),
        )
    assert offers == []
