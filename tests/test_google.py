from __future__ import annotations

import json
import logging
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import httpx
import pytest

from flightsearch.context import RunContext
from flightsearch.models import SearchQuery
from flightsearch.sources.base import SourceError
from flightsearch.sources import google as google_mod
from flightsearch.sources.google import (
    GoogleFlightsSource,
    calendar_offer,
    flight_to_offer,
    google_flights_url,
    plan_date_jobs,
)

FIXTURES = Path(__file__).resolve().parent / "fixtures" / "google"


class FakeFx:
    def __init__(self, rate: float = 1.1) -> None:
        self.rate = rate
        self.calls: list[tuple[float, str]] = []

    def to_usd(self, amount: float, currency: str) -> float:
        self.calls.append((amount, currency.upper()))
        if currency.upper() == "USD":
            return float(amount)
        return round(float(amount) * self.rate, 2)


def _load(name: str) -> Any:
    path = FIXTURES / name
    if path.suffix == ".json":
        return json.loads(path.read_text(encoding="utf-8"))
    return path.read_text(encoding="utf-8")


def _query(**overrides: Any) -> SearchQuery:
    base: dict[str, Any] = dict(
        home_origin="BKK",
        positioning_origins=(),
        destinations=("WAW",),
        extra_destinations=(),
        date_from=date(2026, 10, 20),
        date_to=date(2026, 10, 22),
        adults=1,
        cabin_bags=1,
        checked_bags=0,
        max_total_usd=250.0,
        near_miss_usd=300.0,
        currency="USD",
    )
    base.update(overrides)
    return SearchQuery(**base)


def _ctx(
    fx: FakeFx | None = None,
    now: datetime | None = None,
    state: dict[str, Any] | None = None,
) -> RunContext:
    return RunContext(
        http=httpx.AsyncClient(),
        fx=fx or FakeFx(),
        state={} if state is None else state,
        log=logging.getLogger("test.google"),
        now=now or datetime(2026, 9, 25, 12, 0, tzinfo=timezone.utc),
        env={},
    )


def _leg(raw: dict[str, Any]) -> SimpleNamespace:
    return SimpleNamespace(
        airline=SimpleNamespace(name=raw["airline"]),
        flight_number=raw["flight_number"],
        departure_datetime=datetime.fromisoformat(raw["departure_datetime"]),
        arrival_datetime=datetime.fromisoformat(raw["arrival_datetime"]),
    )


def _flight(raw: dict[str, Any], **extra: Any) -> SimpleNamespace:
    return SimpleNamespace(
        price=raw["price"],
        currency=raw["currency"],
        stops=raw["stops"],
        duration=raw["duration"],
        self_transfer=raw.get("self_transfer"),
        mixed_cabin=raw.get("mixed_cabin"),
        legs=[_leg(leg) for leg in raw["legs"]],
        **extra,
    )


def _date_row(raw: dict[str, Any]) -> SimpleNamespace:
    return SimpleNamespace(
        date=(datetime.fromisoformat(raw["date"]),),
        price=raw["price"],
        currency=raw["currency"],
    )


def test_flight_to_offer_maps_fields() -> None:
    raw = _load("flight_results.json")["BKK-WAW-2026-10-20"][0]
    fx = FakeFx()
    offer = flight_to_offer(
        _flight(raw),
        origin="BKK",
        destination="WAW",
        depart_date=date(2026, 10, 20),
        query=_query(),
        fx=fx,
        link="https://www.google.com/travel/flights",
    )
    assert offer is not None
    assert offer.source == "google"
    assert offer.origin == "BKK"
    assert offer.destination == "WAW"
    assert offer.depart_date == date(2026, 10, 20)
    assert offer.price_usd == 238.0
    assert offer.price_original == 238.0
    assert offer.currency_original == "USD"
    assert offer.depart_time == datetime(2026, 10, 20, 1, 25)
    assert offer.arrive_time == datetime(2026, 10, 20, 13, 25)
    assert offer.airlines == ["QR"]
    assert offer.flight_numbers == ["QR837", "QR263"]
    assert offer.stops == 1
    assert offer.duration_minutes == 900
    assert offer.link and offer.link.startswith("https://")
    assert offer.cabin_bag_included is None
    assert "bags not verified (Google)" in offer.notes
    assert offer.self_transfer is False
    assert fx.calls == [(238.0, "USD")]


