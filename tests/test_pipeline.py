from __future__ import annotations

import asyncio
from collections.abc import Mapping
from datetime import date, datetime, timezone
from pathlib import Path
from typing import ClassVar

import httpx
import pytest
import respx

from flightsearch.config import AppConfig
from flightsearch.context import RunContext
from flightsearch.fx import FRANKFURTER_URL
from flightsearch.models import Offer, SearchQuery
from flightsearch.pipeline import run_search
from flightsearch.report import format_deals
from flightsearch.sources.base import Source, SourceError
from flightsearch.state import State

NOW = datetime(2026, 10, 21, 8, 0, tzinfo=timezone.utc)


def _query() -> SearchQuery:
    return SearchQuery(
        home_origin="CNX",
        positioning_origins=("BKK", "DMK", "HKT"),
        destinations=("WAW", "KRK"),
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


def _config(sources: dict | None = None) -> AppConfig:
    return AppConfig(
        query=_query(),
        hop_min_connection_minutes=240,
        hop_max_connection_hours=24,
        sources=sources
        or {
            "kiwi": {"enabled": True},
            "google": {"enabled": True},
        },
        notify={"telegram": {"enabled": True, "heartbeat_utc_hour": 0}},
    )


def _offer(
    *,
    source: str,
    origin: str = "CNX",
    destination: str = "WAW",
    depart_date: date = date(2026, 10, 25),
    price_usd: float = 200.0,
    depart_time: datetime | None = datetime(2026, 10, 25, 10, 0),
    flight_numbers: list[str] | None = None,
) -> Offer:
    return Offer(
        source=source,
        origin=origin,
        destination=destination,
        depart_date=depart_date,
        price_usd=price_usd,
        price_original=price_usd,
        currency_original="USD",
        depart_time=depart_time,
        flight_numbers=flight_numbers or ["LO68"],
    )


class FakeSource(Source):
    name: ClassVar[str] = "fake"

    def __init__(self, settings: dict | None = None) -> None:
        super().__init__(settings)
        self._offers = list(settings.get("offers") or []) if settings else []
        self._error = settings.get("error") if settings else None
        self._available = settings.get("available", True) if settings else True
        self._reason = settings.get("reason", "") if settings else ""

    def is_available(self, env: Mapping[str, str]) -> tuple[bool, str]:
        return bool(self._available), str(self._reason)

    async def search(self, query: SearchQuery, ctx: RunContext) -> list[Offer]:
        ctx.state["seen"] = ctx.state.get("seen", 0) + 1
        if self._error:
            raise SourceError(self._error)
        return list(self._offers)


class FakeNotifier:
    def __init__(self) -> None:
        self.sent: list[str] = []

    async def send(self, text: str, parse_mode: str = "HTML") -> None:
        self.sent.append(text)


def _install_sources(monkeypatch: pytest.MonkeyPatch, mapping: dict[str, FakeSource]) -> None:
    def fake_load(name: str, settings: dict) -> Source:
        if name not in mapping:
            raise KeyError(name)
        return mapping[name]

    monkeypatch.setattr("flightsearch.pipeline.load_source", fake_load)


def _patch_hops(monkeypatch: pytest.MonkeyPatch, hops: list[Offer] | None = None) -> None:
    async def fake(query: SearchQuery, ctx: RunContext) -> list[Offer]:
        return list(hops or [])

    monkeypatch.setattr("flightsearch.pipeline.fetch_hops", fake)


@respx.mock
async def _run(monkeypatch, **kwargs):
    respx.get(FRANKFURTER_URL).mock(
        return_value=httpx.Response(200, json={"rates": {"EUR": 0.9, "PLN": 4.0}})
    )
    hops_fn = kwargs.pop("hops_fn", None)
    if hops_fn is not None:
        monkeypatch.setattr("flightsearch.pipeline.fetch_hops", hops_fn)
    else:
        _patch_hops(monkeypatch, kwargs.pop("hops", []))
    async with httpx.AsyncClient() as http:
        return await run_search(http=http, **kwargs)


def test_format_deals_matches_spec_example() -> None:
    hop = Offer(
        source="kiwi",
        origin="CNX",
        destination="BKK",
        depart_date=date(2026, 10, 25),
        price_usd=35.0,
        price_original=35.0,
        currency_original="USD",
        arrive_time=datetime(2026, 10, 25, 7, 10),
    )
    offer = Offer(
        source="kiwi",
        origin="BKK",
        destination="KRK",
        depart_date=date(2026, 10, 25),
        price_usd=200.0,
        price_original=200.0,
        currency_original="USD",
        depart_time=datetime(2026, 10, 25, 23, 50),
        arrive_time=datetime(2026, 10, 26, 14, 5),
        airlines=["Oman Air", "Ryanair"],
        stops=2,
        self_transfer=True,
        link="https://example.com/offer",
        cabin_bag_included=True,
        hop=hop,
    )
    text = format_deals([offer])
    assert "✈️ <b>$235</b> BKK→KRK · Sun 25 Oct 23:50→Mon 26 Oct 14:05" in text
    assert "Airlines: Oman Air, Ryanair · 2 stops · self-transfer" in text
    assert "+ hop CNX→BKK $35 (arr 07:10) — included in total" in text
    assert "Cabin bag: yes" in text
    assert 'Source: kiwi · <a href="https://example.com/offer">open</a>' in text


def test_format_deals_cabin_bag_and_notes() -> None:
    unknown = _offer(source="google", price_usd=199.0)
    unknown.cabin_bag_included = None
    unknown.notes = "bags not verified (Google)"
    unknown.link = "https://example.com/g"
    missing = _offer(source="kiwi", price_usd=180.0, flight_numbers=["X1"])
    missing.cabin_bag_included = False
    missing.notes = ""
    text = format_deals([unknown, missing])
    assert "Cabin bag: unknown" in text
    assert "bags not verified (Google)" in text
    assert "Cabin bag: not included" in text


def test_format_deals_also_on_lines_max_three() -> None:
    offer = _offer(source="google", price_usd=180.0)
    extras = [
        {
            "source": "kiwi",
            "total_usd": 198.0,
            "link": "https://kiwi.example/1",
            "cabin_bag_included": True,
        },
        {
            "source": "letsfg",
            "total_usd": 205.0,
            "link": None,
            "cabin_bag_included": False,
        },
        {
            "source": "skiplagged",
            "total_usd": 210.0,
            "link": "https://s.example",
            "cabin_bag_included": None,
        },
        {
            "source": "rpl",
            "total_usd": 220.0,
            "link": None,
            "cabin_bag_included": True,
        },
    ]
    text = format_deals([offer], [extras])
    assert "also on kiwi $198 (cabin bag yes) · <a href=\"https://kiwi.example/1\">open</a>" in text
    assert "also on letsfg $205 (cabin bag not included)" in text
    assert "also on skiplagged $210 (cabin bag unknown)" in text
    assert "rpl" not in text
    risky = _offer(source="google", destination="KRK", price_usd=210.0, flight_numbers=["X2"])
    risky.notes = "see <script>alert(1)</script>"
    escaped = format_deals([risky])
    assert "<script>" not in escaped
    assert "see &lt;script&gt;alert(1)&lt;/script&gt;" in escaped


async def test_dedupe_across_sources_keeps_cheapest(monkeypatch: pytest.MonkeyPatch) -> None:
    kiwi = FakeSource(
        {
            "offers": [
                _offer(source="kiwi", price_usd=220.0),
            ]
        }
    )
    kiwi.name = "kiwi"
    google = FakeSource(
        {
            "offers": [
                _offer(source="google", price_usd=199.0),
            ]
        }
    )
    google.name = "google"
    _install_sources(monkeypatch, {"kiwi": kiwi, "google": google})
    result = await _run(
        monkeypatch,
        config=_config(),
        env={},
        state=State(),
        now=NOW,
        dry_run=True,
    )
    assert len(result.qualifying) == 1
    assert result.qualifying[0].source == "google"
    assert result.qualifying[0].total_usd == 199.0
    assert result.best_per_source["kiwi"].total_usd == 220.0
    assert result.best_per_source["google"].total_usd == 199.0
    key = State().alert_key(result.qualifying[0])
    extras = result.also_seen[key]
    assert extras[0]["source"] == "kiwi"
    assert extras[0]["total_usd"] == 220.0
    assert extras[0]["cabin_bag_included"] is None
    text = format_deals(
        result.qualifying,
        [extras],
    )
    assert "also on kiwi $220 (cabin bag unknown)" in text


async def test_alert_only_new_or_cheaper(monkeypatch: pytest.MonkeyPatch) -> None:
    def src(price: float) -> FakeSource:
        s = FakeSource({"offers": [_offer(source="kiwi", price_usd=price)]})
        s.name = "kiwi"
        return s

    notifier = FakeNotifier()
    state = State()
    _install_sources(monkeypatch, {"kiwi": src(200.0), "google": FakeSource({"available": False, "reason": "off"})})
    first = await _run(
        monkeypatch,
        config=_config({"kiwi": {"enabled": True}, "google": {"enabled": True}}),
        env={},
        state=state,
        now=NOW,
        notifier=notifier,
    )
    assert len(first.new_alerts) == 1
    assert any("200" in msg or "$200" in msg for msg in notifier.sent)
    key = state.alert_key(first.qualifying[0])
    assert key in state.alerts

    notifier.sent.clear()
    _install_sources(monkeypatch, {"kiwi": src(198.0), "google": FakeSource({"available": False})})
    second = await _run(
        monkeypatch,
        config=_config({"kiwi": {"enabled": True}, "google": {"enabled": True}}),
        env={},
        state=state,
        now=NOW,
        notifier=notifier,
    )
    assert second.new_alerts == []
    assert not any("$198" in msg for msg in notifier.sent)

    notifier.sent.clear()
    _install_sources(monkeypatch, {"kiwi": src(190.0), "google": FakeSource({"available": False})})
    third = await _run(
        monkeypatch,
        config=_config({"kiwi": {"enabled": True}, "google": {"enabled": True}}),
        env={},
        state=state,
        now=NOW,
        notifier=notifier,
    )
    assert len(third.new_alerts) == 1
    assert third.new_alerts[0].total_usd == 190.0


async def test_dry_run_does_not_mutate_alerts_or_heartbeat(monkeypatch: pytest.MonkeyPatch) -> None:
    source = FakeSource({"offers": [_offer(source="kiwi", price_usd=180.0)]})
    source.name = "kiwi"
    _install_sources(monkeypatch, {"kiwi": source})
    state = State()
    old = _offer(source="kiwi", depart_date=date(2026, 10, 1), price_usd=100.0, flight_numbers=["OLD"])
    state.mark_alerted(old, NOW)
    state.runs = 4
    state.last_heartbeat_date = None
    notifier = FakeNotifier()
    await _run(
        monkeypatch,
        config=_config({"kiwi": {"enabled": True}}),
        env={},
        state=state,
        now=NOW,
        notifier=notifier,
        dry_run=True,
    )
    assert state.runs == 4
    assert state.alert_key(old) in state.alerts
    assert state.last_heartbeat_date is None
    assert notifier.sent == []


async def test_source_failure_is_isolated(monkeypatch: pytest.MonkeyPatch) -> None:
    good = FakeSource({"offers": [_offer(source="kiwi", price_usd=210.0)]})
    good.name = "kiwi"
    bad = FakeSource({"error": "boom"})
    bad.name = "google"
    _install_sources(monkeypatch, {"kiwi": good, "google": bad})
    result = await _run(
        monkeypatch,
        config=_config(),
        env={},
        state=State(),
        now=NOW,
        dry_run=True,
    )
    assert result.source_status["kiwi"]["status"] == "ok"
    assert result.source_status["google"]["status"] == "error"
    assert result.source_status["google"]["error"] == "boom"
    assert len(result.qualifying) == 1


async def test_health_alert_on_third_failure(monkeypatch: pytest.MonkeyPatch) -> None:
    bad = FakeSource({"error": "down"})
    bad.name = "kiwi"
    _install_sources(monkeypatch, {"kiwi": bad})
    state = State()
    notifier = FakeNotifier()
    cfg = _config({"kiwi": {"enabled": True}})
    for _ in range(2):
        await _run(monkeypatch, config=cfg, env={}, state=state, now=NOW, notifier=notifier)
    assert state.sources["kiwi"]["health"]["consecutive_failures"] == 2
    assert not any("3 consecutive" in msg for msg in notifier.sent)
    notifier.sent.clear()
    await _run(monkeypatch, config=cfg, env={}, state=state, now=NOW, notifier=notifier)
    assert state.sources["kiwi"]["health"]["consecutive_failures"] == 3
    assert any("kiwi" in msg and "3 consecutive" in msg for msg in notifier.sent)


async def test_heartbeat_once_per_utc_day(monkeypatch: pytest.MonkeyPatch) -> None:
    source = FakeSource({"offers": [_offer(source="kiwi", price_usd=240.0)]})
    source.name = "kiwi"
    _install_sources(monkeypatch, {"kiwi": source})
    state = State()
    notifier = FakeNotifier()
    cfg = _config({"kiwi": {"enabled": True}})
    first = await _run(monkeypatch, config=cfg, env={}, state=state, now=NOW, notifier=notifier)
    heartbeats = [m for m in notifier.sent if "heartbeat" in m.lower()]
    assert len(heartbeats) == 1
    assert first.messages_sent >= 2  # deal + heartbeat
    assert state.last_heartbeat_date == "2026-10-21"
    notifier.sent.clear()
    later = NOW.replace(hour=20)
    await _run(monkeypatch, config=cfg, env={}, state=state, now=later, notifier=notifier)
    assert not any("heartbeat" in m.lower() for m in notifier.sent)


async def test_window_over_skips_search(monkeypatch: pytest.MonkeyPatch) -> None:
    called = {"n": 0}

    async def boom(query, ctx):
        called["n"] += 1
        raise AssertionError("hops should not run")

    monkeypatch.setattr("flightsearch.pipeline.fetch_hops", boom)

    def fail_load(name, settings):
        raise AssertionError("sources should not load")

    monkeypatch.setattr("flightsearch.pipeline.load_source", fail_load)
    notifier = FakeNotifier()
    state = State()
    after = datetime(2026, 11, 2, 1, 0, tzinfo=timezone.utc)
    result = await run_search(
        _config(),
        env={},
        state=state,
        now=after,
        notifier=notifier,
    )
    assert result.window_over is True
    assert result.offers == []
    assert result.qualifying == []
    assert called["n"] == 0
    assert any("window is over" in m.lower() or "heartbeat" in m.lower() for m in notifier.sent)
    assert state.last_heartbeat_date == "2026-11-02"


async def test_cnx_price_thresholds(monkeypatch: pytest.MonkeyPatch) -> None:
    source = FakeSource(
        {
            "offers": [
                _offer(source="kiwi", destination="WAW", price_usd=249.99, flight_numbers=["A1"]),
                _offer(source="kiwi", destination="KRK", price_usd=250.01, flight_numbers=["A2"]),
                _offer(source="kiwi", destination="WMI", price_usd=260.0, flight_numbers=["A3"]),
            ]
        }
    )
    source.name = "kiwi"
    _install_sources(monkeypatch, {"kiwi": source})
    result = await _run(
        monkeypatch,
        config=_config({"kiwi": {"enabled": True}}),
        env={},
        state=State(),
        now=NOW,
        dry_run=True,
    )
    assert [o.total_usd for o in result.qualifying] == [249.99]
    assert [o.total_usd for o in result.near_misses] == [250.01, 260.0]


async def test_skipped_unknown_source(monkeypatch: pytest.MonkeyPatch) -> None:
    _install_sources(monkeypatch, {})
    result = await _run(
        monkeypatch,
        config=_config(),
        env={},
        state=State(),
        now=NOW,
        only_sources=["none_existing_name"],
        dry_run=True,
    )
    assert result.source_status["none_existing_name"]["status"] == "skipped"
    assert result.window_over is False
    assert result.source_status["hops"]["status"] == "ok"


async def test_hop_fetch_runs_alongside_sources(monkeypatch: pytest.MonkeyPatch) -> None:
    order: list[str] = []

    async def slow_hops(query: SearchQuery, ctx: RunContext) -> list[Offer]:
        order.append("hops-start")
        await asyncio.sleep(0.05)
        order.append("hops-end")
        return []

    class SlowSource(FakeSource):
        async def search(self, query: SearchQuery, ctx: RunContext) -> list[Offer]:
            order.append("src-start")
            await asyncio.sleep(0.05)
            order.append("src-end")
            return [_offer(source="kiwi", price_usd=210.0)]

    src = SlowSource({})
    src.name = "kiwi"
    _install_sources(monkeypatch, {"kiwi": src})
    result = await _run(
        monkeypatch,
        config=_config({"kiwi": {"enabled": True}}),
        env={},
        state=State(),
        now=NOW,
        dry_run=True,
        hops_fn=slow_hops,
    )
    assert "hops-start" in order and "src-start" in order
    assert order.index("src-start") < order.index("hops-end")
    assert order.index("hops-start") < order.index("src-end")
    assert result.source_status["hops"]["status"] == "ok"


async def test_hop_fetch_failure_recorded_and_health(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def boom(query: SearchQuery, ctx: RunContext) -> list[Offer]:
        raise RuntimeError("mcp down")

    source = FakeSource({"offers": [_offer(source="kiwi", price_usd=210.0)]})
    source.name = "kiwi"
    _install_sources(monkeypatch, {"kiwi": source})
    state = State()
    cfg = _config({"kiwi": {"enabled": True}})
    result = await _run(
        monkeypatch,
        config=cfg,
        env={},
        state=state,
        now=NOW,
        dry_run=True,
        hops_fn=boom,
    )
    assert result.source_status["hops"]["status"] == "error"
    assert "mcp down" in (result.source_status["hops"]["error"] or "")
    assert result.qualifying  # sources still succeed
    assert state.sources["hops"]["health"]["consecutive_failures"] == 1


async def test_hop_timeout_uses_config_and_health_alert(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def slow(query: SearchQuery, ctx: RunContext) -> list[Offer]:
        await asyncio.sleep(1)
        return []

    source = FakeSource({"offers": [_offer(source="kiwi", price_usd=210.0)]})
    source.name = "kiwi"
    _install_sources(monkeypatch, {"kiwi": source})
    state = State()
    notifier = FakeNotifier()
    cfg = _config({"kiwi": {"enabled": True}})
    cfg.hop_fetch_timeout_s = 0.05
    for _ in range(2):
        await _run(
            monkeypatch,
            config=cfg,
            env={},
            state=state,
            now=NOW,
            notifier=notifier,
            hops_fn=slow,
        )
    assert state.sources["hops"]["health"]["consecutive_failures"] == 2
    assert not any("hops" in msg and "3 consecutive" in msg for msg in notifier.sent)
    notifier.sent.clear()
    third = await _run(
        monkeypatch,
        config=cfg,
        env={},
        state=state,
        now=NOW,
        notifier=notifier,
        hops_fn=slow,
    )
    assert third.source_status["hops"]["status"] == "error"
    assert "timeout" in (third.source_status["hops"]["error"] or "")
    assert state.sources["hops"]["health"]["consecutive_failures"] == 3
    assert any("hops" in msg and "3 consecutive" in msg for msg in notifier.sent)


def test_config_yaml_has_hop_fetch_timeout() -> None:
    text = (Path(__file__).resolve().parents[1] / "config.yaml").read_text(encoding="utf-8")
    assert "fetch_timeout_s: 360" in text


async def test_sources_see_bangkok_date(monkeypatch: pytest.MonkeyPatch) -> None:
    seen: dict[str, object] = {}

    class Capture(FakeSource):
        async def search(self, query: SearchQuery, ctx: RunContext) -> list[Offer]:
            seen["date"] = ctx.now.date()
            seen["tz"] = getattr(ctx.now.tzinfo, "key", str(ctx.now.tzinfo))
            return []

    source = Capture({})
    source.name = "kiwi"
    _install_sources(monkeypatch, {"kiwi": source})

    async def hop_check(query: SearchQuery, ctx: RunContext) -> list[Offer]:
        seen["hop_date"] = ctx.now.date()
        return []

    now = datetime(2026, 10, 19, 17, 30, tzinfo=timezone.utc)
    await _run(
        monkeypatch,
        config=_config({"kiwi": {"enabled": True}}),
        env={},
        state=State(),
        now=now,
        dry_run=True,
        hops_fn=hop_check,
    )
    assert seen["date"] == date(2026, 10, 20)
    assert seen["hop_date"] == date(2026, 10, 20)
    assert seen["tz"] == "Asia/Bangkok"
