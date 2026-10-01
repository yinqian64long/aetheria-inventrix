from __future__ import annotations

from datetime import date, datetime

from flightsearch.hops import UNVERIFIED_NOTE, attach_hops
from flightsearch.models import Offer, SearchQuery

QUERY = SearchQuery(
    home_origin="CNX",
    positioning_origins=("BKK", "DMK", "HKT"),
    destinations=("WAW", "KRK", "GDN", "KTW", "WRO", "POZ", "PRG", "IST"),
    extra_destinations=("WMI",),
    date_from=date(2026, 10, 20),
    date_to=date(2026, 11, 1),
    adults=1,
    cabin_bags=1,
    checked_bags=0,
    max_total_usd=250.0,
    near_miss_usd=300.0,
    currency="USD",
)


def _offer(
    *,
    origin: str,
    destination: str,
    depart_date: date,
    price_usd: float,
    source: str = "kiwi",
    depart_time: datetime | None = None,
    arrive_time: datetime | None = None,
    **kwargs,
) -> Offer:
    return Offer(
        source=source,
        origin=origin,
        destination=destination,
        depart_date=depart_date,
        price_usd=price_usd,
        price_original=price_usd,
        currency_original="USD",
        depart_time=depart_time,
        arrive_time=arrive_time,
        **kwargs,
    )


def _hop(
    dest: str,
    arrive: datetime | None,
    price: float,
    depart_date: date | None = None,
) -> Offer:
    day = depart_date or (arrive.date() if arrive else date(2026, 10, 25))
    return _offer(
        origin="CNX",
        destination=dest,
        depart_date=day,
        price_usd=price,
        depart_time=datetime(day.year, day.month, day.day, 6, 0) if arrive else None,
        arrive_time=arrive,
    )


def _attach(offers: list[Offer], hops: list[Offer]):
    return attach_hops(offers, hops, QUERY, 240, 24)


def test_cheapest_valid_connection_for_bkk_krk() -> None:
    offer = _offer(
        origin="BKK",
        destination="KRK",
        depart_date=date(2026, 10, 25),
        price_usd=200.0,
        depart_time=datetime(2026, 10, 25, 23, 50),
        arrive_time=datetime(2026, 10, 26, 14, 5),
        airlines=["Oman Air", "Ryanair"],
        stops=2,
        self_transfer=True,
    )
    hops = [
        _hop("BKK", datetime(2026, 10, 25, 11, 15), 40.0),
        _hop("BKK", datetime(2026, 10, 25, 7, 10), 35.0),
        _hop("BKK", datetime(2026, 10, 25, 20, 10), 30.0),
    ]
    kept, dropped = _attach([offer], hops)
    assert dropped == []
    assert len(kept) == 1
    assert kept[0].hop is not None
    assert kept[0].hop.price_usd == 35.0
    assert kept[0].hop.arrive_time == datetime(2026, 10, 25, 7, 10)
    assert kept[0].total_usd == 235.0


def test_dmk_offer_cannot_use_cnx_bkk_hop() -> None:
    offer = _offer(
        origin="DMK",
        destination="WAW",
        depart_date=date(2026, 10, 25),
        price_usd=180.0,
        depart_time=datetime(2026, 10, 25, 23, 50),
    )
    hops = [_hop("BKK", datetime(2026, 10, 25, 7, 10), 35.0)]
    kept, dropped = _attach([offer], hops)
    assert kept == []
    assert len(dropped) == 1
    assert dropped[0][1] == "no positioning hop"


def test_overnight_connection_is_valid() -> None:
    offer = _offer(
        origin="BKK",
        destination="WAW",
        depart_date=date(2026, 10, 26),
        price_usd=200.0,
        depart_time=datetime(2026, 10, 26, 1, 30),
    )
    hop = _hop("BKK", datetime(2026, 10, 25, 21, 0), 30.0, depart_date=date(2026, 10, 25))
    kept, dropped = _attach([offer], [hop])
    assert dropped == []
    assert kept[0].hop is not None
    assert kept[0].hop.price_usd == 30.0
    assert kept[0].total_usd == 230.0


def test_hop_arriving_30h_before_is_invalid() -> None:
    offer = _offer(
        origin="BKK",
        destination="WAW",
        depart_date=date(2026, 10, 26),
        price_usd=200.0,
        depart_time=datetime(2026, 10, 26, 1, 30),
    )
    hop = _hop("BKK", datetime(2026, 10, 24, 19, 30), 30.0, depart_date=date(2026, 10, 24))
    kept, dropped = _attach([offer], [hop])
    assert kept == []
    assert dropped[0][1] == "no positioning hop"


def test_cnx_home_origin_kept_regardless_of_price() -> None:
    cheap = _offer(
        origin="CNX",
        destination="WAW",
        depart_date=date(2026, 10, 25),
        price_usd=249.99,
    )
    dear = _offer(
        origin="CNX",
        destination="WAW",
        depart_date=date(2026, 10, 25),
        price_usd=250.01,
    )
    kept, dropped = _attach([cheap, dear], [])
    assert dropped == []
    assert [o.price_usd for o in kept] == [249.99, 250.01]
    assert kept[0].total_usd == 249.99
    assert kept[1].total_usd == 250.01


def test_destination_ber_dropped_wmi_accepted() -> None:
    ber = _offer(
        origin="CNX",
        destination="BER",
        depart_date=date(2026, 10, 25),
        price_usd=199.0,
    )
    wmi = _offer(
        origin="CNX",
        destination="WMI",
        depart_date=date(2026, 10, 25),
        price_usd=199.0,
    )
    kept, dropped = _attach([ber, wmi], [])
    assert [o.destination for o in kept] == ["WMI"]
    assert dropped[0][0].destination == "BER"
    assert dropped[0][1] == "destination not in search"


def test_date_outside_window_and_unsupported_origin() -> None:
    late = _offer(
        origin="CNX",
        destination="WAW",
        depart_date=date(2026, 11, 2),
        price_usd=100.0,
    )
    sin = _offer(
        origin="SIN",
        destination="WAW",
        depart_date=date(2026, 10, 25),
        price_usd=100.0,
    )
    kept, dropped = _attach([late, sin], [])
    assert kept == []
    reasons = {o.destination + o.origin: reason for o, reason in dropped}
    assert reasons["WAWCNX"] == "date outside window"
    assert reasons["WAWSIN"] == "unsupported origin"


def test_missing_times_accept_previous_day_hop_and_note() -> None:
    offer = _offer(
        origin="BKK",
        destination="KRK",
        depart_date=date(2026, 10, 26),
        price_usd=200.0,
    )
    good = _hop("BKK", None, 40.0, depart_date=date(2026, 10, 25))
    same_day = _hop("BKK", None, 20.0, depart_date=date(2026, 10, 26))
    kept, dropped = _attach([offer], [same_day, good])
    assert dropped == []
    assert kept[0].hop is not None
    assert kept[0].hop.price_usd == 40.0
    assert UNVERIFIED_NOTE in kept[0].notes