def test_flight_to_offer_uses_explicit_baggage() -> None:
    raw = _load("flight_results.json")["BKK-WAW-2026-10-20"][0]
    offer = flight_to_offer(
        _flight(raw, cabin_bag_included=True),
        origin="BKK",
        destination="WAW",
        depart_date=date(2026, 10, 20),
        query=_query(),
        fx=FakeFx(),
        link="https://www.google.com/travel/flights",
    )
    assert offer is not None
    assert offer.cabin_bag_included is True
    assert "bags not verified" not in offer.notes


def test_calendar_offer_has_no_times() -> None:
    from flightsearch.sources.google import _DateCell

    fx = FakeFx(rate=1.1)
    offer = calendar_offer(
        _DateCell("BKK", "WAW", date(2026, 10, 20), 200.0, "EUR"),
        query=_query(),
        fx=fx,
        link="https://www.google.com/travel/flights",
    )
    assert offer.depart_time is None
    assert offer.arrive_time is None
    assert offer.cabin_bag_included is None
    assert "calendar fare" in offer.notes
    assert "bags not verified (Google)" in offer.notes
    assert offer.price_original == 200.0
    assert offer.currency_original == "EUR"
    assert offer.price_usd == 220.0
    assert fx.calls == [(200.0, "EUR")]


def test_google_flights_url_is_constructible() -> None:
    url = google_flights_url("BKK", "WAW", date(2026, 10, 20))
    assert "google.com/travel/flights" in url
    assert "BKK" in url or "tfs=" in url


def test_empty_wrb_fixture_documents_error_13() -> None:
    body = _load("empty_wrb.txt")
    assert "[13" in body
    assert "wrb.fr" in body
    assert "ErrorResponse" in body


@pytest.mark.asyncio
async def test_search_details_and_calendar(monkeypatch: pytest.MonkeyPatch) -> None:
    dates = _load("date_prices.json")
    flights = _load("flight_results.json")
    date_calls: list[tuple[str, str]] = []
    flight_calls: list[tuple[str, str, str]] = []

    def fake_dates(
        origin: str, dest: str, date_from: date, date_to: date, *_args: Any, **_kwargs: Any
    ) -> list[Any]:
        date_calls.append((origin, dest, date_from))
        key = f"{origin}-{dest}"
        out: list[Any] = []
        for row in dates.get(key, []):
            day = date.fromisoformat(row["date"])
            if date_from <= day <= date_to:
                out.append(
                    google_mod._DateCell(origin, dest, day, row["price"], row["currency"])
                )
        return out

    def fake_flights(
        origin: str, dest: str, depart: date, *_args: Any, **_kwargs: Any
    ) -> list[Any]:
        flight_calls.append((origin, dest, depart.isoformat()))
        rows = flights.get(f"{origin}-{dest}-{depart.isoformat()}", [])
        return [_flight(row) for row in rows]

    monkeypatch.setattr(google_mod, "search_date_prices", fake_dates)
    monkeypatch.setattr(google_mod, "search_flight_results", fake_flights)

    src = GoogleFlightsSource(
        {"max_detail_searches": 2, "concurrency": 2, "jitter_s": (0, 0)}
    )
    query = _query(destinations=("WAW", "KRK"))
    ctx = _ctx()
    try:
        offers = await src.search(query, ctx)
    finally:
        await ctx.http.aclose()

    assert {(o, d) for o, d, _day in date_calls} == {("BKK", "WAW"), ("BKK", "KRK")}
    assert len(date_calls) == 6
    # Cheapest cells ≤ 300: KRK 210, WAW 240 — then WAW 255 is calendar-only.
    assert set(flight_calls) == {("BKK", "KRK", "2026-10-20"), ("BKK", "WAW", "2026-10-20")}

    detailed = [o for o in offers if o.depart_time is not None]
    calendar = [o for o in offers if "calendar fare" in o.notes]
    assert detailed
    assert any(o.flight_numbers == ["LO68"] and o.origin == "BKK" for o in detailed)
    assert any(o.price_usd == 238.0 and o.airlines == ["QR"] for o in detailed)
    assert all(o.cabin_bag_included is None for o in offers)
    assert all("bags not verified (Google)" in o.notes for o in offers)
    assert calendar
    assert all(o.depart_time is None for o in calendar)
    assert {o.depart_date for o in calendar} >= {date(2026, 10, 21), date(2026, 10, 22)}
    assert all(o.currency_original == "USD" for o in offers)
    assert all(o.link and "google.com/travel/flights" in o.link for o in offers)


