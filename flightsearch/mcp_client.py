from __future__ import annotations

import asyncio
import json
import random
import re
from contextlib import AsyncExitStack
from typing import Any

import httpx2
from mcp import Client
from mcp.client.streamable_http import streamable_http_client
from mcp.shared._httpx_utils import create_mcp_http_client


class McpCallError(Exception):
    """Raised when an MCP tool call fails or returns an error result."""

    def __init__(
        self,
        message: str,
        *,
        status: int | None = None,
        retry_after: float | None = None,
    ) -> None:
        super().__init__(message)
        self.status = status
        self.retry_after = retry_after


_RETRY_DELAYS = (1.0, 3.0)
_MAX_RETRY_AFTER_S = 45.0
_RETRY_STATUSES = frozenset({403, 408, 425, 429, 500, 502, 503, 504, 522, 524})
_STATUS_IN_TEXT = re.compile(
    r"(?:HTTP(?:/\d+(?:\.\d+)?)?\s+)?([1-5]\d{2})\s+"
    r"(?:Too Many|Forbidden|Bad Gateway|Service Unavailable|Gateway Timeout|"
    r"Internal Server|Cloudflare|Request Timeout)",
    re.IGNORECASE,
)
_SESSION_DEAD = (
    "session terminated",
    "connection reset",
    "connection closed",
    "server disconnected",
    "broken pipe",
    "closedresource",
    "endofstream",
)


def unwrap_exception_group(exc: BaseException) -> list[BaseException]:
    """Flatten ``BaseExceptionGroup`` / ``ExceptionGroup`` down to leaf exceptions."""
    if isinstance(exc, BaseExceptionGroup):
        leaves: list[BaseException] = []
        for inner in exc.exceptions:
            leaves.extend(unwrap_exception_group(inner))
        return leaves or [exc]
    return [exc]


def _parse_retry_after(raw: Any) -> float | None:
    if raw is None:
        return None
    text = str(raw).strip()
    if not text:
        return None
    try:
        return max(0.0, float(text))
    except ValueError:
        return None


def _status_from_exc(exc: BaseException) -> int | None:
    for leaf in unwrap_exception_group(exc):
        status = getattr(leaf, "status", None)
        if isinstance(status, int) and 100 <= status <= 599:
            return status
        response = getattr(leaf, "response", None)
        if response is not None:
            code = getattr(response, "status_code", None)
            if isinstance(code, int) and 100 <= code <= 599:
                return code
        code = getattr(leaf, "status_code", None)
        if isinstance(code, int) and 100 <= code <= 599:
            return code
        match = _STATUS_IN_TEXT.search(str(leaf))
        if match:
            return int(match.group(1))
    return None


def _retry_after_from_exc(exc: BaseException) -> float | None:
    own = getattr(exc, "retry_after", None)
    if isinstance(own, (int, float)):
        return float(own)
    for leaf in unwrap_exception_group(exc):
        value = getattr(leaf, "retry_after", None)
        if isinstance(value, (int, float)):
            return float(value)
        response = getattr(leaf, "response", None)
        headers = getattr(response, "headers", None) if response is not None else None
        if headers is not None:
            parsed = _parse_retry_after(
                headers.get("Retry-After") or headers.get("retry-after")
            )
            if parsed is not None:
                return parsed
    return None


def format_mcp_error(exc: BaseException, *, status: int | None = None) -> str:
    """Human-readable leaf error, including HTTP status when known."""
    leaves = unwrap_exception_group(exc)
    parts: list[str] = []
    for leaf in leaves:
        leaf_status = _status_from_exc(leaf) or status
        text = str(leaf).strip() or type(leaf).__name__
        if leaf_status is not None and str(leaf_status) not in text:
            parts.append(f"HTTP {leaf_status}: {text}")
        else:
            parts.append(text)
    return "; ".join(parts) if parts else type(exc).__name__


def _session_is_dead(exc: BaseException) -> bool:
    for leaf in unwrap_exception_group(exc):
        name = type(leaf).__name__.lower()
        if name in {"closedresourceerror", "endofstream", "brokenresourceerror"}:
            return True
        text = str(leaf).lower()
        if any(marker in text or marker in name for marker in _SESSION_DEAD):
            return True
    return False


