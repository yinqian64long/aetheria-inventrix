from __future__ import annotations

import asyncio
import logging
from typing import Any

import httpx

log = logging.getLogger("flightsearch.notify")

SPLIT_LIMIT = 4000
API_ROOT = "https://api.telegram.org"


class TelegramError(Exception):
    """Raised when a Telegram API call fails."""


def _redact(text: str, token: str) -> str:
    if not token:
        return text
    return text.replace(token, "<redacted>")


def _split_text(text: str, limit: int = SPLIT_LIMIT) -> list[str]:
    if len(text) <= limit:
        return [text] if text else [""]
    chunks: list[str] = []
    current: list[str] = []
    current_len = 0
    for line in text.splitlines(keepends=True):
        if current and current_len + len(line) > limit:
            chunks.append("".join(current))
            current = []
            current_len = 0
        if len(line) > limit:
            if current:
                chunks.append("".join(current))
                current = []
                current_len = 0
            for i in range(0, len(line), limit):
                chunks.append(line[i : i + limit])
            continue
        current.append(line)
        current_len += len(line)
    if current:
        chunks.append("".join(current))
    return chunks or [""]


def _retry_after(resp: httpx.Response) -> float:
    try:
        payload = resp.json()
        params = payload.get("parameters") if isinstance(payload, dict) else None
        if isinstance(params, dict) and params.get("retry_after") is not None:
            return max(0.0, float(params["retry_after"]))
    except Exception:
        pass
    header = resp.headers.get("Retry-After")
    if header:
        try:
            return max(0.0, float(header))
        except ValueError:
            return 0.0
    return 0.0


def _api_url(token: str, method: str) -> str:
    return f"{API_ROOT}/bot{token}/{method}"


class TelegramNotifier:
    def __init__(self, token: str, chat_id: str | int, http: httpx.AsyncClient) -> None:
        self.token = token
        self.chat_id = chat_id
        self.http = http

    async def send(self, text: str, parse_mode: str = "HTML") -> None:
        for chunk in _split_text(text):
            await self._send_chunk(chunk, parse_mode, retried=False)

    async def _send_chunk(self, text: str, parse_mode: str, *, retried: bool) -> None:
        url = _api_url(self.token, "sendMessage")
        payload = {
            "chat_id": self.chat_id,
            "text": text,
            "parse_mode": parse_mode,
            "disable_web_page_preview": True,
        }
        try:
            resp = await self.http.post(url, json=payload)
        except Exception as exc:
            raise TelegramError(_redact(f"Telegram send failed: {exc}", self.token)) from None
        if resp.status_code == 429 and not retried:
            delay = _retry_after(resp)
            log.warning("Telegram 429; retrying after %s s", delay)
            await asyncio.sleep(delay)
            await self._send_chunk(text, parse_mode, retried=True)
            return
        if resp.status_code != 200:
            raise TelegramError(self._error_message(resp))
        try:
            data = resp.json()
        except Exception as exc:
            raise TelegramError(_redact(f"Telegram send failed: {exc}", self.token)) from None
        if not isinstance(data, dict) or not data.get("ok"):
            raise TelegramError(self._error_message(resp, data if isinstance(data, dict) else None))

    def _error_message(self, resp: httpx.Response, data: dict[str, Any] | None = None) -> str:
        desc = ""
        if data is None:
            try:
                parsed = resp.json()
                if isinstance(parsed, dict):
                    desc = str(parsed.get("description") or "")
            except Exception:
                desc = (resp.text or "")[:200]
        else:
            desc = str(data.get("description") or "")
        msg = f"Telegram send failed: HTTP {resp.status_code}"
        if desc:
            msg = f"{msg} {desc}"
        return _redact(msg, self.token)


async def get_chat_ids(token: str, http: httpx.AsyncClient) -> list[dict]:
    url = _api_url(token, "getUpdates")
    try:
        resp = await http.get(url)
    except Exception as exc:
        raise TelegramError(_redact(f"Telegram getUpdates failed: {exc}", token)) from None
    if resp.status_code != 200:
        raise TelegramError(_redact(f"Telegram getUpdates failed: HTTP {resp.status_code}", token))
    try:
        data = resp.json()
    except Exception as exc:
        raise TelegramError(_redact(f"Telegram getUpdates failed: {exc}", token)) from None
    if not isinstance(data, dict) or not data.get("ok"):
        desc = ""
        if isinstance(data, dict):
            desc = str(data.get("description") or "")
        raise TelegramError(_redact(f"Telegram getUpdates failed: {desc}".strip(), token))
    chats: dict[int | str, dict] = {}
    for update in data.get("result") or []:
        if not isinstance(update, dict):
            continue
        for chat in _chats_in_update(update):
            chats[chat["id"]] = chat
    return list(chats.values())


def _chats_in_update(update: dict[str, Any]) -> list[dict]:
    found: list[dict] = []
    for key in (
        "message",
        "edited_message",
        "channel_post",
        "edited_channel_post",
        "my_chat_member",
        "chat_member",
    ):
        block = update.get(key)
        if isinstance(block, dict):
            chat = _normalize_chat(block.get("chat"))
            if chat:
                found.append(chat)
    callback = update.get("callback_query")
    if isinstance(callback, dict):
        message = callback.get("message")
        if isinstance(message, dict):
            chat = _normalize_chat(message.get("chat"))
            if chat:
                found.append(chat)
    return found


def _normalize_chat(raw: Any) -> dict | None:
    if not isinstance(raw, dict) or raw.get("id") is None:
        return None
    return {
        "id": raw["id"],
        "type": raw.get("type") or "",
        "username": raw.get("username"),
        "title": raw.get("title"),
    }
