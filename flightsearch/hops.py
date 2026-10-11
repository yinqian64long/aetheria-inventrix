from __future__ import annotations

from dataclasses import replace

from flightsearch.models import Offer, SearchQuery

CHECKED_BAG_NOTE = "checked bag not verified"


def attach_hops(
    offers: list[Offer],
    query: SearchQuery,
) -> tuple[list[Offer], list[tuple[Offer, str]]]:
    """Keep offers whose airports and date are inside the search.

    The name stays from the old positioning-hop step. Every configured origin
    is a direct departure, so nothing is priced as a connecting hop.
    """
    kept: list[Offer] = []
    dropped: list[tuple[Offer, str]] = []
    origins = {code.upper() for code in query.origins}
    destinations = {code.upper() for code in query.destinations}

    for offer in offers:
        dest = offer.destination.upper()
        origin = offer.origin.upper()
        if dest not in destinations:
            dropped.append((offer, "destination not in search"))
            continue
        if offer.depart_date < query.date_from or offer.depart_date > query.date_to:
            dropped.append((offer, "date outside window"))
            continue
        if origin not in origins:
            dropped.append((offer, "unsupported origin"))
            continue
        kept.append(_note_unchecked_bag(offer))

    return kept, dropped


def _note_unchecked_bag(offer: Offer) -> Offer:
    if offer.checked_bag_included is not None:
        return offer
    if CHECKED_BAG_NOTE in offer.notes:
        return offer
    notes = (
        f"{offer.notes} · {CHECKED_BAG_NOTE}".strip(" ·")
        if offer.notes
        else CHECKED_BAG_NOTE
    )
    return replace(offer, notes=notes)