@pytest.mark.asyncio
async def test_top_20_per_od(monkeypatch: pytest.MonkeyPatch) -> None:
    def fake_dates(
        origin: str, dest: str, date_from: date, date_to: date, *_args: Any, **_kwargs: Any
    ) -> list[Any]:
        start = date(2026, 10, 20)
        return [
            google_mod._DateCell(origin, dest, start + timedelta(days=i), 100.0 + i, "USD")
            for i in range(25)
            if date_from <= start + timedelta(days=i) <= date_to
        ]

    def fake_flights(*_args: Any, **_kwargs: Any) -> list[Any]:
        return []

    monkeypatch.setattr(google_mod, "search_date_prices", fake_dates)
    monkeypatch.setattr(google_mod, "search_flight_results", fake_flights)

    src = GoogleFlightsSource({"max_detail_searches": 0, "jitter_s": (0, 0)})
    ctx = _ctx()
    query = _query(date_to=date(2026, 10, 20) + timedelta(days=24))
    try:
        offers = await src.search(query, ctx)
    finally:
        await ctx.http.aclose()
    assert len(offers) == 20
    assert offers[0].price_usd == 100.0
    assert offers[-1].price_usd == 119.0
    assert all("calendar fare" in o.notes for o in offers)


@pytest.mark.asyncio
async def test_empty_payloads_raise_source_error(monkeypatch: pytest.MonkeyPatch) -> None:
    def fake_dates(*_args: Any, **_kwargs: Any) -> list[Any]:
        raise google_mod.EmptyGooglePayload("wrb.fr [13] ErrorResponse")

    monkeypatch.setattr(google_mod, "search_date_prices", fake_dates)
    src = GoogleFlightsSource({"jitter_s": (0, 0)})
    ctx = _ctx()
    try:
        with pytest.raises(SourceError, match="empty payloads"):
            await src.search(_query(), ctx)
    finally:
        await ctx.http.aclose()