def _is_connect_or_timeout(exc: BaseException) -> bool:
    for leaf in unwrap_exception_group(exc):
        if isinstance(leaf, (TimeoutError, ConnectionError, OSError, asyncio.TimeoutError)):
            return True
        name = type(leaf).__name__.lower()
        if any(token in name for token in ("timeout", "connect", "connection")):
            return True
        text = str(leaf).lower()
        if "timed out" in text or "timeout" in text:
            return True
        if "connect" in text and "error" in text:
            return True
    return False


def is_retryable_mcp_error(
    exc: BaseException, *, status: int | None = None
) -> bool:
    code = status if status is not None else _status_from_exc(exc)
    if code is not None:
        if code in _RETRY_STATUSES or 500 <= code <= 599:
            return True
        if code == 404 and _session_is_dead(exc):
            return True
        return False
    if _session_is_dead(exc) or _is_connect_or_timeout(exc):
        return True
    text = format_mcp_error(exc).lower()
    return "server returned an error response" in text


def _backoff_s(
    attempt: int,
    retry_after: float | None,
    *,
    status: int | None = None,
) -> float:
    # Honor Retry-After only for 429. 522/5xx often advertise long waits while
    # the origin is simply down; short backoff + retry is enough.
    if status == 429 and retry_after is not None:
        base = min(float(retry_after), _MAX_RETRY_AFTER_S)
        return base + random.uniform(0.0, 0.5)
    base = _RETRY_DELAYS[min(attempt, len(_RETRY_DELAYS) - 1)]
    return max(0.0, base * (0.75 + 0.5 * random.random()))


def _parse_tool_content(result: Any) -> Any:
    for block in result.content or []:
        text = getattr(block, "text", None)
        if text is None:
            continue
        try:
            return json.loads(text)
        except json.JSONDecodeError:
            return {"text": text}
    return {}


