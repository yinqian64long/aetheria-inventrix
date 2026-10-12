from __future__ import annotations

from collections.abc import Iterator

import pytest

from flightsearch.sources.apify_routes import clear_exhausted_apify_tokens


@pytest.fixture(autouse=True)
def _reset_exhausted_apify_tokens() -> Iterator[None]:
    clear_exhausted_apify_tokens()
    yield
    clear_exhausted_apify_tokens()
