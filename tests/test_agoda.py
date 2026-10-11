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
from flightsearch.sources.agoda import ACTOR_ID, AgodaSource
from flightsearch.sources.apify_routes import run_url
from flightsearch.sources.base import SourceError

FIXTURE = Path(__file__).resolve().parent / "fixtures" / "agoda" / "sample_dataset.json"
APIFY_RUN_URL = run_url(ACTOR_ID)


class _FakeFx:
    def to_usd(self, amount: float, currency: str) -> float:
        if currency.upper() != "USD":
            raise ValueError(currency)
        return float(amount)


def _query(**overrides: object) -> SearchQuery:
    base: dict = dict(
        origins=("BKK", "DMK"),
        destinations=("SGN", "HAN"),
        priority_routes=(("BKK", "SGN"), ("DMK", "HAN")),
        date_from=date(2026, 10, 28),
        date_to=date(2026, 10, 28),
        adults=1,
        cabin_bags=1,
        checked_bags=1,
        max_total_usd=110.0,
        near_miss_usd=130.0,
        currency="USD",
    )
    base.update(overrides)
    return SearchQuery(**base)


def _ctx(*, state: dict | None = None, http: httpx.AsyncClient | None = None) -> RunContext:
    return RunContext(
        http=http or httpx.AsyncClient(),
        fx=_FakeFx(),
        state=state if state is not None else {},
        log=logging.getLogger("test.agoda"),
        now=datetime(2026, 10, 11, 12, 0, tzinfo=timezone.utc),
        env={"APIFY_TOKEN": "test-token-secret"},
    )


def test_load_source_agoda() -> None:
    src = load_source("agoda", {"max_routes_per_day": 4})
    assert isinstance(src, AgodaSource)


def test_is_available_requires_apify_token() -> None:
    ok, reason = AgodaSource().is_available({})
    assert ok is False
    assert "APIFY_TOKEN" in reason


@pytest.mark.asyncio
@respx.mock
async def test_request_shape_and_baggage() -> None:
    items = json.loads(FIXTURE.read_text())
    route = respx.post(APIFY_RUN_URL).mock(return_value=httpx.Response(200, json=items))
    src = AgodaSource({"max_routes_per_day": 4, "max_results": 10, "date_timeout_s": 90})
    state: dict = {}
    async with httpx.AsyncClient() as http:
        offers = await src.search(_query(), _ctx(state=state, http=http))

    assert route.call_count == 1
    body = json.loads(route.calls[0].request.content.decode())
    assert body["origin"] == "BKK"
    assert body["destination"] == "SGN"
    assert body["departDate"] == "2026-10-28"
    assert body["cabin"] == "Economy"
    assert body["maxResults"] == 10
    assert "returnDate" not in body
    assert route.calls[0].request.headers["Authorization"] == "Bearer test-token-secret"

    by_flight = {tuple(o.flight_numbers): o for o in offers}
    booked = by_flight[("VJ805",)]
    assert booked.checked_bag_included is True
    assert booked.cabin_bag_included is True
    assert booked.price_usd == 89.5
    assert booked.depart_time == datetime(2026, 10, 28, 6, 5)
    assert booked.duration_minutes == 95
    assert booked.link.startswith("https://www.agoda.com/")

    light = by_flight[("VJ809",)]
    assert light.checked_bag_included is False

    unknown = next(o for o in offers if o.airlines == ["Thai AirAsia"])
    assert unknown.checked_bag_included is None
    assert unknown.depart_time == datetime(2026, 10, 28, 9, 15)
    assert unknown.duration_minutes == 85
    assert "agoda.com/flights/results" in (unknown.link or "")
    assert all(o.depart_date == date(2026, 10, 28) for o in offers)


@pytest.mark.asyncio
@respx.mock
async def test_blaspat_payload() -> None:
    respx.post(run_url("blaspat~agoda-flights-scraper")).mock(
        return_value=httpx.Response(200, json=[])
    )
    src = AgodaSource({"actor": "blaspat/agoda-flights-scraper", "max_routes_per_day": 4})
    async with httpx.AsyncClient() as http:
        await src.search(_query(), _ctx(http=http))
    body = json.loads(respx.calls[0].request.content.decode())
    assert body["sort"] == "cheapest"
    assert body["departDate"] == "2026-10-28"


@pytest.mark.asyncio
@respx.mock
async def test_insufficient_credit() -> None:
    respx.post(APIFY_RUN_URL).mock(
        return_value=httpx.Response(402, text="Payment required: insufficient credit")
    )
    src = AgodaSource({"max_routes_per_day": 4})
    async with httpx.AsyncClient() as http:
        with pytest.raises(SourceError, match="insufficient credit"):
            await src.search(_query(), _ctx(http=http))
