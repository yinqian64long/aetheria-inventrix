from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import date, datetime
from pathlib import Path
from typing import Any

from flightsearch.models import Offer

PRICE_DROP_USD = 5.0


def _iso(now: datetime | date | str) -> str:
    if isinstance(now, str):
        return now
    return now.isoformat()


def _empty_source() -> dict[str, Any]:
    return {
        "health": {
            "consecutive_failures": 0,
            "last_ok": None,
            "last_error": None,
        },
        "data": {},
    }


@dataclass
class State:
    version: int = 1
    runs: int = 0
    last_heartbeat_date: str | None = None
    alerts: dict[str, dict[str, Any]] = field(default_factory=dict)
    sources: dict[str, dict[str, Any]] = field(default_factory=dict)

    @classmethod
    def load(cls, path: str | Path) -> State:
        p = Path(path)
        if not p.exists():
            return cls()
        raw = json.loads(p.read_text(encoding="utf-8"))
        if not isinstance(raw, dict):
            return cls()
        sources = raw.get("sources") or {}
        normalized: dict[str, dict[str, Any]] = {}
        for name, entry in sources.items():
            src = _empty_source()
            if isinstance(entry, dict):
                health = entry.get("health") or {}
                if isinstance(health, dict):
                    src["health"].update(health)
                data = entry.get("data")
                if isinstance(data, dict):
                    src["data"] = data
            normalized[str(name)] = src
        return cls(
            version=int(raw.get("version") or 1),
            runs=int(raw.get("runs") or 0),
            last_heartbeat_date=raw.get("last_heartbeat_date"),
            alerts=dict(raw.get("alerts") or {}),
            sources=normalized,
        )

    def save(self, path: str | Path) -> None:
        p = Path(path)
        p.parent.mkdir(parents=True, exist_ok=True)
        text = json.dumps(self.to_dict(), indent=2, sort_keys=True, ensure_ascii=False)
        p.write_text(text + "\n", encoding="utf-8")

    def to_dict(self) -> dict[str, Any]:
        return {
            "version": self.version,
            "runs": self.runs,
            "last_heartbeat_date": self.last_heartbeat_date,
            "alerts": self.alerts,
            "sources": self.sources,
        }

    def source_data(self, name: str) -> dict:
        src = self.sources.get(name)
        if src is None:
            src = _empty_source()
            self.sources[name] = src
        src.setdefault("health", _empty_source()["health"])
        src.setdefault("data", {})
        return src["data"]

    def record_source_result(
        self,
        name: str,
        ok: bool,
        error: str | None,
        now: datetime | date | str,
    ) -> None:
        self.source_data(name)
        health = self.sources[name]["health"]
        stamp = _iso(now)
        if ok:
            health["consecutive_failures"] = 0
            health["last_ok"] = stamp
            health["last_error"] = None
        else:
            health["consecutive_failures"] = int(health.get("consecutive_failures") or 0) + 1
            health["last_error"] = error

    def alert_key(self, offer: Offer) -> str:
        key = offer.key()
        prefix = f"{offer.source}|"
        if key.startswith(prefix):
            return key[len(prefix) :]
        if "|" in key:
            return key.split("|", 1)[1]
        return key

    def should_alert(self, offer: Offer) -> bool:
        key = self.alert_key(offer)
        rec = self.alerts.get(key)
        if rec is None:
            return True
        try:
            previous = float(rec.get("total_usd"))
        except (TypeError, ValueError):
            return True
        return (previous - offer.total_usd) >= PRICE_DROP_USD

    def mark_alerted(self, offer: Offer, now: datetime | date | str) -> None:
        key = self.alert_key(offer)
        stamp = _iso(now)
        rec = self.alerts.get(key)
        if rec is None:
            self.alerts[key] = {
                "total_usd": offer.total_usd,
                "first_seen": stamp,
                "last_alerted": stamp,
            }
        else:
            rec["total_usd"] = offer.total_usd
            rec["last_alerted"] = stamp

    def prune(self, today: date) -> None:
        keep: dict[str, dict[str, Any]] = {}
        for key, rec in self.alerts.items():
            depart = _depart_date_from_key(key)
            if depart is None or depart >= today:
                keep[key] = rec
        self.alerts = keep


def _depart_date_from_key(key: str) -> date | None:
    parts = key.split("|")
    if len(parts) < 2:
        return None
    try:
        return date.fromisoformat(parts[1])
    except ValueError:
        return None
