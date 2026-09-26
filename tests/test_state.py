from __future__ import annotations

import json
from datetime import date, datetime, timezone
from pathlib import Path

from flightsearch.models import Offer
from flightsearch.state import State


def _offer(
    *,
    source: str = "kiwi",
    origin: str = "BKK",
    destination: str = "KRK",
    depart_date: date = date(2026, 10, 25),
    price_usd: float = 200.0,
    hop: Offer | None = None,
) -> Offer:
    return Offer(
        source=source,
        origin=origin,
        destination=destination,
        depart_date=depart_date,
        price_usd=price_usd,
        price_original=price_usd,
        currency_original="USD",
        flight_numbers=["LO68"],
        hop=hop,
    )


def test_load_missing_and_save_pretty_sorted(tmp_path: Path) -> None:
    path = tmp_path / "nested" / "state.json"
    state = State.load(path)
    assert state.runs == 0
    assert state.alerts == {}
    state.runs = 3
    state.last_heartbeat_date = "2026-10-21"
    state.alerts["BKK-KRK|2026-10-25||LO68"] = {
        "total_usd": 200.0,
        "first_seen": "t1",
        "last_alerted": "t1",
    }
    state.source_data("kiwi")["cursor"] = 1
    state.save(path)
    raw = path.read_text(encoding="utf-8")
    data = json.loads(raw)
    assert data["version"] == 1
    assert list(data.keys()) == sorted(data.keys())
    assert raw.endswith("\n")
    restored = State.load(path)
    assert restored.runs == 3
    assert restored.last_heartbeat_date == "2026-10-21"
    assert restored.source_data("kiwi")["cursor"] == 1


def test_source_data_is_mutable_and_record_health() -> None:
    state = State()
    now = datetime(2026, 10, 21, 8, 0, tzinfo=timezone.utc)
    data = state.source_data("google")
    data["n"] = 2
    assert state.sources["google"]["data"]["n"] == 2
    state.record_source_result("google", False, "boom", now)
    assert state.sources["google"]["health"]["consecutive_failures"] == 1
    assert state.sources["google"]["health"]["last_error"] == "boom"
    state.record_source_result("google", False, "again", now)
    assert state.sources["google"]["health"]["consecutive_failures"] == 2
    state.record_source_result("google", True, None, now)
    assert state.sources["google"]["health"]["consecutive_failures"] == 0
    assert state.sources["google"]["health"]["last_error"] is None
    assert state.sources["google"]["health"]["last_ok"] == now.isoformat()


def test_alert_key_strips_source_and_dedupes() -> None:
    state = State()
    kiwi = _offer(source="kiwi", price_usd=200)
    google = _offer(source="google", price_usd=180)
    assert state.alert_key(kiwi) == kiwi.key().removeprefix("kiwi|")
    assert not state.alert_key(kiwi).startswith("kiwi|")
    assert state.alert_key(kiwi) == state.alert_key(google)


def test_should_alert_new_or_cheaper_by_five() -> None:
    state = State()
    now = datetime(2026, 10, 21, tzinfo=timezone.utc)
    first = _offer(price_usd=200)
    assert state.should_alert(first)
    state.mark_alerted(first, now)
    assert not state.should_alert(_offer(price_usd=196))
    assert state.should_alert(_offer(price_usd=195))
    cheaper = _offer(price_usd=190)
    state.mark_alerted(cheaper, now)
    rec = state.alerts[state.alert_key(cheaper)]
    assert rec["total_usd"] == 190.0
    assert rec["first_seen"] == now.isoformat()
    assert rec["last_alerted"] == now.isoformat()


def test_prune_drops_past_departures() -> None:
    state = State()
    now = datetime(2026, 10, 21, tzinfo=timezone.utc)
    old = _offer(depart_date=date(2026, 10, 20), price_usd=100)
    upcoming = _offer(depart_date=date(2026, 10, 25), price_usd=100)
    state.mark_alerted(old, now)
    state.mark_alerted(upcoming, now)
    state.prune(date(2026, 10, 21))
    keys = set(state.alerts)
    assert state.alert_key(old) not in keys
    assert state.alert_key(upcoming) in keys
