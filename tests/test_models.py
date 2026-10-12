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
        origins=("BKK", "CNX"),
        destinations=("SGN", "HAN"),
        priority_routes=(("BKK", "SGN"),),
        date_from=date(2026, 10, 20),
        date_to=date(2026, 10, 22),
        adults=1,
        cabin_bags=1,
        checked_bags=1,
        max_total_usd=110.0,
        near_miss_usd=130.0,
        currency="USD",
    )
    assert query.all_origins == ("BKK", "CNX")
    assert query.all_destinations == ("SGN", "HAN")
    assert query.route_pairs() == [("BKK", "SGN"), ("BKK", "HAN"), ("CNX", "SGN"), ("CNX", "HAN")]
    assert query.route_pairs(priority=True) == [("BKK", "SGN")]
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
    assert cfg.query.origins == ("BKK", "DMK", "CNX", "HKT", "KBV", "USM", "HDY", "CEI")
    assert cfg.query.destinations == ("SGN", "HAN", "DAD", "CXR", "PQC", "HPH")
    assert ("BKK", "SGN") in cfg.query.priority_routes
    assert ("HKT", "HAN") in cfg.query.priority_routes
    assert cfg.query.date_from == date(2026, 10, 28)
    assert cfg.query.date_to == date(2026, 10, 31)
    assert cfg.query.adults == 1
    assert cfg.query.cabin_bags == 1
    assert cfg.query.checked_bags == 1
    assert cfg.query.max_total_usd == 110
    assert cfg.query.near_miss_usd == 130
    assert cfg.query.currency == "USD"
    assert cfg.sources["skiplagged"]["max_detail_searches"] == 12
    assert cfg.sources["agoda"]["enabled"] is True
    assert cfg.sources["rpl"]["enabled"] is False
    assert cfg.notify["telegram"]["heartbeat_utc_hour"] == 0
    assert cfg.apify_max_concurrent_runs == 3
