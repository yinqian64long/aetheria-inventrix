from __future__ import annotations

import logging

import httpx
import pytest
import respx

from flightsearch.fx import ER_API_URL, FRANKFURTER_URL, LiveFx


@respx.mock
async def test_load_from_frankfurter() -> None:
    respx.get(FRANKFURTER_URL).mock(
        return_value=httpx.Response(
            200,
            json={"base": "USD", "rates": {"EUR": 0.5, "PLN": 4.0, "THB": 40.0}},
        )
    )
    async with httpx.AsyncClient() as http:
        fx = await LiveFx.load(http)
    assert fx.to_usd(10, "USD") == 10
    assert fx.to_usd(5, "EUR") == 10.0
    assert fx.to_usd(80, "PLN") == 20.0


@respx.mock
async def test_frankfurter_failure_falls_back_to_er_api() -> None:
    respx.get(FRANKFURTER_URL).mock(return_value=httpx.Response(503))
    respx.get(ER_API_URL).mock(
        return_value=httpx.Response(
            200,
            json={"result": "success", "rates": {"GBP": 0.8, "THB": 32.0}},
        )
    )
    async with httpx.AsyncClient() as http:
        fx = await LiveFx.load(http)
    assert fx.to_usd(8, "gbp") == 10.0
    assert fx.to_usd(64, "THB") == 2.0


@respx.mock
async def test_both_apis_fail_uses_static_fallback(caplog: pytest.LogCaptureFixture) -> None:
    respx.get(FRANKFURTER_URL).mock(return_value=httpx.Response(500))
    respx.get(ER_API_URL).mock(side_effect=httpx.ConnectError("nope"))
    caplog.set_level(logging.WARNING)
    async with httpx.AsyncClient() as http:
        fx = await LiveFx.load(http)
    assert fx.to_usd(100, "USD") == 100
    assert fx.to_usd(3.85, "PLN") == pytest.approx(1.0)
    assert fx.to_usd(34.5, "THB") == pytest.approx(1.0)
    assert any("static fallback" in rec.message.lower() for rec in caplog.records)


def test_usd_is_identity_and_unknown_raises() -> None:
    fx = LiveFx({"EUR": 0.92})
    assert fx.to_usd(12.34, "USD") == 12.34
    with pytest.raises(ValueError, match="Unknown currency"):
        fx.to_usd(1, "XYZ")
    with pytest.raises(ValueError):
        fx.to_usd(1, "")
