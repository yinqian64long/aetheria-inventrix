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
from flightsearch.sources.apify_routes import run_url
from flightsearch.sources.base import SourceError
from flightsearch.sources.vietjet import ACTOR_ID, VietjetSource

FIXTURE = Path(__file__).resolve().parent / "fixtures" / "vietjet" / "sample_dataset.json"
APIFY_RUN_URL = run_url(ACTOR_ID)


class _FakeFx:
    def to_usd(self, amount: float, currency: str) -> float:
        return float(amount)


def _query() -> SearchQuery:
    return SearchQuery(
        origins=("BKK", "CNX"),
        destinations=("SGN", "HAN"),
        priority_routes=(("BKK", "SGN"),),
        date_from=date(2026, 10, 28),
        date_to=date(2026, 10, 28),
        adults=1,
        cabin_bags=1,
        checked_bags=1,
        max_total_usd=110.0,
        near_miss_usd=130.0,
        currency="USD",
    )


def _ctx(*, http: httpx.AsyncClient) -> RunContext:
    return RunContext(
        http=http,
        fx=_FakeFx(),
        state={},
        log=logging.getLogger("test.vietjet"),
        now=datetime(2026, 10, 11, 12, 0, tzinfo=timezone.utc),
        env={"APIFY_TOKEN": "test-token-secret"},
    )


@pytest.mark.asyncio
@respx.mock
async def test_keeps_only_vietjet_and_prefers_vj() -> None:
    items = json.loads(FIXTURE.read_text())
    route = respx.post(APIFY_RUN_URL).mock(return_value=httpx.Response(200, json=items))
    src = VietjetSource({"max_routes_per_day": 4, "max_results": 10, "date_timeout_s": 90})
    async with httpx.AsyncClient() as http:
        offers = await src.search(_query(), _ctx(http=http))

    body = json.loads(route.calls[0].request.content.decode())
    assert body["tripType"] == "oneWay"
    assert body["origin"] == "BKK"
    assert body["destination"] == "SGN"
    assert body["departureDate"] == "2026-10-28"
    assert body["preferredAirlines"] == ["VJ", "VietJet Air"]
    assert body["includeBaggageEstimate"] is True
    assert body["maxResults"] == 10

    assert len(offers) == 1
    offer = offers[0]
    assert offer.flight_numbers == ["VJ805"]
    assert offer.price_usd == 79.0
    assert offer.checked_bag_included is True
    assert offer.cabin_bag_included is True
    assert offer.depart_time == datetime(2026, 10, 28, 6, 5)
    assert offer.arrive_time == datetime(2026, 10, 28, 7, 40)
    assert offer.link.startswith("https://www.vietjetair.com/")
    assert "TG" not in offer.airlines


@pytest.mark.asyncio
@respx.mock
async def test_insufficient_credit() -> None:
    respx.post(APIFY_RUN_URL).mock(
        return_value=httpx.Response(402, text="insufficient credit")
    )
    src = VietjetSource({"max_routes_per_day": 4})
    async with httpx.AsyncClient() as http:
        with pytest.raises(SourceError, match="insufficient credit"):
            await src.search(_query(), _ctx(http=http))
