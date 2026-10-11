from __future__ import annotations

from datetime import date

from flightsearch.hops import CHECKED_BAG_NOTE, attach_hops
from flightsearch.models import Offer, SearchQuery

QUERY = SearchQuery(
    origins=("BKK", "CNX", "HKT"),
    destinations=("SGN", "HAN"),
    priority_routes=(),
    date_from=date(2026, 10, 28),
    date_to=date(2026, 10, 31),
    adults=1,
    cabin_bags=1,
    checked_bags=1,
    max_total_usd=110.0,
    near_miss_usd=130.0,
    currency="USD",
)


def _offer(
    *,
    origin: str,
    destination: str,
    depart_date: date,
    price_usd: float,
    notes: str = "",
    checked_bag_included: bool | None = None,
) -> Offer:
    return Offer(
        source="kiwi",
        origin=origin,
        destination=destination,
        depart_date=depart_date,
        price_usd=price_usd,
        price_original=price_usd,
        currency_original="USD",
        notes=notes,
        checked_bag_included=checked_bag_included,
    )


def test_keeps_offers_inside_the_route_and_window() -> None:
    kept, dropped = attach_hops(
        [
            _offer(origin="bkk", destination="sgn", depart_date=date(2026, 10, 28), price_usd=80),
            _offer(origin="CNX", destination="HAN", depart_date=date(2026, 10, 31), price_usd=90),
        ],
        QUERY,
    )
    assert dropped == []
    assert [(o.origin, o.destination) for o in kept] == [("bkk", "sgn"), ("CNX", "HAN")]


def test_drops_unknown_airport_and_date() -> None:
    late = _offer(origin="BKK", destination="SGN", depart_date=date(2026, 11, 2), price_usd=50)
    sin = _offer(origin="SIN", destination="SGN", depart_date=date(2026, 10, 28), price_usd=50)
    dad = _offer(origin="BKK", destination="DAD", depart_date=date(2026, 10, 28), price_usd=50)
    kept, dropped = attach_hops([late, sin, dad], QUERY)
    assert kept == []
    reasons = {(offer.origin, offer.destination): reason for offer, reason in dropped}
    assert reasons[("BKK", "SGN")] == "date outside window"
    assert reasons[("SIN", "SGN")] == "unsupported origin"
    assert reasons[("BKK", "DAD")] == "destination not in search"


def test_unknown_checked_bag_is_noted() -> None:
    known = _offer(
        origin="BKK",
        destination="SGN",
        depart_date=date(2026, 10, 28),
        price_usd=70,
        checked_bag_included=True,
        notes="fare",
    )
    unknown = _offer(
        origin="CNX",
        destination="HAN",
        depart_date=date(2026, 10, 29),
        price_usd=75,
        notes="bags not verified (Google)",
    )
    kept, _dropped = attach_hops([known, unknown], QUERY)
    assert kept[0].notes == "fare"
    assert CHECKED_BAG_NOTE in kept[1].notes
    assert "bags not verified (Google)" in kept[1].notes
