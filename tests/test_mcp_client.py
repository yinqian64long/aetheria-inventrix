from __future__ import annotations

import asyncio
import inspect
from types import SimpleNamespace
from typing import Any

import httpx
import pytest

from flightsearch.mcp_client import (
    McpCallError,
    McpSession,
    call_mcp_tool,
    format_mcp_error,
    is_retryable_mcp_error,
    list_mcp_tools,
    unwrap_exception_group,
)


def _http_error(status: int, *, retry_after: str | None = None) -> httpx.HTTPStatusError:
    headers = {"Retry-After": retry_after} if retry_after else {}
    request = httpx.Request("POST", "https://mcp.example.test/mcp")
    response = httpx.Response(status, headers=headers, request=request)
    return httpx.HTTPStatusError(
        f"Client error '{status} {'Too Many Requests' if status == 429 else 'Error'}'",
        request=request,
        response=response,
    )


def _ok_result(payload: Any = None) -> SimpleNamespace:
    text = '{"ok": true}' if payload is None else payload
    if not isinstance(text, str):
        import json

        text = json.dumps(payload)
    return SimpleNamespace(
        content=[SimpleNamespace(text=text)],
        is_error=False,
        isError=False,
    )


def test_response_hook_is_awaitable() -> None:
    assert inspect.iscoroutinefunction(McpSession._capture_response)


def test_unwrap_exception_group_flattens_leaves() -> None:
    inner = RuntimeError("leaf-b")
    mid = ExceptionGroup("mid", [ValueError("leaf-a"), inner])
    outer = ExceptionGroup("unhandled errors in a TaskGroup (1 sub-exception)", [mid])
    leaves = unwrap_exception_group(outer)
    assert [type(x) for x in leaves] == [ValueError, RuntimeError]
    assert [str(x) for x in leaves] == ["leaf-a", "leaf-b"]


def test_unwrap_plain_exception() -> None:
    err = RuntimeError("plain")
    assert unwrap_exception_group(err) == [err]


def test_format_includes_http_status() -> None:
    grouped = ExceptionGroup("unhandled errors in a TaskGroup", [_http_error(429)])
    text = format_mcp_error(grouped)
    assert "429" in text
    assert "TaskGroup" not in text or "429" in text


def test_retryable_429_and_5xx() -> None:
    assert is_retryable_mcp_error(_http_error(429), status=429)
    assert is_retryable_mcp_error(RuntimeError("boom"), status=503)
    assert is_retryable_mcp_error(RuntimeError("boom"), status=403)
    assert not is_retryable_mcp_error(RuntimeError("bad args"), status=400)


def test_retryable_connect_and_session_death() -> None:
    assert is_retryable_mcp_error(ConnectionError("connect error"))
    assert is_retryable_mcp_error(TimeoutError("timed out"))
    assert is_retryable_mcp_error(RuntimeError("Session terminated"))
    assert not is_retryable_mcp_error(RuntimeError("invalid params"))


