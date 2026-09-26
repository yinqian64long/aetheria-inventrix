from __future__ import annotations

import json
import logging
import os
from datetime import date, datetime, timezone
from pathlib import Path

import httpx
import pytest
import respx

from flightsearch.context import RunContext
from flightsearch.models import SearchQuery
from flightsearch.sources.base import SourceError
from flightsearch.sources.skyscanner import APIFY_RUN_URL, SkyscannerSource

FIXTURE = (
    Path(__file__).resolve().parent / "fixtures" / "skyscanner" / "sample_dataset.json"
)


class _FakeFx:
    rates = {"USD": 1.0, "EUR": 1.1}

    def to_usd(self, amount: float, currency: str) -> float:
        rate = self.rates.get(currency.upper())
        if rate is None:
            raise ValueError(f"unknown currency {currency}")
        return amount * rate


def _query(**overrides: object) -> SearchQuery:
    base: dict = dict(
        home_origin="CNX",
        positioning_origins=("BKK",),
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
        log=logging.getLogger("test.skyscanner"),
        now=now or datetime(2026, 9, 25, 12, 0, tzinfo=timezone.utc),
        env=env if env is not None else {"APIFY_TOKEN": "test-token-secret"},
    )


def test_is_available_requires_token() -> None:
    src = SkyscannerSource()
    ok, reason = src.is_available({})
    assert ok is False
    assert "APIFY_TOKEN" in reason
    ok, reason = src.is_available({"APIFY_TOKEN": "  "})
    assert ok is False
    ok, reason = src.is_available({"APIFY_TOKEN": "tok"})
    assert ok is True
    assert reason == ""


@pytest.mark.asyncio
@respx.mock
async def test_request_shape_auth_header_and_parsing() -> None:
    items = json.loads(FIXTURE.read_text())
    route = respx.post(APIFY_RUN_URL).mock(
        return_value=httpx.Response(200, json=items)
    )
    src = SkyscannerSource({"max_runs_per_day": 6, "timeout_s": 1200})
    state: dict = {}
    async with httpx.AsyncClient() as http:
        ctx = _ctx(state=state, http=http)
        offers = await src.search(_query(), ctx)

    assert route.called
    # ceil(6/4) == 2 actor runs per pipeline invocation
    assert route.call_count == 2
    req = route.calls[0].request
    assert req.headers["Authorization"] == "Bearer test-token-secret"
    assert "token=" not in str(req.url)
    assert "test-token-secret" not in str(req.url)
    body = json.loads(req.content.decode())
    assert body["origin"] == "CNX"
    assert body["destination"] == "WAW"
    assert body["departDate"] == "2026-10-20"
    assert body["departDateEnd"] == "2026-11-01"
    assert body["adults"] == 1
    assert body["currency"] == "USD"
    assert body["maxFlights"] == 30
    assert body["cabinClass"] == "ECONOMY"
    assert req.url.params.get("timeout") == "1200"

    assert state["day"] == "2026-09-25"
    assert state["runs_today"] == 2
    assert state["cursor"] == 2

    assert len(offers) >= 1
    cheap = next(o for o in offers if o.destination == "WAW" and o.price_usd == 130.0)
    assert cheap.source == "skyscanner"
    assert cheap.origin == "CNX"
    assert cheap.depart_date == date(2026, 10, 22)
    assert cheap.depart_time == datetime(2026, 10, 22, 6, 0)
    assert cheap.arrive_time == datetime(2026, 10, 22, 14, 32)
    assert cheap.airlines == ["American Airlines"]
    assert cheap.flight_numbers == ["AA123"]
    assert cheap.stops == 0
    assert cheap.duration_minutes == 332
    assert cheap.cabin_bag_included is True
    assert cheap.self_transfer is False
    assert cheap.link == "https://www.skyscanner.net/transport/flights/cnx/waw/261022/"
    assert "travelpayouts" in cheap.notes

    eur = next(o for o in offers if o.destination == "KRK")
    assert eur.price_original == 245.5
    assert eur.currency_original == "EUR"
    assert eur.price_usd == pytest.approx(270.05)
    assert eur.self_transfer is True
    assert eur.cabin_bag_included is False
    assert eur.flight_numbers == ["QR835", "LO174"]
    assert eur.stops == 1


