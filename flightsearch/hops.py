from __future__ import annotations

from dataclasses import replace
from datetime import timedelta

from flightsearch.models import Offer, SearchQuery

UNVERIFIED_NOTE = "hop timing unverified"


def attach_hops(
    offers: list[Offer],
    hops: list[Offer],
    query: SearchQuery,
    min_connection_minutes: int,
    max_connection_hours: int,
) -> tuple[list[Offer], list[tuple[Offer, str]]]:
    kept: list[Offer] = []
    dropped: list[tuple[Offer, str]] = []
    min_delta = timedelta(minutes=min_connection_minutes)
    max_delta = timedelta(hours=max_connection_hours)
    positioning = set(query.positioning_origins)
    destinations = set(query.all_destinations)

    for offer in offers:
        dest = offer.destination
        if dest not in destinations:
            dropped.append((offer, "destination not in search"))
            continue
        if offer.depart_date < query.date_from or offer.depart_date > query.date_to:
            dropped.append((offer, "date outside window"))
            continue
        if offer.origin == query.home_origin:
            kept.append(offer)
            continue
        if offer.origin in positioning:
            hop = _choose_hop(offer, hops, query, min_delta, max_delta)
            if hop is None:
                dropped.append((offer, "no positioning hop"))
                continue
            kept.append(hop)
            continue
        dropped.append((offer, "unsupported origin"))

    return kept, dropped


def _choose_hop(
    offer: Offer,
    hops: list[Offer],
    query: SearchQuery,
    min_delta: timedelta,
    max_delta: timedelta,
) -> Offer | None:
    candidates: list[tuple[float, Offer, bool]] = []
    for hop in hops:
        if hop.origin != query.home_origin or hop.destination != offer.origin:
            continue
        ok, unverified = _hop_connects(offer, hop, min_delta, max_delta)
        if ok:
            candidates.append((hop.price_usd, hop, unverified))
    if not candidates:
        return None
    _price, hop, unverified = min(candidates, key=lambda item: item[0])
    notes = offer.notes
    if unverified and UNVERIFIED_NOTE not in notes:
        notes = f"{notes} · {UNVERIFIED_NOTE}".strip(" ·") if notes else UNVERIFIED_NOTE
    return replace(offer, hop=hop, notes=notes)


def _hop_connects(
    offer: Offer,
    hop: Offer,
    min_delta: timedelta,
    max_delta: timedelta,
) -> tuple[bool, bool]:
    if offer.depart_time is not None and hop.arrive_time is not None:
        delta = offer.depart_time - hop.arrive_time
        return min_delta <= delta <= max_delta, False
    expected = offer.depart_date - timedelta(days=1)
    return hop.depart_date == expected, True