class McpSession:
    """Long-lived MCP connection that reconnects and retries transient failures."""

    def __init__(
        self,
        url: str,
        *,
        headers: dict | None = None,
        timeout: float = 120,
        legacy: bool = False,
    ) -> None:
        self.url = url
        self.headers = headers
        self.timeout = timeout
        self.legacy = legacy
        self._client: Client | None = None
        self._stack: AsyncExitStack | None = None
        self._connect_lock = asyncio.Lock()
        self._last_status: int | None = None
        self._last_retry_after: float | None = None

    async def _capture_response(self, response: Any) -> None:
        try:
            self._last_status = int(response.status_code)
        except (TypeError, ValueError, AttributeError):
            return
        headers = getattr(response, "headers", None)
        raw = None
        if headers is not None:
            raw = headers.get("Retry-After") or headers.get("retry-after")
        self._last_retry_after = _parse_retry_after(raw)

    async def __aenter__(self) -> McpSession:
        last_error: BaseException | None = None
        last_status: int | None = None
        last_retry_after: float | None = None
        for attempt in range(2):
            try:
                await self._connect()
                return self
            except BaseException as exc:
                status, retry_after = self._classify(exc)
                last_error = exc
                last_status = status
                last_retry_after = retry_after
                if not is_retryable_mcp_error(exc, status=status) or attempt == 1:
                    raise McpCallError(
                        f"MCP connect failed: {format_mcp_error(exc, status=status)}",
                        status=status,
                        retry_after=retry_after,
                    ) from exc
                await asyncio.sleep(_backoff_s(attempt, retry_after, status=status))
        assert last_error is not None
        raise McpCallError(
            f"MCP connect failed: {format_mcp_error(last_error, status=last_status)}",
            status=last_status,
            retry_after=last_retry_after,
        ) from last_error

    async def __aexit__(self, exc_type: type[BaseException] | None, exc: BaseException | None, tb: Any) -> bool:
        try:
            await self._close()
        except BaseException as close_exc:
            if isinstance(close_exc, BaseExceptionGroup):
                # anyio TaskGroup teardown noise — keep the body exception if any.
                return exc_type is None
            if exc_type is not None:
                return False
            raise
        return False

    async def _connect(self) -> None:
        await self._close()
        stack = AsyncExitStack()
        await stack.__aenter__()
        try:
            mode = "legacy" if self.legacy else "auto"
            http_timeout = httpx2.Timeout(45.0, read=max(float(self.timeout), 300.0))
            http = create_mcp_http_client(
                headers=dict(self.headers) if self.headers else None,
                timeout=http_timeout,
            )
            http.event_hooks.setdefault("response", []).append(self._capture_response)
            await stack.enter_async_context(http)
            transport = streamable_http_client(self.url, http_client=http)
            client = Client(transport, read_timeout_seconds=self.timeout, mode=mode)
            self._client = await stack.enter_async_context(client)
            self._stack = stack
        except BaseException:
            try:
                await stack.aclose()
            except BaseException:
                pass
            self._client = None
            self._stack = None
            raise

    async def _close(self) -> None:
        stack = self._stack
        self._stack = None
        self._client = None
        if stack is None:
            return
        try:
            await stack.aclose()
        except BaseException as exc:
            if isinstance(exc, BaseExceptionGroup):
                return
            raise

    async def _ensure_open(self) -> Client:
        if self._client is not None:
            return self._client
        async with self._connect_lock:
            if self._client is None:
                await self._connect()
            assert self._client is not None
            return self._client

    async def _invalidate(self) -> None:
        async with self._connect_lock:
            await self._close()

    def _classify(self, exc: BaseException) -> tuple[int | None, float | None]:
        status = _status_from_exc(exc) or self._last_status
        retry_after = _retry_after_from_exc(exc)
        if retry_after is None and status == 429:
            retry_after = self._last_retry_after
        return status, retry_after

    async def call_tool(
        self,
        tool: str,
        args: dict,
        *,
        retries: int = 2,
    ) -> Any:
        last_error: BaseException | None = None
        last_status: int | None = None
        last_retry_after: float | None = None
        attempts = retries + 1
        for attempt in range(attempts):
            self._last_status = None
            self._last_retry_after = None
            try:
                client = await self._ensure_open()
                result = await client.call_tool(tool, args)
                is_error = bool(
                    getattr(result, "is_error", False)
                    or getattr(result, "isError", False)
                )
                if is_error:
                    raise McpCallError(f"MCP tool {tool!r} returned an error result")
                return _parse_tool_content(result)
            except McpCallError as exc:
                if exc.status is None or not is_retryable_mcp_error(exc, status=exc.status):
                    raise
                last_error = exc
                last_status = exc.status
                last_retry_after = exc.retry_after
            except BaseException as exc:
                status, retry_after = self._classify(exc)
                last_error = exc
                last_status = status
                last_retry_after = retry_after
                if not is_retryable_mcp_error(exc, status=status):
                    raise McpCallError(
                        f"MCP tool {tool!r} failed: {format_mcp_error(exc, status=status)}",
                        status=status,
                        retry_after=retry_after,
                    ) from exc
                if _session_is_dead(exc):
                    await self._invalidate()
            if attempt < attempts - 1:
                await asyncio.sleep(
                    _backoff_s(attempt, last_retry_after, status=last_status)
                )
                continue
        assert last_error is not None
        raise McpCallError(
            f"MCP tool {tool!r} failed after {attempts} attempts: "
            f"{format_mcp_error(last_error, status=last_status)}",
            status=last_status,
            retry_after=last_retry_after,
        ) from last_error

    async def list_tools(self) -> list[dict]:
        client = await self._ensure_open()
        result = await client.list_tools()
        tools: list[dict] = []
        for t in result.tools:
            dumped = t.model_dump(by_alias=True) if hasattr(t, "model_dump") else {}
            tools.append(
                {
                    "name": t.name,
                    "description": getattr(t, "description", None) or "",
                    "inputSchema": dumped.get("inputSchema")
                    or getattr(t, "input_schema", None)
                    or {},
                }
            )
        return tools


async def call_mcp_tool(
    url: str,
    tool: str,
    args: dict,
    *,
    headers: dict | None = None,
    timeout: float = 120,
    retries: int = 2,
    legacy: bool = False,
) -> Any:
    async with McpSession(
        url, headers=headers, timeout=timeout, legacy=legacy
    ) as session:
        return await session.call_tool(tool, args, retries=retries)


async def list_mcp_tools(
    url: str,
    *,
    headers: dict | None = None,
    legacy: bool = False,
) -> list[dict]:
    async with McpSession(url, headers=headers, timeout=120, legacy=legacy) as session:
        return await session.list_tools()
