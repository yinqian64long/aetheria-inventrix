from __future__ import annotations

import importlib
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from flightsearch.sources.base import Source

SOURCE_CLASSES: dict[str, str] = {
    "kiwi": "flightsearch.sources.kiwi:KiwiSource",
    "skiplagged": "flightsearch.sources.skiplagged:SkiplaggedSource",
    "google": "flightsearch.sources.google:GoogleFlightsSource",
    "letsfg": "flightsearch.sources.letsfg:LetsFGSource",
    "skyscanner": "flightsearch.sources.skyscanner:SkyscannerSource",
    "rpl": "flightsearch.sources.rpl:RplSource",
    "itaka": "flightsearch.sources.itaka:ItakaSource",
    "chartershop": "flightsearch.sources.chartershop:ChartershopSource",
    "trip": "flightsearch.sources.trip:TripSource",
}


def load_source(name: str, settings: dict) -> Source:
    """Lazily import and instantiate a source by registry name."""
    try:
        target = SOURCE_CLASSES[name]
    except KeyError as exc:
        raise KeyError(f"Unknown source: {name}") from exc
    module_path, class_name = target.split(":", 1)
    module = importlib.import_module(module_path)
    cls = getattr(module, class_name)
    return cls(settings)
