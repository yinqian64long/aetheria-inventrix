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
from flightsearch.sources.airasia import ACTOR_ID, AirAsiaSource
from flightsearch.sources.apify_routes import run_url
from flightsearch.sources.base import SourceError

FIXTURE = Path(__file__).resolve().parent / "fixtures" / "airasia" / "sample_dataset.json"
APIFY_RUN_URL = run_url(ACTOR_ID)


class _FakeFx:
    def to_usd(self, amount: float, currency: str) -> float:
        return float(amount)


def _query() -> SearchQuery:
    return SearchQuery(
        origins=("DMK",),
        destinations=("HAN",),
        priority_routes=(("DMK", "HAN"),),
        date_from=date(2026, 10, 29),
        date_to=date(2026, 10, 29),
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
        log=logging.getLogger("test.airasia"),
        now=datetime(2026, 10, 11, 12, 0, tzinfo=timezone.utc),
        env={"APIFY_TOKEN": "test-token-secret"},
    )


@pytest.mark.asyncio
@respx.mock
async def test_request_shape_baggage_and_report_skipped() -> None:
    items = json.loads(FIXTURE.read_text())
    route = respx.post(APIFY_RUN_URL).mock(return_value=httpx.Response(200, json=items))
    src = AirAsiaSource({"max_routes_per_day": 4, "max_results": 10, "date_timeout_s": 90})
    async with httpx.AsyncClient() as http:
        offers = await src.search(_query(), _ctx(http=http))

    body = json.loads(route.calls[0].request.content.decode())
    assert body["origin"] == "DMK"
    assert body["destination"] == "HAN"
    assert body["departureDate"] == "2026-10-29"
    assert body["currency"] == "USD"
    assert body["countryCode"] == "TH"
    assert body["maxResults"] == 10
    assert "returnDate" not in body

    booked = next(o for o in offers if o.flight_numbers == ["FD614"])
    assert booked.checked_bag_included is True
    assert booked.cabin_bag_included is True
    assert booked.price_usd == 92.4
    assert booked.depart_time == datetime(2026, 10, 29, 7, 10)
    assert booked.duration_minutes == 110
    assert booked.link.startswith("https://www.airasia.com/")
    assert booked.self_transfer is False

    bare = next(o for o in offers if not o.flight_numbers)
    assert bare.checked_bag_included is False
    assert bare.cabin_bag_included is True
    assert len(offers) == 2


@pytest.mark.asyncio
@respx.mock
async def test_all_routes_failed() -> None:
    respx.post(APIFY_RUN_URL).mock(return_value=httpx.Response(500, text="boom"))
    src = AirAsiaSource({"max_routes_per_day": 4})
    async with httpx.AsyncClient() as http:
        with pytest.raises(SourceError, match="all 1 route"):
            await src.search(_query(), _ctx(http=http))
