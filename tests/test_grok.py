from __future__ import annotations

import json

import httpx
import pytest
import respx

from flightsearch.notify.grok import GrokBotError, GrokBotNotifier, plain_message
from flightsearch.notify.multi import MultiNotifier

URL = "https://grok.example/webhook"
KEY = "webhook-secret-value"


def test_plain_message_keeps_link_targets() -> None:
    text = '<b>$90</b> CNX→DAD · <a href="https://example.com/offer">open</a>'
    assert plain_message(text) == "$90 CNX→DAD · open (https://example.com/offer)"


@respx.mock
async def test_send_posts_message_with_bearer() -> None:
    route = respx.post(URL).mock(return_value=httpx.Response(200, json={"ok": True}))
    async with httpx.AsyncClient() as http:
        await GrokBotNotifier(URL, KEY, http).send("<b>hello</b>")
    assert route.call_count == 1
    request = route.calls[0].request
    assert request.headers["Authorization"] == f"Bearer {KEY}"
    assert json.loads(request.content) == {"message": "hello"}


@respx.mock
async def test_failure_sanitizes_key() -> None:
    respx.post(URL).mock(
        return_value=httpx.Response(401, json={"error": f"bad key {KEY}"})
    )
    async with httpx.AsyncClient() as http:
        with pytest.raises(GrokBotError) as exc:
            await GrokBotNotifier(URL, KEY, http).send("hello")
    assert KEY not in str(exc.value)
    assert "401" in str(exc.value)


@respx.mock
async def test_multi_notifier_keeps_going_when_one_channel_fails() -> None:
    class Boom:
        async def send(self, text: str, parse_mode: str = "HTML") -> None:
            raise RuntimeError("telegram down")

    route = respx.post(URL).mock(return_value=httpx.Response(200, json={"ok": True}))
    async with httpx.AsyncClient() as http:
        await MultiNotifier([Boom(), GrokBotNotifier(URL, KEY, http)]).send("deal")
    assert route.call_count == 1
    assert json.loads(route.calls[0].request.content)["message"] == "deal"


@respx.mock
async def test_multi_notifier_raises_when_every_channel_fails() -> None:
    respx.post(URL).mock(return_value=httpx.Response(500, text="nope"))

    class Boom:
        async def send(self, text: str, parse_mode: str = "HTML") -> None:
            raise RuntimeError("telegram down")

    async with httpx.AsyncClient() as http:
        with pytest.raises(RuntimeError, match="telegram down"):
            await MultiNotifier([Boom(), GrokBotNotifier(URL, KEY, http)]).send("deal")
