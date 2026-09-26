from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Mapping
from typing import ClassVar

from flightsearch.context import RunContext
from flightsearch.models import Offer, SearchQuery


class SourceError(Exception):
    """Raised when a source fails for a real (non-empty-result) reason."""


class Source(ABC):
    """Flight search source.

    ``search`` must return parsed offers at any price (cap ~20 cheapest per
    origin-destination). Prices must be converted to USD via ``ctx.fx``. Never
    raise for "no results" — return an empty list. Raise ``SourceError`` for
    real failures.
    """

    name: ClassVar[str]

    def __init__(self, settings: dict | None = None) -> None:
        self.settings = settings or {}

    def is_available(self, env: Mapping[str, str]) -> tuple[bool, str]:
        return True, ""

    @abstractmethod
    async def search(self, query: SearchQuery, ctx: RunContext) -> list[Offer]:
        ...
