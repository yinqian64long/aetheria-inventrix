from __future__ import annotations

import html
from collections.abc import Mapping, Sequence
from datetime import datetime
from typing import Any

from flightsearch.models import Offer

ALSO_SEEN_LIMIT = 3


def format_deals(
    offers: list[Offer],
    also_seen: Sequence[Sequence[Mapping[str, Any]]] | None = None,
) -> str:
    chunks: list[str] = []
    for i, offer in enumerate(offers):
        extras = list(also_seen[i]) if also_seen is not None and i < len(also_seen) else []
        chunks.append(_format_deal(offer, extras))
    return "\n\n".join(chunks)


def format_heartbeat(result: Any) -> str:
    lines = ["<b>flightsearch heartbeat</b>"]
    if getattr(result, "window_over", False):
        lines.append("Search window is over.")
    qualifying = list(getattr(result, "qualifying", []) or [])
    near = list(getattr(result, "near_misses", []) or [])
    dropped = getattr(result, "dropped_count", 0)
    lines.append(
        f"Qualifying: {len(qualifying)} · Near misses: {len(near)} · Dropped: {dropped}"
    )
    best = getattr(result, "best_per_source", {}) or {}
    lines.append("<b>Best per source</b>")
    if best:
        for name, offer in sorted(best.items()):
            lines.append(
                f"• {html.escape(str(name))}: <b>{_money(offer.total_usd)}</b> "
                f"{html.escape(offer.origin)}→{html.escape(offer.destination)} "
                f"{offer.depart_date.isoformat()}"
            )
    else:
        lines.append("• none")
    status = getattr(result, "source_status", {}) or {}
    lines.append("<b>Status</b>")
    if status:
        for name, st in sorted(status.items()):
            count = st.get("count", 0)
            seconds = float(st.get("seconds") or 0)
            err = st.get("error")
            extra = f" ({count} offers, {seconds:.1f}s)"
            err_s = f" — {html.escape(str(err))}" if err else ""
            lines.append(
                f"• {html.escape(str(name))}: {html.escape(str(st.get('status', '')))}{extra}{err_s}"
            )
    else:
        lines.append("• none")
    return "\n".join(lines)


def format_health_alert(source: str, error: str) -> str:
    err = html.escape(error or "unknown error")
    return f"⚠️ Source <b>{html.escape(source)}</b> failed 3 consecutive times.\n{err}"


def format_markdown(result: Any) -> str:
    lines = ["# Flight search", ""]
    if getattr(result, "window_over", False):
        lines += ["Search window is over.", ""]
    lines += [
        f"- Qualifying: {len(getattr(result, 'qualifying', []) or [])}",
        f"- Near misses: {len(getattr(result, 'near_misses', []) or [])}",
        f"- Dropped: {getattr(result, 'dropped_count', 0)}",
        f"- Messages sent: {getattr(result, 'messages_sent', 0)}",
        "",
        "## Qualifying deals",
        "",
        *_offer_table(getattr(result, "qualifying", []) or []),
        "",
        "## Near misses",
        "",
        *_offer_table(getattr(result, "near_misses", []) or []),
        "",
        "## Best per source",
        "",
    ]
    best = getattr(result, "best_per_source", {}) or {}
    if best:
        lines += [
            "| Source | Total | Route | Date |",
            "| --- | --- | --- | --- |",
        ]
        for name, offer in sorted(best.items()):
            lines.append(
                f"| {_md(name)} | {_md(_money(offer.total_usd))} | "
                f"{_md(offer.origin)}→{_md(offer.destination)} | "
                f"{offer.depart_date.isoformat()} |"
            )
    else:
        lines.append("_none_")
    lines += ["", "## Source status", ""]
    status = getattr(result, "source_status", {}) or {}
    if status:
        lines += [
            "| Source | Status | Count | Seconds | Error |",
            "| --- | --- | --- | --- | --- |",
        ]
        for name, st in sorted(status.items()):
            lines.append(
                f"| {_md(name)} | {_md(st.get('status', ''))} | "
                f"{st.get('count', 0)} | {float(st.get('seconds') or 0):.1f} | "
                f"{_md(st.get('error') or '')} |"
            )
    else:
        lines.append("_none_")
    lines.append("")
    return "\n".join(lines)