@pytest.mark.asyncio
async def test_silent_empty_raises_source_error(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(google_mod, "search_date_prices", lambda *_a, **_k: [])
    src = GoogleFlightsSource({"jitter_s": (0, 0)})
    ctx = _ctx()
    try:
        with pytest.raises(SourceError, match="empty payloads"):
            await src.search(_query(), ctx)
    finally:
        await ctx.http.aclose()


@pytest.mark.asyncio
async def test_search_empty_dates() -> None:
    src = GoogleFlightsSource({"jitter_s": (0, 0)})
    query = _query(date_from=date(2026, 10, 20), date_to=date(2026, 10, 22))
    ctx = _ctx(now=datetime(2026, 11, 10, tzinfo=timezone.utc))
    try:
        assert await src.search(query, ctx) == []
    finally:
        await ctx.http.aclose()


def test_plan_date_jobs_home_full_positioning_rotates() -> None:
    query = _query(
        home_origin="CNX",
        positioning_origins=("BKK", "DMK", "HKT"),
        destinations=("WAW", "KRK", "GDN", "KTW", "WRO", "POZ", "PRG"),
        extra_destinations=("WMI",),
        date_from=date(2026, 10, 20),
        date_to=date(2026, 11, 1),
    )
    dates = query.dates(date(2026, 9, 25))
    assert len(dates) == 13
    even = plan_date_jobs(query, dates, 0)
    odd = plan_date_jobs(query, dates, 1)
    assert len(even) == 104 + 147
    assert len(odd) == 104 + 126
    assert {(o, d, day) for o, d, day in even if o == "CNX"} == {
        ("CNX", dest, day) for dest in query.all_destinations for day in dates
    }
    assert not any(dest == "WMI" and origin != "CNX" for origin, dest, _day in even + odd)
    bkk_even = {day for o, dest, day in even if o == "BKK" and dest == "WAW"}
    bkk_odd = {day for o, dest, day in odd if o == "BKK" and dest == "WAW"}
    assert bkk_even.isdisjoint(bkk_odd)
    assert bkk_even | bkk_odd == set(dates)


@pytest.mark.asyncio
async def test_state_rotates_positioning_dates(monkeypatch: pytest.MonkeyPatch) -> None:
    seen: list[tuple[str, str, date]] = []

    def fake_dates(
        origin: str, dest: str, date_from: date, date_to: date, *_args: Any, **_kwargs: Any
    ) -> list[Any]:
        seen.append((origin, dest, date_from))
        return [google_mod._DateCell(origin, dest, date_from, 100.0, "USD")]

    monkeypatch.setattr(google_mod, "search_date_prices", fake_dates)
    monkeypatch.setattr(google_mod, "search_flight_results", lambda *_a, **_k: [])
    src = GoogleFlightsSource({"max_detail_searches": 0, "jitter_s": (0, 0)})
    query = _query(
        home_origin="CNX",
        positioning_origins=("BKK",),
        destinations=("WAW",),
        extra_destinations=("WMI",),
        date_from=date(2026, 10, 20),
        date_to=date(2026, 10, 23),
    )
    ctx = _ctx()
    try:
        await src.search(query, ctx)
        assert ctx.state["date_parity"] == 1
        first_pos = {day for o, _d, day in seen if o == "BKK"}
        seen.clear()
        await src.search(query, ctx)
        assert ctx.state["date_parity"] == 0
        second_pos = {day for o, _d, day in seen if o == "BKK"}
    finally:
        await ctx.http.aclose()
    days = {date(2026, 10, 20), date(2026, 10, 21), date(2026, 10, 22), date(2026, 10, 23)}
    assert first_pos.isdisjoint(second_pos)
    assert first_pos | second_pos == days
    assert not any(dest == "WMI" and origin == "BKK" for origin, dest, _day in seen)


@pytest.mark.live
@pytest.mark.asyncio
async def test_live_bkk_waw() -> None:
    src = GoogleFlightsSource({"max_detail_searches": 2, "concurrency": 1})
    query = _query(
        home_origin="BKK",
        positioning_origins=(),
        destinations=("WAW",),
        extra_destinations=(),
        date_from=date(2026, 10, 20),
        date_to=date(2026, 10, 21),
        near_miss_usd=1000.0,
    )
    ctx = _ctx(fx=FakeFx())
    try:
        offers = await src.search(query, ctx)
    except SourceError as exc:
        pytest.fail(f"google live blocked or API change: {exc}")
    finally:
        await ctx.http.aclose()
    assert offers
    assert all(o.source == "google" for o in offers)
    assert all(o.origin == "BKK" and o.destination == "WAW" for o in offers)
    assert all(o.price_usd > 0 for o in offers)
    assert all(o.currency_original == "USD" for o in offers)
    assert any(o.depart_time is not None or o.notes == "calendar fare" for o in offers)
