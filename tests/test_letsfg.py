from __future__ import annotations

import json
import logging
import os
import stat
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs

import httpx
import pytest
import respx

from flightsearch.context import RunContext
from flightsearch.models import SearchQuery
from flightsearch.sources.base import SourceError
from flightsearch.sources import letsfg as letsfg_mod
from flightsearch.sources.letsfg import (
    SEARCH_URL,
    TOKEN_URL,
    LetsFGSource,
    cells_for,
    parse_results,
)

FIXTURES = Path(__file__).resolve().parent / "fixtures" / "letsfg"


class FakeFx:
    def __init__(self, rate: float = 1.1) -> None:
        self.rate = rate
        self.calls: list[tuple[float, str]] = []

    def to_usd(self, amount: float, currency: str) -> float:
        self.calls.append((amount, currency.upper()))
        if currency.upper() == "USD":
            return float(amount)
        return round(float(amount) * self.rate, 2)


class FakeClock:
    def __init__(self, start: float = 1_000.0) -> None:
        self.t = start
        self.sleeps: list[float] = []

    def monotonic(self) -> float:
        return self.t

    async def sleep(self, seconds: float) -> None:
        self.sleeps.append(float(seconds))
        self.t += max(0.0, float(seconds))


def _load(name: str) -> dict[str, Any]:
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


