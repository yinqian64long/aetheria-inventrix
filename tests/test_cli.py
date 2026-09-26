from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

import httpx
import pytest
import respx

from flightsearch.cli import main
from flightsearch.config import load_config
from flightsearch.models import Offer
from flightsearch.notify.telegram import API_ROOT
from flightsearch.pipeline import RunResult

ROOT = Path(__file__).resolve().parents[1]
NOW = datetime(2026, 10, 21, 8, 0, tzinfo=timezone.utc)


def _result(**kwargs) -> RunResult:
    base = dict(
        window_over=False,
        offers=[],
        qualifying=[],
        new_alerts=[],
        near_misses=[],
        dropped_count=0,
        best_per_source={},
        source_status={"kiwi": {"status": "ok", "count": 0, "seconds": 0.1, "error": None}},
        messages_sent=0,
        started_at=NOW,
        finished_at=NOW,
    )
    base.update(kwargs)
    return RunResult(**base)


def _patch_run(monkeypatch: pytest.MonkeyPatch, result: RunResult) -> list[dict]:
    calls: list[dict] = []

    async def fake(config, **kwargs):
        calls.append(kwargs)
        return result

    monkeypatch.setattr("flightsearch.cli.run_search", fake)
    return calls


def test_run_writes_json_md_and_step_summary(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    offer = Offer(
        source="kiwi",
        origin="CNX",
        destination="WAW",
        depart_date=NOW.date(),
        price_usd=240.0,
        price_original=240.0,
        currency_original="USD",
    )
    result = _result(
        offers=[offer],
        qualifying=[offer],
        best_per_source={"kiwi": offer},
    )
    _patch_run(monkeypatch, result)
    summary = tmp_path / "summary.md"
    monkeypatch.setenv("GITHUB_STEP_SUMMARY", str(summary))
    out = tmp_path / "out"
    state = tmp_path / "state.json"
    code = main(
        [
            "run",
            "--config",
            str(ROOT / "config.yaml"),
            "--dry-run",
            "--no-notify",
            "--out",
            str(out),
            "--state",
            str(state),
            "--sources",
            "kiwi",
        ]
    )
    assert code == 0
    payload = json.loads((out / "latest.json").read_text(encoding="utf-8"))
    assert payload["window_over"] is False
    assert payload["qualifying"][0]["destination"] == "WAW"
    assert payload["qualifying"][0]["also_seen"] == []
    assert "started_at" in payload
    assert "source_status" in payload
    md = (out / "latest.md").read_text(encoding="utf-8")
    assert "Qualifying" in md
    assert summary.read_text(encoding="utf-8") == md
    assert not state.exists()


def test_run_saves_state_when_not_dry_run(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _patch_run(monkeypatch, _result())
    state = tmp_path / "nested" / "state.json"
    code = main(
        [
            "run",
            "--config",
            str(ROOT / "config.yaml"),
            "--no-notify",
            "--out",
            str(tmp_path / "out"),
            "--state",
            str(state),
        ]
    )
    assert code == 0
    assert state.exists()


def test_exit_1_when_every_attempted_source_failed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _patch_run(
        monkeypatch,
        _result(
            source_status={
                "kiwi": {"status": "error", "count": 0, "seconds": 1.0, "error": "x"},
                "google": {"status": "skipped", "count": 0, "seconds": 0.0, "error": "off"},
            }
        ),
    )
    code = main(
        [
            "run",
            "--config",
            str(ROOT / "config.yaml"),
            "--dry-run",
            "--no-notify",
            "--out",
            str(tmp_path / "out"),
            "--state",
            str(tmp_path / "state.json"),
        ]
    )
    assert code == 1


def test_exit_0_when_window_over_or_no_attempted(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _patch_run(monkeypatch, _result(window_over=True, source_status={}))
    code = main(
        [
            "run",
            "--config",
            str(ROOT / "config.yaml"),
            "--dry-run",
            "--no-notify",
            "--out",
            str(tmp_path / "a"),
            "--state",
            str(tmp_path / "s.json"),
        ]
    )
    assert code == 0

    _patch_run(
        monkeypatch,
        _result(
            source_status={
                "none_existing_name": {
                    "status": "skipped",
                    "count": 0,
                    "seconds": 0.0,
                    "error": "unknown source",
                }
            }
        ),
    )
    code = main(
        [
            "run",
            "--config",
            str(ROOT / "config.yaml"),
            "--dry-run",
            "--no-notify",
            "--sources",
            "none_existing_name",
            "--out",
            str(tmp_path / "b"),
            "--state",
            str(tmp_path / "s2.json"),
        ]
    )
    assert code == 0


@respx.mock
def test_test_telegram(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "tok")
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "1")
    route = respx.post(url__regex=r".*/bottok/sendMessage").mock(
        return_value=httpx.Response(200, json={"ok": True, "result": {}})
    )
    assert main(["test-telegram"]) == 0
    assert route.call_count == 1
    payload = json.loads(route.calls[0].request.content)
    assert payload["text"] == "flightsearch test ✅"


@respx.mock
def test_telegram_chat_id(monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture) -> None:
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "tok")
    respx.get(url__regex=r".*/bottok/getUpdates").mock(
        return_value=httpx.Response(
            200,
            json={
                "ok": True,
                "result": [
                    {"message": {"chat": {"id": 7, "type": "private", "username": "ada"}}}
                ],
            },
        )
    )
    assert main(["telegram-chat-id"]) == 0
    out = capsys.readouterr().out
    assert "7" in out
    assert "private" in out
    assert "ada" in out


def test_load_config_reads_hop_fetch_timeout() -> None:
    cfg = load_config(ROOT / "config.yaml")
    assert cfg.hop_fetch_timeout_s == 360
