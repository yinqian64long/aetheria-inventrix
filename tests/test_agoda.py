from __future__ import annotations

import asyncio
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
from flightsearch.sources.apify_routes import (
    DEFAULT_APIFY_MAX_CONCURRENT_RUNS,
    configure_apify_concurrency,
    run_url,
)
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


def _ctx(
    *,
    state: dict | None = None,
    http: httpx.AsyncClient | None = None,
    env: dict | None = None,
) -> RunContext:
    return RunContext(
        http=http or httpx.AsyncClient(),
        fx=_FakeFx(),
        state=state if state is not None else {},
        log=logging.getLogger("test.agoda"),
        now=datetime(2026, 10, 11, 12, 0, tzinfo=timezone.utc),
        env=env if env is not None else {"APIFY_TOKEN": "test-token-secret"},
    )


def test_load_source_agoda() -> None:
    src = load_source("agoda", {"max_routes_per_day": 4})
    assert isinstance(src, AgodaSource)


def test_is_available_requires_apify_token() -> None:
    ok, reason = AgodaSource().is_available({})
    assert ok is False
    assert "APIFY_TOKEN" in reason
    ok, reason = AgodaSource().is_available({"APIFY_TOKEN_BACKUP": "backup-token"})
    assert ok is True
    assert reason == ""


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


def _no_backoff(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        "flightsearch.sources.apify_routes._CAPACITY_BACKOFF_S",
        (0.0, 0.0, 0.0),
    )


def _memory_limit() -> httpx.Response:
    return httpx.Response(
        402,
        json={
            "error": {
                "type": "actor-memory-limit-exceeded",
                "message": "By launching this job you will exceed the memory limit",
            }
        },
    )


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


@pytest.mark.asyncio
@respx.mock
async def test_credit_error_includes_apify_type() -> None:
    route = respx.post(APIFY_RUN_URL).mock(
        return_value=httpx.Response(
            402,
            json={
                "error": {
                    "type": "not-enough-usage-to-run-paid-actor",
                    "message": "You do not have enough usage to run this Actor",
                }
            },
        )
    )
    src = AgodaSource({"max_routes_per_day": 4})
    async with httpx.AsyncClient() as http:
        with pytest.raises(SourceError, match="not-enough-usage-to-run-paid-actor") as exc:
            await src.search(_query(), _ctx(http=http))
    assert "enough usage" in str(exc.value)
    assert route.call_count == 1


def _credit_response() -> httpx.Response:
    return httpx.Response(
        402,
        json={
            "error": {
                "type": "not-enough-usage-to-run-paid-actor",
                "message": "You do not have enough usage to run this Actor",
            }
        },
    )


@pytest.mark.asyncio
@respx.mock
async def test_credit_exhaustion_switches_to_backup_token(
    caplog: pytest.LogCaptureFixture,
) -> None:
    primary = "primary-apify-token-aaa"
    backup = "backup-apify-token-bbb"

    def handler(request: httpx.Request) -> httpx.Response:
        if request.headers["Authorization"] == f"Bearer {primary}":
            return _credit_response()
        return httpx.Response(200, json=[])

    route = respx.post(APIFY_RUN_URL).mock(side_effect=handler)
    src = AgodaSource({"max_routes_per_day": 4})
    env = {"APIFY_TOKEN": primary, "APIFY_TOKEN_BACKUP": backup}
    with caplog.at_level(logging.WARNING, logger="test.agoda"):
        async with httpx.AsyncClient() as http:
            offers = await src.search(_query(), _ctx(http=http, env=env))
            assert offers == []
            assert route.call_count == 2
            assert route.calls[0].request.headers["Authorization"] == f"Bearer {primary}"
            assert route.calls[1].request.headers["Authorization"] == f"Bearer {backup}"
            await src.search(_query(), _ctx(http=http, env=env, state={}))
    assert route.call_count == 3
    assert route.calls[2].request.headers["Authorization"] == f"Bearer {backup}"
    joined = "\n".join(record.message for record in caplog.records)
    assert "switching to backup API token" in joined
    assert primary not in joined
    assert backup not in joined


@pytest.mark.asyncio
@respx.mock
async def test_backup_token_credit_error_still_raises() -> None:
    route = respx.post(APIFY_RUN_URL).mock(return_value=_credit_response())
    src = AgodaSource({"max_routes_per_day": 4})
    env = {
        "APIFY_TOKEN": "primary-apify-token-ccc",
        "APIFY_TOKEN_BACKUP": "backup-apify-token-ddd",
    }
    async with httpx.AsyncClient() as http:
        with pytest.raises(SourceError, match="not-enough-usage-to-run-paid-actor"):
            await src.search(_query(), _ctx(http=http, env=env))
    assert route.call_count == 2
    assert [call.request.headers["Authorization"] for call in route.calls] == [
        "Bearer primary-apify-token-ccc",
        "Bearer backup-apify-token-ddd",
    ]


@pytest.mark.asyncio
@respx.mock
async def test_memory_limit_is_retried_then_succeeds(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _no_backoff(monkeypatch)
    route = respx.post(APIFY_RUN_URL)
    route.side_effect = [_memory_limit(), httpx.Response(200, json=[])]
    src = AgodaSource({"max_routes_per_day": 4})
    async with httpx.AsyncClient() as http:
        offers = await src.search(_query(), _ctx(http=http))
    assert offers == []
    assert route.call_count == 2


@pytest.mark.asyncio
@respx.mock
async def test_memory_limit_is_not_reported_as_credit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _no_backoff(monkeypatch)
    route = respx.post(APIFY_RUN_URL).mock(side_effect=lambda request: _memory_limit())
    src = AgodaSource({"max_routes_per_day": 4})
    async with httpx.AsyncClient() as http:
        with pytest.raises(SourceError, match="actor-memory-limit-exceeded") as exc:
            await src.search(_query(), _ctx(http=http))
    assert "insufficient credit" not in str(exc.value)
    assert route.call_count == 4


@pytest.mark.asyncio
@respx.mock
async def test_insufficient_permissions_is_not_a_credit_error() -> None:
    route = respx.post(APIFY_RUN_URL).mock(
        return_value=httpx.Response(403, text="insufficient permissions")
    )
    src = AgodaSource({"max_routes_per_day": 4})
    async with httpx.AsyncClient() as http:
        with pytest.raises(SourceError, match="all 1 route") as exc:
            await src.search(_query(), _ctx(http=http))
    assert "insufficient permissions" in str(exc.value)
    assert "insufficient credit" not in str(exc.value)
    assert route.call_count == 1


@pytest.mark.asyncio
async def test_apify_runs_stay_within_concurrency_cap() -> None:
    configure_apify_concurrency(2)
    current = 0
    peak = 0

    async def handler(request: httpx.Request) -> httpx.Response:
        nonlocal current, peak
        current += 1
        peak = max(peak, current)
        try:
            await asyncio.sleep(0.05)
        finally:
            current -= 1
        return httpx.Response(200, json=[])

    transport = httpx.MockTransport(handler)
    src = AgodaSource({"max_routes_per_day": 4})
    try:
        async with httpx.AsyncClient(transport=transport) as http:
            await asyncio.gather(
                *(
                    src.search(_query(), _ctx(state={}, http=http))
                    for _ in range(4)
                )
            )
    finally:
        configure_apify_concurrency(DEFAULT_APIFY_MAX_CONCURRENT_RUNS)
    assert peak == 2
    assert current == 0
