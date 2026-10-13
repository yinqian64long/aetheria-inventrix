from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path
from typing import Any

import yaml

from flightsearch.models import PriceCap, SearchQuery


@dataclass
class AppConfig:
    query: SearchQuery
    sources: dict[str, dict]
    notify: dict
    apify_max_concurrent_runs: int = 3


def _as_date(value: date | str) -> date:
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    return date.fromisoformat(str(value))


def load_config(path: str | Path = "config.yaml") -> AppConfig:
    with open(path, encoding="utf-8") as f:
        raw: dict[str, Any] = yaml.safe_load(f)

    query = SearchQuery(
        origins=_codes(raw.get("origins")),
        destinations=_codes(raw.get("destinations")),
        priority_routes=_routes(raw.get("priority_routes")),
        date_from=_as_date(raw["date_from"]),
        date_to=_as_date(raw["date_to"]),
        adults=int(raw["adults"]),
        cabin_bags=int(raw["cabin_bags"]),
        checked_bags=int(raw["checked_bags"]),
        max_total_usd=float(raw["max_total_usd"]),
        near_miss_usd=float(raw["near_miss_usd"]),
        currency=str(raw["currency"]),
        price_caps=_price_caps(raw.get("price_caps")),
        near_miss_margin_usd=float(raw.get("near_miss_margin_usd", 20)),
    )
    return AppConfig(
        query=query,
        sources=dict(raw.get("sources") or {}),
        notify=dict(raw.get("notify") or {}),
        apify_max_concurrent_runs=max(1, int(raw.get("apify_max_concurrent_runs", 3))),
    )


def _price_caps(value: Any) -> tuple[PriceCap, ...]:
    caps: list[PriceCap] = []
    for item in value or []:
        if not isinstance(item, dict):
            continue
        origins = tuple(
            str(code).strip().upper()
            for code in (item.get("origins") or [])
            if str(code).strip()
        )
        if not origins:
            continue
        caps.append(
            PriceCap(
                origins=origins,
                with_bag_usd=float(item["with_bag"]),
                without_bag_usd=float(item["without_bag"]),
            )
        )
    return tuple(caps)


def _codes(value: Any) -> tuple[str, ...]:
    return tuple(str(code).strip().upper() for code in (value or []) if str(code).strip())


def _routes(value: Any) -> tuple[tuple[str, str], ...]:
    routes: list[tuple[str, str]] = []
    for item in value or []:
        if isinstance(item, str) and "-" in item:
            origin, dest = item.split("-", 1)
        elif isinstance(item, (list, tuple)) and len(item) == 2:
            origin, dest = item
        else:
            continue
        origin_code = str(origin).strip().upper()
        dest_code = str(dest).strip().upper()
        if origin_code and dest_code:
            routes.append((origin_code, dest_code))
    return tuple(routes)
