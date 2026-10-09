from __future__ import annotations

from datetime import date, datetime
from pathlib import Path

from flightsearch.config import load_config
from flightsearch.models import Offer, SearchQuery

ROOT = Path(__file__).resolve().parents[1]


def test_offer_total_usd_and_key_round_trip() -> None:
    hop = Offer(
        source="kiwi",
        origin="CNX",
        destination="BKK",
        depart_date=date(2026, 10, 20),
        price_usd=40.555,
        price_original=40.555,
        currency_original="USD",
        depart_time=datetime(2026, 10, 20, 8, 0),
        airlines=["TG"],
        flight_numbers=["TG103"],
    )
    offer = Offer(
        source="kiwi",
        origin="BKK",
        destination="WAW",
        depart_date=date(2026, 10, 21),
        price_usd=199.449,
        price_original=199.449,
        currency_original="USD",
        depart_time=datetime(2026, 10, 21, 14, 30),
        airlines=["LO"],
        flight_numbers=["LO68"],
        hop=hop,
    )

    assert offer.total_usd == 240.0
    assert "hop:" in offer.key()
    assert hop.key() in offer.key()

    restored = Offer.from_dict(offer.to_dict())
    assert restored.total_usd == offer.total_usd
    assert restored.key() == offer.key()
    assert restored.hop is not None
    assert restored.hop.flight_numbers == ["TG103"]
    assert restored.depart_date == date(2026, 10, 21)
    assert restored.depart_time == datetime(2026, 10, 21, 14, 30)


def test_offer_key_without_hop_uses_airlines_fallback() -> None:
    offer = Offer(
        source="google",
        origin="CNX",
        destination="KRK",
        depart_date=date(2026, 10, 25),
        price_usd=250.0,
        price_original=250.0,
        currency_original="USD",
        airlines=["QR", "LO"],
    )
    assert offer.key() == "google|CNX-KRK|2026-10-25||QR,LO"


def test_search_query_dates() -> None:
    query = SearchQuery(
        home_origin="CNX",
        positioning_origins=("BKK",),
        destinations=("WAW",),
        extra_destinations=("WMI",),
        date_from=date(2026, 10, 20),
        date_to=date(2026, 10, 22),
        adults=1,
        cabin_bags=1,
        checked_bags=0,
        max_total_usd=250.0,
        near_miss_usd=300.0,
        currency="USD",
    )
    assert query.all_origins == ("CNX", "BKK")
    assert query.all_destinations == ("WAW", "WMI")
    assert query.dates(date(2026, 10, 21)) == [
        date(2026, 10, 21),
        date(2026, 10, 22),
    ]
    assert query.dates(date(2026, 10, 20)) == [
        date(2026, 10, 20),
        date(2026, 10, 21),
        date(2026, 10, 22),
    ]
    assert query.dates(date(2026, 10, 23)) == []


def test_load_config_real_yaml() -> None:
    cfg = load_config(ROOT / "config.yaml")
    assert cfg.query.home_origin == "CNX"
    assert cfg.query.positioning_origins == ("BKK", "DMK", "HKT")
    assert cfg.query.destinations == ("WAW", "KRK", "GDN", "KTW", "WRO", "POZ", "PRG", "IST", "BUD")
    assert cfg.query.extra_destinations == ("WMI",)
    assert cfg.query.date_from == date(2026, 10, 20)
    assert cfg.query.date_to == date(2026, 11, 1)
    assert cfg.query.adults == 1
    assert cfg.query.cabin_bags == 1
    assert cfg.query.checked_bags == 0
    assert cfg.query.max_total_usd == 250
    assert cfg.query.near_miss_usd == 300
    assert cfg.query.currency == "USD"
    assert cfg.hop_min_connection_minutes == 240
    assert cfg.hop_max_connection_hours == 24
    assert cfg.hop_fetch_timeout_s == 360
    assert cfg.sources["skiplagged"]["max_detail_searches"] == 12
    assert cfg.notify["telegram"]["heartbeat_utc_hour"] == 0