def _format_deal(offer: Offer, also_seen: Sequence[Mapping[str, Any]] | None = None) -> str:
    route = f"{html.escape(offer.origin)}→{html.escape(offer.destination)}"
    when = _format_when(offer)
    line1 = f"✈️ <b>{_money(offer.total_usd)}</b> {route} · {when}"

    bits: list[str] = []
    airlines = ", ".join(html.escape(a) for a in offer.airlines) or "unknown"
    bits.append(f"Airlines: {airlines}")
    stops = _format_stops(offer.stops)
    if stops:
        bits.append(stops)
    if offer.self_transfer:
        bits.append("self-transfer")
    line2 = " · ".join(bits)

    lines = [line1, line2]
    if offer.hop is not None:
        hop = offer.hop
        hop_arr = ""
        if hop.arrive_time is not None:
            hop_arr = f" (arr {hop.arrive_time.strftime('%H:%M')})"
        lines.append(
            f"+ hop {html.escape(hop.origin)}→{html.escape(hop.destination)} "
            f"{_money(hop.price_usd)}{hop_arr} — included in total"
        )

    bag = _bag_label(offer.cabin_bag_included)
    tail = [f"Cabin bag: {bag}", f"Source: {html.escape(offer.source)}"]
    if offer.link:
        href = html.escape(offer.link, quote=True)
        tail.append(f'<a href="{href}">open</a>')
    lines.append(" · ".join(tail))
    note = _short_note(offer.notes)
    if note:
        lines.append(html.escape(note))
    for extra in list(also_seen or [])[:ALSO_SEEN_LIMIT]:
        lines.append(_format_also_on(extra))
    return "\n".join(lines)


def _format_also_on(extra: Mapping[str, Any]) -> str:
    source = html.escape(str(extra.get("source") or "unknown"))
    total = _money(float(extra.get("total_usd") or 0))
    bag = _bag_label(extra.get("cabin_bag_included"))  # type: ignore[arg-type]
    line = f"also on {source} {total} (cabin bag {bag})"
    link = extra.get("link")
    if link:
        href = html.escape(str(link), quote=True)
        line += f' · <a href="{href}">open</a>'
    return line


def _format_when(offer: Offer) -> str:
    if offer.depart_time is not None:
        start = _fmt_local(offer.depart_time)
        if offer.arrive_time is not None:
            return f"{start}→{_fmt_local(offer.arrive_time)}"
        return start
    return offer.depart_date.strftime("%a %d %b")


def _fmt_local(dt: datetime) -> str:
    return dt.strftime("%a %d %b %H:%M")


def _format_stops(stops: int | None) -> str | None:
    if stops is None:
        return None
    if stops == 0:
        return "nonstop"
    if stops == 1:
        return "1 stop"
    return f"{stops} stops"


def _bag_label(value: bool | None) -> str:
    if value is True:
        return "yes"
    if value is False:
        return "not included"
    return "unknown"


NOTE_LIMIT = 140


def _short_note(notes: str, limit: int = NOTE_LIMIT) -> str:
    text = " ".join((notes or "").split())
    if not text:
        return ""
    if len(text) > limit:
        return text[: limit - 1].rstrip() + "…"
    return text


def _money(amount: float) -> str:
    rounded = round(float(amount), 2)
    if abs(rounded - round(rounded)) < 1e-9:
        return f"${int(round(rounded))}"
    return f"${rounded:.2f}"


def _offer_table(offers: list[Offer]) -> list[str]:
    if not offers:
        return ["_none_"]
    rows = [
        "| Total | Route | Date | Source | Link |",
        "| --- | --- | --- | --- | --- |",
    ]
    for offer in offers:
        link = offer.link or ""
        rows.append(
            f"| {_md(_money(offer.total_usd))} | "
            f"{_md(offer.origin)}→{_md(offer.destination)} | "
            f"{offer.depart_date.isoformat()} | {_md(offer.source)} | {_md(link)} |"
        )
    return rows


def _md(value: Any) -> str:
    return str(value).replace("|", "\\|")
