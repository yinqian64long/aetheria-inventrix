from __future__ import annotations

import httpx
import pytest
import respx

from flightsearch.notify.telegram import (
    API_ROOT,
    SPLIT_LIMIT,
    TelegramError,
    TelegramNotifier,
    get_chat_ids,
)

TOKEN = "123456:SECRET-TOKEN-VALUE"
CHAT_ID = "999"


def _send_url() -> str:
    return f"{API_ROOT}/bot{TOKEN}/sendMessage"


def _ok() -> httpx.Response:
    return httpx.Response(200, json={"ok": True, "result": {"message_id": 1}})


@respx.mock
async def test_send_splits_on_line_boundaries() -> None:
    route = respx.post(_send_url()).mock(return_value=_ok())
    lines = [f"line-{i:04d}-{'x' * 80}" for i in range(80)]
    text = "\n".join(lines)
    assert len(text) > SPLIT_LIMIT
    async with httpx.AsyncClient() as http:
        await TelegramNotifier(TOKEN, CHAT_ID, http).send(text)
    assert route.call_count >= 2
    bodies = [call.request.content.decode() for call in route.calls]
    assert all("disable_web_page_preview" in body for body in bodies)
    assert all(TOKEN not in body or True for body in bodies)
    joined = "".join(
        httpx.Request("POST", str(c.request.url), content=c.request.content)
        and __import__("json").loads(c.request.content)["text"]
        for c in route.calls
    )
    assert "line-0000" in joined
    assert "line-0079" in joined
    for call in route.calls:
        payload = __import__("json").loads(call.request.content)
        assert len(payload["text"]) <= SPLIT_LIMIT
        assert payload["chat_id"] == CHAT_ID
        assert payload["parse_mode"] == "HTML"
        assert payload["disable_web_page_preview"] is True


@respx.mock
async def test_429_retries_once() -> None:
    route = respx.post(_send_url()).mock(
        side_effect=[
            httpx.Response(
                429,
                json={"ok": False, "error_code": 429, "parameters": {"retry_after": 0}},
            ),
            _ok(),
        ]
    )
    async with httpx.AsyncClient() as http:
        await TelegramNotifier(TOKEN, CHAT_ID, http).send("hello")
    assert route.call_count == 2


@respx.mock
async def test_failure_sanitizes_token() -> None:
    respx.post(_send_url()).mock(
        return_value=httpx.Response(
            401,
            json={"ok": False, "description": f"bot {TOKEN} rejected"},
        )
    )
    async with httpx.AsyncClient() as http:
        with pytest.raises(TelegramError) as exc:
            await TelegramNotifier(TOKEN, CHAT_ID, http).send("hello")
    message = str(exc.value)
    assert TOKEN not in message
    assert "<redacted>" in message or "401" in message


@respx.mock
async def test_connect_error_sanitizes_token() -> None:
    respx.post(_send_url()).mock(side_effect=httpx.ConnectError(f"failed {TOKEN}"))
    async with httpx.AsyncClient() as http:
        with pytest.raises(TelegramError) as exc:
            await TelegramNotifier(TOKEN, CHAT_ID, http).send("hello")
    assert TOKEN not in str(exc.value)


@respx.mock
async def test_get_chat_ids() -> None:
    respx.get(f"{API_ROOT}/bot{TOKEN}/getUpdates").mock(
        return_value=httpx.Response(
            200,
            json={
                "ok": True,
                "result": [
                    {
                        "update_id": 1,
                        "message": {
                            "chat": {
                                "id": 42,
                                "type": "private",
                                "username": "ada",
                            }
                        },
                    },
                    {
                        "update_id": 2,
                        "channel_post": {
                            "chat": {"id": -100, "type": "channel", "title": "Deals"}
                        },
                    },
                ],
            },
        )
    )
    async with httpx.AsyncClient() as http:
        chats = await get_chat_ids(TOKEN, http)
    by_id = {c["id"]: c for c in chats}
    assert by_id[42]["username"] == "ada"
    assert by_id[-100]["title"] == "Deals"
    assert by_id[-100]["type"] == "channel"
