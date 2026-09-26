from __future__ import annotations

import logging
from typing import Any

import httpx

log = logging.getLogger("flightsearch.fx")

FRANKFURTER_URL = "https://api.frankfurter.app/latest?from=USD"
ER_API_URL = "https://open.er-api.com/v6/latest/USD"

# Approximate USD→currency rates used only when both live APIs fail.
STATIC_RATES: dict[str, float] = {
    "USD": 1.0,
    "EUR": 0.92,
    "GBP": 0.79,
    "PLN": 3.85,
    "THB": 34.5,
    "CZK": 23.0,
    "CHF": 0.88,
    "HUF": 360.0,
    "SEK": 10.5,
    "NOK": 10.8,
    "DKK": 6.85,
    "AUD": 1.52,
    "CAD": 1.37,
    "JPY": 150.0,
    "CNY": 7.2,
    "SGD": 1.35,
    "MYR": 4.5,
    "INR": 84.0,
    "HKD": 7.8,
    "KRW": 1350.0,
}


class LiveFx:
    def __init__(self, rates: dict[str, float]) -> None:
        self.rates = {k.upper(): float(v) for k, v in rates.items() if v}
        self.rates.setdefault("USD", 1.0)

    def to_usd(self, amount: float, currency: str) -> float:
        code = (currency or "").upper()
        if not code:
            raise ValueError("Unknown currency: empty")
        if code == "USD":
            return float(amount)
        rate = self.rates.get(code)
        if rate is None:
            raise ValueError(f"Unknown currency: {currency}")
        return float(amount) / rate

    @classmethod
    async def load(cls, http: httpx.AsyncClient) -> LiveFx:
        rates = await _fetch_frankfurter(http)
        if rates is None:
            rates = await _fetch_er_api(http)
        if rates is None:
            log.warning("FX APIs unavailable; using static fallback rates")
            rates = dict(STATIC_RATES)
        return cls(rates)


async def _fetch_frankfurter(http: httpx.AsyncClient) -> dict[str, float] | None:
    return await _get_rates(http, FRANKFURTER_URL)


async def _fetch_er_api(http: httpx.AsyncClient) -> dict[str, float] | None:
    return await _get_rates(http, ER_API_URL)


async def _get_rates(http: httpx.AsyncClient, url: str) -> dict[str, float] | None:
    try:
        resp = await http.get(url, follow_redirects=True)
        resp.raise_for_status()
        payload: Any = resp.json()
    except Exception as exc:
        log.info("FX fetch failed for %s: %s", url, exc)
        return None
    raw = payload.get("rates") if isinstance(payload, dict) else None
    if not isinstance(raw, dict) or not raw:
        log.info("FX response from %s had no rates", url)
        return None
    out: dict[str, float] = {"USD": 1.0}
    for key, value in raw.items():
        try:
            out[str(key).upper()] = float(value)
        except (TypeError, ValueError):
            continue
    return out if len(out) > 1 else None