@pytest.mark.asyncio
@respx.mock
async def test_budget_rotation_and_day_rollover() -> None:
    respx.post(APIFY_RUN_URL).mock(return_value=httpx.Response(200, json=[]))
    src = SkyscannerSource({"max_runs_per_day": 6, "timeout_s": 60})
    state: dict = {}
    query = _query()

    async with httpx.AsyncClient() as http:
        # Day 1: four pipeline runs × 2 actor runs = 6, then exhausted
        for expected_runs in (2, 4, 6, 6):
            ctx = _ctx(
                state=state,
                http=http,
                now=datetime(2026, 9, 25, 10, 0, tzinfo=timezone.utc),
            )
            await src.search(query, ctx)
            assert state["runs_today"] == expected_runs

        assert state["cursor"] == 6
        assert respx.calls.call_count == 6

        before = respx.calls.call_count
        ctx = _ctx(
            state=state,
            http=http,
            now=datetime(2026, 9, 25, 22, 0, tzinfo=timezone.utc),
        )
        offers = await src.search(query, ctx)
        assert offers == []
        assert respx.calls.call_count == before
        assert state["runs_today"] == 6

        # Next UTC day: budget resets; cursor continues (WAW then KRK)
        ctx = _ctx(
            state=state,
            http=http,
            now=datetime(2026, 9, 26, 1, 0, tzinfo=timezone.utc),
        )
        await src.search(query, ctx)
        assert state["day"] == "2026-09-26"
        assert state["runs_today"] == 2
        assert state["cursor"] == 8
        bodies = [
            json.loads(c.request.content.decode())
            for c in respx.calls[-2:]
        ]
        assert bodies[0]["destination"] == "WAW"
        assert bodies[1]["destination"] == "KRK"


@pytest.mark.asyncio
@respx.mock
async def test_http_error_all_runs_raise_source_error() -> None:
    respx.post(APIFY_RUN_URL).mock(return_value=httpx.Response(500, text="boom"))
    src = SkyscannerSource({"max_runs_per_day": 4})
    async with httpx.AsyncClient() as http:
        ctx = _ctx(http=http)
        with pytest.raises(SourceError, match="all 1 actor run"):
            await src.search(_query(), ctx)


@pytest.mark.asyncio
@respx.mock
async def test_402_insufficient_credit() -> None:
    respx.post(APIFY_RUN_URL).mock(
        return_value=httpx.Response(402, text="Payment required: insufficient credit")
    )
    src = SkyscannerSource({"max_runs_per_day": 6})
    async with httpx.AsyncClient() as http:
        ctx = _ctx(http=http)
        with pytest.raises(SourceError, match="insufficient credit"):
            await src.search(_query(), ctx)


@pytest.mark.asyncio
@respx.mock
async def test_partial_failure_returns_parsed_offers() -> None:
    items = json.loads(FIXTURE.read_text())[:1]
    route = respx.post(APIFY_RUN_URL)
    route.side_effect = [
        httpx.Response(503, text="unavailable"),
        httpx.Response(200, json=items),
    ]
    src = SkyscannerSource({"max_runs_per_day": 6})
    async with httpx.AsyncClient() as http:
        ctx = _ctx(http=http)
        offers = await src.search(_query(), ctx)
    assert len(offers) == 1
    assert offers[0].destination == "WAW"


@pytest.mark.asyncio
@respx.mock
async def test_token_never_logged(caplog: pytest.LogCaptureFixture) -> None:
    respx.post(APIFY_RUN_URL).mock(return_value=httpx.Response(200, json=[]))
    src = SkyscannerSource({"max_runs_per_day": 6})
    secret = "super-secret-apify-token-xyz"
    with caplog.at_level(logging.DEBUG, logger="test.skyscanner"):
        async with httpx.AsyncClient() as http:
            ctx = _ctx(http=http, env={"APIFY_TOKEN": secret})
            await src.search(_query(), ctx)
    joined = "\n".join(r.message for r in caplog.records)
    assert secret not in joined


@pytest.mark.live
@pytest.mark.asyncio
async def test_live_apify_one_pair() -> None:
    if not os.environ.get("APIFY_TOKEN"):
        pytest.skip("APIFY_TOKEN not set")
    src = SkyscannerSource({"max_runs_per_day": 1, "timeout_s": 300})
    async with httpx.AsyncClient() as http:
        ctx = _ctx(
            http=http,
            env={"APIFY_TOKEN": os.environ["APIFY_TOKEN"]},
            state={},
            now=datetime.now(timezone.utc),
        )
        offers = await src.search(
            _query(
                destinations=("WAW",),
                date_from=date(2026, 10, 20),
                date_to=date(2026, 10, 22),
            ),
            ctx,
        )
    assert isinstance(offers, list)
