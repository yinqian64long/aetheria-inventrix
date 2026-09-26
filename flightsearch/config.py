from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path
from typing import Any

import yaml

from flightsearch.models import SearchQuery


@dataclass
class AppConfig:
    query: SearchQuery
    hop_min_connection_minutes: int
    hop_max_connection_hours: int
    sources: dict[str, dict]
    notify: dict
    hop_fetch_timeout_s: int = 360


def _as_date(value: date | str) -> date:
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    return date.fromisoformat(str(value))


def load_config(path: str | Path = "config.yaml") -> AppConfig:
    with open(path, encoding="utf-8") as f:
        raw: dict[str, Any] = yaml.safe_load(f)

    hop = raw.get("hop") or {}
    query = SearchQuery(
        home_origin=raw["home_origin"],
        positioning_origins=tuple(raw.get("positioning_origins") or ()),
        destinations=tuple(raw.get("destinations") or ()),
        extra_destinations=tuple(raw.get("extra_destinations") or ()),
        date_from=_as_date(raw["date_from"]),
        date_to=_as_date(raw["date_to"]),
        adults=int(raw["adults"]),
        cabin_bags=int(raw["cabin_bags"]),
        checked_bags=int(raw["checked_bags"]),
        max_total_usd=float(raw["max_total_usd"]),
        near_miss_usd=float(raw["near_miss_usd"]),
        currency=str(raw["currency"]),
    )
    return AppConfig(
        query=query,
        hop_min_connection_minutes=int(hop["min_connection_minutes"]),
        hop_max_connection_hours=int(hop["max_connection_hours"]),
        sources=dict(raw.get("sources") or {}),
        notify=dict(raw.get("notify") or {}),
        hop_fetch_timeout_s=int(hop.get("fetch_timeout_s", 360)),
    )
