from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from typing import Any


@dataclass
class Offer:
    source: str
    origin: str
    destination: str
    depart_date: date
    price_usd: float
    price_original: float
    currency_original: str
    depart_time: datetime | None = None  # local naive
    arrive_time: datetime | None = None  # local naive
    airlines: list[str] = field(default_factory=list)
    flight_numbers: list[str] = field(default_factory=list)
    stops: int | None = None
    duration_minutes: int | None = None
    link: str | None = None
    cabin_bag_included: bool | None = None
    checked_bag_included: bool | None = None
    self_transfer: bool | None = None
    notes: str = ""
    hop: "Offer | None" = None

    @property
    def total_usd(self) -> float:
        hop_price = self.hop.price_usd if self.hop else 0.0
        return round(self.price_usd + hop_price, 2)

    def key(self) -> str:
        ident = ",".join(self.flight_numbers) or ",".join(self.airlines)
        depart = self.depart_time.isoformat() if self.depart_time else ""
        base = (
            f"{self.source}|{self.origin}-{self.destination}|"
            f"{self.depart_date.isoformat()}|{depart}|{ident}"
        )
        if self.hop is not None:
            return f"{base}|hop:{self.hop.key()}"
        return base

    def to_dict(self) -> dict[str, Any]:
        d: dict[str, Any] = {
            "source": self.source,
            "origin": self.origin,
            "destination": self.destination,
            "depart_date": self.depart_date.isoformat(),
            "price_usd": self.price_usd,
            "price_original": self.price_original,
            "currency_original": self.currency_original,
            "depart_time": self.depart_time.isoformat() if self.depart_time else None,
            "arrive_time": self.arrive_time.isoformat() if self.arrive_time else None,
            "airlines": list(self.airlines),
            "flight_numbers": list(self.flight_numbers),
            "stops": self.stops,
            "duration_minutes": self.duration_minutes,
            "link": self.link,
            "cabin_bag_included": self.cabin_bag_included,
            "checked_bag_included": self.checked_bag_included,
            "self_transfer": self.self_transfer,
            "notes": self.notes,
            "hop": self.hop.to_dict() if self.hop else None,
        }
        return d

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> Offer:
        hop_data = data.get("hop")
        return cls(
            source=data["source"],
            origin=data["origin"],
            destination=data["destination"],
            depart_date=_parse_date(data["depart_date"]),
            price_usd=float(data["price_usd"]),
            price_original=float(data["price_original"]),
            currency_original=data["currency_original"],
            depart_time=_parse_datetime(data.get("depart_time")),
            arrive_time=_parse_datetime(data.get("arrive_time")),
            airlines=list(data.get("airlines") or []),
            flight_numbers=list(data.get("flight_numbers") or []),
            stops=data.get("stops"),
            duration_minutes=data.get("duration_minutes"),
            link=data.get("link"),
            cabin_bag_included=data.get("cabin_bag_included"),
            checked_bag_included=data.get("checked_bag_included"),
            self_transfer=data.get("self_transfer"),
            notes=data.get("notes") or "",
            hop=cls.from_dict(hop_data) if hop_data else None,
        )


def _unique_pairs(pairs) -> list[tuple[str, str]]:
    out: list[tuple[str, str]] = []
    seen: set[tuple[str, str]] = set()
    for origin, dest in pairs:
        key = (str(origin).strip().upper(), str(dest).strip().upper())
        if not key[0] or not key[1] or key in seen:
            continue
        seen.add(key)
        out.append(key)
    return out


def _parse_date(value: date | str) -> date:
    if isinstance(value, date) and not isinstance(value, datetime):
        return value
    return date.fromisoformat(str(value))


def _parse_datetime(value: datetime | str | None) -> datetime | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        return value
    return datetime.fromisoformat(str(value))


@dataclass(frozen=True)
class SearchQuery:
    origins: tuple[str, ...]
    destinations: tuple[str, ...]
    priority_routes: tuple[tuple[str, str], ...]
    date_from: date
    date_to: date
    adults: int
    cabin_bags: int
    checked_bags: int
    max_total_usd: float
    near_miss_usd: float
    currency: str

    @property
    def all_origins(self) -> tuple[str, ...]:
        return self.origins

    @property
    def all_destinations(self) -> tuple[str, ...]:
        return self.destinations

    def route_pairs(self, *, priority: bool = False) -> list[tuple[str, str]]:
        """Origin-destination pairs.

        ``priority=True`` returns ``priority_routes`` when that list is set,
        so paid sources stay inside a daily actor budget. Otherwise every
        origin is paired with every destination.
        """
        if priority and self.priority_routes:
            return _unique_pairs(self.priority_routes)
        return _unique_pairs((origin, dest) for origin in self.origins for dest in self.destinations)

    def dates(self, today: date) -> list[date]:
        start = max(self.date_from, today)
        if start > self.date_to:
            return []
        out: list[date] = []
        cur = start
        while cur <= self.date_to:
            out.append(cur)
            cur += timedelta(days=1)
        return out
