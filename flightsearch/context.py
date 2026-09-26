from __future__ import annotations

import logging
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime
from typing import Protocol

import httpx


class FxConverter(Protocol):
    def to_usd(self, amount: float, currency: str) -> float: ...


@dataclass
class RunContext:
    http: httpx.AsyncClient
    fx: FxConverter
    state: dict  # SOURCE-PRIVATE persisted dict; sources may read/write freely
    log: logging.Logger
    now: datetime  # tz-aware, Asia/Bangkok
    env: Mapping[str, str]
