from __future__ import annotations

import html
import logging
import re
from typing import Any

import httpx

log = logging.getLogger("flightsearch.notify")

_ANCHOR = re.compile(
    r'<a\s+href="([^"]+)"[^>]*>(.*?)</a>',
    re.IGNORECASE | re.DOTALL,
)
_TAG = re.compile(r"<[^>]+>")


class GrokBotError(Exception):
    """Raised when a Grok Bot webhook call fails."""


def plain_message(text: str) -> str:
    """Turn Telegram HTML into text a Grok Bot chat can show."""

    def anchor(match: re.Match[str]) -> str:
        href = html.unescape(match.group(1)).strip()
        label = _TAG.sub("", match.group(2))
        label = html.unescape(label).strip()
        if label and label != href:
            return f"{label} ({href})"
        return href

    without_links = _ANCHOR.sub(anchor, text)
    return html.unescape(_TAG.sub("", without_links))


def _redact(text: str, key: str) -> str:
    if not key:
        return text
    return text.replace(key, "<redacted>")


class GrokBotNotifier:
    def __init__(self, url: str, key: str, http: httpx.AsyncClient) -> None:
        self.url = url
        self.key = key
        self.http = http

    async def send(self, text: str, parse_mode: str = "HTML") -> None:
        del parse_mode
        payload = {"message": plain_message(text)}
        headers = {"Authorization": f"Bearer {self.key}"}
        try:
            resp = await self.http.post(self.url, json=payload, headers=headers)
        except Exception as exc:
            raise GrokBotError(_redact(f"Grok Bot send failed: {exc}", self.key)) from None
        if resp.status_code != 200:
            raise GrokBotError(self._error_message(resp))

    def _error_message(self, resp: httpx.Response) -> str:
        body = ""
        try:
            parsed: Any = resp.json()
            if isinstance(parsed, dict):
                body = str(parsed.get("error") or parsed.get("message") or "")
        except Exception:
            body = (resp.text or "")[:200]
        msg = f"Grok Bot send failed: HTTP {resp.status_code}"
        if body:
            msg = f"{msg} {body}"
        return _redact(msg, self.key)