@pytest.mark.asyncio
async def test_call_tool_retries_429_honors_retry_after(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sleeps: list[float] = []

    async def fake_sleep(delay: float) -> None:
        sleeps.append(delay)

    monkeypatch.setattr(asyncio, "sleep", fake_sleep)
    session = McpSession("https://example.test/mcp")
    calls = 0

    class FakeClient:
        async def call_tool(self, name: str, arguments: dict) -> Any:
            nonlocal calls
            calls += 1
            if calls < 3:
                session._last_status = 429
                session._last_retry_after = 1.25
                raise ExceptionGroup(
                    "unhandled errors in a TaskGroup (1 sub-exception)",
                    [_http_error(429, retry_after="1.25")],
                )
            return _ok_result({"ok": True, "n": calls})

    session._client = FakeClient()  # type: ignore[assignment]

    result = await session.call_tool("search", {"q": 1}, retries=2)
    assert result == {"ok": True, "n": 3}
    assert calls == 3
    assert sleeps
    assert sleeps[0] >= 1.25


@pytest.mark.asyncio
async def test_call_tool_retries_5xx(monkeypatch: pytest.MonkeyPatch) -> None:
    async def fake_sleep(delay: float) -> None:
        return None

    monkeypatch.setattr(asyncio, "sleep", fake_sleep)
    session = McpSession("https://example.test/mcp")
    calls = 0

    class FakeClient:
        async def call_tool(self, name: str, arguments: dict) -> Any:
            nonlocal calls
            calls += 1
            if calls == 1:
                session._last_status = 502
                raise RuntimeError("Server returned an error response")
            return _ok_result()

    session._client = FakeClient()  # type: ignore[assignment]
    result = await session.call_tool("t", {}, retries=2)
    assert result == {"ok": True}
    assert calls == 2


@pytest.mark.asyncio
async def test_call_tool_retries_connect_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def fake_sleep(delay: float) -> None:
        return None

    monkeypatch.setattr(asyncio, "sleep", fake_sleep)
    session = McpSession("https://example.test/mcp")
    calls = 0

    class FakeClient:
        async def call_tool(self, name: str, arguments: dict) -> Any:
            nonlocal calls
            calls += 1
            if calls == 1:
                raise ConnectionError("connect error: timed out")
            return _ok_result()

    session._client = FakeClient()  # type: ignore[assignment]
    assert await session.call_tool("t", {}) == {"ok": True}


@pytest.mark.asyncio
async def test_call_tool_does_not_retry_error_result() -> None:
    session = McpSession("https://example.test/mcp")
    calls = 0

    class FakeClient:
        async def call_tool(self, name: str, arguments: dict) -> Any:
            nonlocal calls
            calls += 1
            return SimpleNamespace(content=[], is_error=True, isError=True)

    session._client = FakeClient()  # type: ignore[assignment]
    with pytest.raises(McpCallError, match="error result"):
        await session.call_tool("t", {}, retries=2)
    assert calls == 1


@pytest.mark.asyncio
async def test_call_tool_does_not_retry_400() -> None:
    session = McpSession("https://example.test/mcp")
    calls = 0

    class FakeClient:
        async def call_tool(self, name: str, arguments: dict) -> Any:
            nonlocal calls
            calls += 1
            session._last_status = 400
            raise RuntimeError("invalid params")

    session._client = FakeClient()  # type: ignore[assignment]
    with pytest.raises(McpCallError, match="invalid params") as caught:
        await session.call_tool("t", {}, retries=2)
    assert caught.value.status == 400
    assert calls == 1


@pytest.mark.asyncio
async def test_session_reconnects_after_death(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def fake_sleep(delay: float) -> None:
        return None

    monkeypatch.setattr(asyncio, "sleep", fake_sleep)
    session = McpSession("https://example.test/mcp")
    connects = 0

    class DeadThenOk:
        def __init__(self, live: bool) -> None:
            self.live = live

        async def call_tool(self, name: str, arguments: dict) -> Any:
            if not self.live:
                raise RuntimeError("Session terminated")
            return _ok_result({"reconnected": True})

    async def fake_connect() -> None:
        nonlocal connects
        connects += 1
        session._client = DeadThenOk(live=True)  # type: ignore[assignment]

    async def fake_close() -> None:
        session._client = None

    session._connect = fake_connect  # type: ignore[method-assign]
    session._close = fake_close  # type: ignore[method-assign]
    session._client = DeadThenOk(live=False)  # type: ignore[assignment]

    result = await session.call_tool("t", {}, retries=2)
    assert result == {"reconnected": True}
    assert connects == 1


@pytest.mark.asyncio
async def test_call_mcp_tool_uses_session(monkeypatch: pytest.MonkeyPatch) -> None:
    class FakeSession:
        def __init__(self, url: str, **kwargs: Any) -> None:
            self.url = url
            self.kwargs = kwargs

        async def __aenter__(self) -> FakeSession:
            return self

        async def __aexit__(self, *args: Any) -> None:
            return None

        async def call_tool(self, tool: str, args: dict, retries: int = 2) -> Any:
            return {"tool": tool, "args": args, "retries": retries}

    monkeypatch.setattr("flightsearch.mcp_client.McpSession", FakeSession)
    out = await call_mcp_tool("https://x", "foo", {"z": 1}, retries=1, legacy=True)
    assert out == {"tool": "foo", "args": {"z": 1}, "retries": 1}


@pytest.mark.asyncio
async def test_list_mcp_tools_uses_session(monkeypatch: pytest.MonkeyPatch) -> None:
    class FakeSession:
        def __init__(self, url: str, **kwargs: Any) -> None:
            self.url = url

        async def __aenter__(self) -> FakeSession:
            return self

        async def __aexit__(self, *args: Any) -> None:
            return None

        async def list_tools(self) -> list[dict]:
            return [{"name": "search-flight", "description": "", "inputSchema": {}}]

    monkeypatch.setattr("flightsearch.mcp_client.McpSession", FakeSession)
    tools = await list_mcp_tools("https://mcp.kiwi.com")
    assert tools[0]["name"] == "search-flight"


@pytest.mark.asyncio
async def test_failed_retries_surface_leaf_status(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def fake_sleep(delay: float) -> None:
        return None

    monkeypatch.setattr(asyncio, "sleep", fake_sleep)
    session = McpSession("https://example.test/mcp")

    class Always429:
        async def call_tool(self, name: str, arguments: dict) -> Any:
            session._last_status = 429
            session._last_retry_after = 2.0
            raise ExceptionGroup(
                "unhandled errors in a TaskGroup (1 sub-exception)",
                [_http_error(429, retry_after="2")],
            )

    session._client = Always429()  # type: ignore[assignment]
    with pytest.raises(McpCallError, match="429") as caught:
        await session.call_tool("t", {}, retries=2)
    assert caught.value.status == 429
    assert "TaskGroup" not in str(caught.value) or "429" in str(caught.value)