def _query(**overrides: Any) -> SearchQuery:
    base = dict(
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


def _ctx(
    *,
    fx: FakeFx | None = None,
    state: dict | None = None,
    env: dict | None = None,
    now: datetime | None = None,
    http: httpx.AsyncClient | None = None,
) -> RunContext:
    return RunContext(
        http=http or httpx.AsyncClient(),
        fx=fx or FakeFx(),
        state=state if state is not None else {},
        log=logging.getLogger("test.letsfg"),
        now=now or datetime(2026, 9, 25, 12, 0, tzinfo=timezone.utc),
        env=env
        if env is not None
        else {
            "LETSFG_REFRESH_TOKEN": "refresh-token-orig-yyyy",
            "LETSFG_CLIENT_ID": "lfg_client_test",
        },
    )


def _install_clock(monkeypatch: pytest.MonkeyPatch, clock: FakeClock) -> FakeClock:
    monkeypatch.setattr(letsfg_mod, "_monotonic", clock.monotonic)
    monkeypatch.setattr(letsfg_mod, "_sleep", clock.sleep)
    return clock


def _token_route(
    *,
    access: str = "access-token-test-aaaa",
    refresh: str | None = "refresh-token-rotated-zzzz",
) -> respx.Route:
    body: dict[str, Any] = {
        "access_token": access,
        "token_type": "Bearer",
        "expires_in": 3600,
    }
    if refresh is not None:
        body["refresh_token"] = refresh
    return respx.post(TOKEN_URL).mock(return_value=httpx.Response(200, json=body))


def _search_ok(search_id: str = "ws_test_cnx_waw") -> respx.Route:
    return respx.post(SEARCH_URL).mock(
        return_value=httpx.Response(
            200, json={"search_id": search_id, "status": "searching"}
        )
    )


def _results_completed() -> respx.Route:
    return respx.get(url__regex=r"https://letsfg\.co/api/results/.+").mock(
        return_value=httpx.Response(200, json=_load("results_completed.json"))
    )


def test_is_available_requires_refresh_and_client_id() -> None:
    src = LetsFGSource()
    ok, reason = src.is_available({})
    assert ok is False
    assert "LETSFG_REFRESH_TOKEN" in reason
    ok, reason = src.is_available({"LETSFG_REFRESH_TOKEN": "rt"})
    assert ok is False
    assert "LETSFG_CLIENT_ID" in reason
    ok, reason = src.is_available(
        {"LETSFG_REFRESH_TOKEN": "rt", "LETSFG_CLIENT_ID": "cid"}
    )
    assert ok is True
    assert reason == ""


def test_cells_home_origin_only_waw_covers_wmi() -> None:
    query = _query()
    cells = cells_for(query, date(2026, 9, 25))
    origins = {c[0] for c in cells}
    dests = {c[1] for c in cells}
    assert origins == {"CNX"}
    assert dests == {"WAW", "KRK", "GDN", "KTW", "WRO", "POZ"}
    assert "WMI" not in dests
    assert "BKK" not in origins
    assert len(cells) == 6 * 13


def test_parse_results_maps_fields_and_caps_five() -> None:
    fx = FakeFx()
    offers = parse_results(
        _load("results_completed.json"),
        fx=fx,
        requested_origin="CNX",
        requested_dest="WAW",
        requested_date=date(2026, 10, 25),
    )
    assert len(offers) == 5
    cheapest = offers[0]
    assert cheapest.source == "letsfg"
    assert cheapest.origin == "CNX"
    assert cheapest.destination == "WAW"
    assert cheapest.price_original == 180.0
    assert cheapest.currency_original == "EUR"
    assert cheapest.price_usd == 198.0
    assert cheapest.cabin_bag_included is True
    assert cheapest.link and cheapest.link.startswith("https://")

    split = next(o for o in offers if o.destination == "WMI")
    assert split.price_usd == 394.0
    assert split.self_transfer is True
    assert "separate tickets" in split.notes
    assert "arrives WMI" in split.notes
    assert split.flight_numbers == ["VZ119", "SK974", "FR1785", "FR1903"]
    assert split.airlines == ["VZ", "SK", "FR"]
    assert split.stops == 3
    assert split.duration_minutes == 2670
    assert split.cabin_bag_included is True
    assert split.depart_time == datetime(2026, 10, 25, 22, 30)
    assert split.arrive_time == datetime(2026, 10, 27, 13, 0)
    assert (180.0, "EUR") in fx.calls


@pytest.mark.asyncio
@respx.mock
async def test_refresh_rotates_token_to_out_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _install_clock(monkeypatch, FakeClock())
    out = tmp_path / "refresh.out"
    token_route = _token_route()
    _search_ok()
    _results_completed()

    src = LetsFGSource({"max_searches_per_run": 1, "timeout_s": 1800})
    async with httpx.AsyncClient() as http:
        ctx = _ctx(
            http=http,
            env={
                "LETSFG_REFRESH_TOKEN": "refresh-token-orig-yyyy",
                "LETSFG_CLIENT_ID": "lfg_client_test",
                "LETSFG_REFRESH_TOKEN_OUT": str(out),
            },
            state={},
        )
        offers = await src.search(_query(), ctx)

    assert token_route.called
    form = parse_qs(token_route.calls[0].request.content.decode())
    assert form["grant_type"] == ["refresh_token"]
    assert form["refresh_token"] == ["refresh-token-orig-yyyy"]
    assert form["client_id"] == ["lfg_client_test"]
    assert "refresh-token-orig-yyyy" not in str(ctx.state)
    assert "refresh-token-rotated-zzzz" not in str(ctx.state)
    assert out.read_text(encoding="utf-8") == "refresh-token-rotated-zzzz"
    assert stat.S_IMODE(out.stat().st_mode) == 0o600
    assert offers
    assert ctx.state["cursor"] == 1
    assert ctx.state["searches_today"] == 1
    assert ctx.state["day"] == "2026-09-25"


@pytest.mark.asyncio
@respx.mock
async def test_cursor_rotation_and_daily_cap(monkeypatch: pytest.MonkeyPatch) -> None:
    _install_clock(monkeypatch, FakeClock())
    _token_route(refresh=None)
    search = _search_ok()
    _results_completed()
    src = LetsFGSource({"max_searches_per_run": 20, "timeout_s": 1800})
    state: dict = {}
    query = _query()

    async with httpx.AsyncClient() as http:
        ctx = _ctx(http=http, state=state)
        await src.search(query, ctx)
        assert state["searches_today"] == 20
        assert state["cursor"] == 20
        assert search.call_count == 20

        bodies = [json.loads(c.request.content.decode()) for c in search.calls]
        assert bodies[0]["origin"] == "CNX"
        assert bodies[0]["destination"] == "WAW"
        assert bodies[0]["date_from"] == "2026-10-20"
        assert bodies[0]["destination_all_airports"] is True
        assert bodies[0]["response_mode"] == "full"
        assert bodies[1]["destination"] == "KRK"
        assert {b["origin"] for b in bodies} == {"CNX"}
        assert "WMI" not in {b["destination"] for b in bodies}

        # Same UTC day: remaining budget 95-20=75, but run cap is 20.
        await src.search(query, ctx)
        assert state["searches_today"] == 40
        assert state["cursor"] == 40

        state["searches_today"] = 94
        before = search.call_count
        await src.search(query, ctx)
        assert search.call_count == before + 1
        assert state["searches_today"] == 95

        before = search.call_count
        offers = await src.search(query, ctx)
        assert offers == []
        assert search.call_count == before
        assert state["searches_today"] == 95

        ctx_next = _ctx(
            http=http,
            state=state,
            now=datetime(2026, 9, 26, 1, 0, tzinfo=timezone.utc),
        )
        await src.search(query, ctx_next)
        assert state["day"] == "2026-09-26"
        assert state["searches_today"] == 20
        assert state["cursor"] == 61


@pytest.mark.asyncio
@respx.mock
async def test_pacing_waits_after_ten_starts(monkeypatch: pytest.MonkeyPatch) -> None:
    clock = _install_clock(monkeypatch, FakeClock())
    _token_route(refresh=None)
    search = _search_ok()
    _results_completed()
    src = LetsFGSource({"max_searches_per_run": 12, "timeout_s": 1800})
    async with httpx.AsyncClient() as http:
        ctx = _ctx(http=http, state={})
        await src.search(_query(), ctx)

    assert search.call_count == 12
    window_waits = [s for s in clock.sleeps if s >= letsfg_mod.WINDOW_10MIN_S]
    assert window_waits
    assert window_waits[0] == pytest.approx(letsfg_mod.WINDOW_10MIN_S + 0.01)


@pytest.mark.asyncio
@respx.mock
async def test_429_stops_early_keeps_cursor(monkeypatch: pytest.MonkeyPatch) -> None:
    _install_clock(monkeypatch, FakeClock())
    _token_route(refresh=None)
    search = respx.post(SEARCH_URL)
    search.side_effect = [
        httpx.Response(200, json={"search_id": "ws_a", "status": "searching"}),
        httpx.Response(200, json={"search_id": "ws_b", "status": "searching"}),
        httpx.Response(429, text="slow down", headers={"Retry-After": "12"}),
    ]
    _results_completed()
    src = LetsFGSource({"max_searches_per_run": 20, "timeout_s": 1800})
    state: dict = {"cursor": 4, "day": "2026-09-25", "searches_today": 10}
    async with httpx.AsyncClient() as http:
        ctx = _ctx(http=http, state=state)
        offers = await src.search(_query(), ctx)

    assert offers
    assert state["cursor"] == 6
    assert state["searches_today"] == 12
    assert search.call_count == 3


@pytest.mark.asyncio
@respx.mock
async def test_401_refreshes_once_then_retries(monkeypatch: pytest.MonkeyPatch) -> None:
    _install_clock(monkeypatch, FakeClock())
    token = respx.post(TOKEN_URL)
    token.side_effect = [
        httpx.Response(
            200,
            json={"access_token": "access-old", "token_type": "Bearer"},
        ),
        httpx.Response(
            200,
            json={"access_token": "access-new", "token_type": "Bearer"},
        ),
    ]
    search = respx.post(SEARCH_URL)
    search.side_effect = [
        httpx.Response(401, json={"error": "expired"}),
        httpx.Response(200, json={"search_id": "ws_retry", "status": "searching"}),
    ]
    _results_completed()
    src = LetsFGSource({"max_searches_per_run": 1, "timeout_s": 1800})
    async with httpx.AsyncClient() as http:
        ctx = _ctx(http=http, state={})
        offers = await src.search(_query(), ctx)
    assert offers
    assert token.call_count == 2
    assert search.call_count == 2
    assert search.calls[1].request.headers["Authorization"] == "Bearer access-new"


@pytest.mark.asyncio
@respx.mock
async def test_402_raises_source_error(monkeypatch: pytest.MonkeyPatch) -> None:
    _install_clock(monkeypatch, FakeClock())
    _token_route(refresh=None)
    respx.post(SEARCH_URL).mock(return_value=httpx.Response(402, text="pay"))
    src = LetsFGSource({"max_searches_per_run": 1})
    async with httpx.AsyncClient() as http:
        ctx = _ctx(http=http)
        with pytest.raises(SourceError, match="402"):
            await src.search(_query(), ctx)


@pytest.mark.asyncio
async def test_empty_dates_returns_empty() -> None:
    src = LetsFGSource()
    query = _query(date_from=date(2026, 10, 20), date_to=date(2026, 10, 22))
    ctx = _ctx(now=datetime(2026, 11, 10, tzinfo=timezone.utc))
    assert await src.search(query, ctx) == []


@pytest.mark.live
@pytest.mark.asyncio
async def test_live_one_cell() -> None:
    if not os.environ.get("LETSFG_REFRESH_TOKEN"):
        pytest.skip("LETSFG_REFRESH_TOKEN not set")
    if not os.environ.get("LETSFG_CLIENT_ID"):
        pytest.skip("LETSFG_CLIENT_ID not set")
    src = LetsFGSource({"max_searches_per_run": 1, "timeout_s": 300})
    async with httpx.AsyncClient(timeout=60.0) as http:
        ctx = _ctx(
            http=http,
            env={
                "LETSFG_REFRESH_TOKEN": os.environ["LETSFG_REFRESH_TOKEN"],
                "LETSFG_CLIENT_ID": os.environ["LETSFG_CLIENT_ID"],
            },
            state={},
            now=datetime.now(timezone.utc),
        )
        offers = await src.search(
            _query(
                destinations=("WAW",),
                date_from=date(2026, 10, 25),
                date_to=date(2026, 10, 25),
            ),
            ctx,
        )
    assert isinstance(offers, list)
    assert "LETSFG_REFRESH_TOKEN" not in str(ctx.state)
    if offers:
        assert all(o.source == "letsfg" for o in offers)
        assert all(o.origin == "CNX" for o in offers)
