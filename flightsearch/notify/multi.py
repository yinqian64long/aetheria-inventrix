from __future__ import annotations

import logging
from typing import Any

log = logging.getLogger("flightsearch.notify")


class MultiNotifier:
    """Deliver one alert to every configured channel.

    A channel that fails is logged. The send fails only when every channel fails,
    so one broken webhook does not drop a message that another channel accepted.
    """

    def __init__(self, notifiers: list[Any]) -> None:
        self.notifiers = list(notifiers)

    async def send(self, text: str, parse_mode: str = "HTML") -> None:
        errors: list[Exception] = []
        for notifier in self.notifiers:
            try:
                await notifier.send(text, parse_mode=parse_mode)
            except Exception as exc:
                log.warning("notify failed: %s", exc)
                errors.append(exc)
        if self.notifiers and len(errors) == len(self.notifiers):
            raise errors[0]
