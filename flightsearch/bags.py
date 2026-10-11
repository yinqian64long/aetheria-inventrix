from __future__ import annotations

import re
from typing import Any

CHECKED_KG = 15

_KG_RE = re.compile(r"(\d+(?:\.\d+)?)\s*kg\b", re.IGNORECASE)
_CHECKED_KEYS = {
    "checked",
    "checkedbag",
    "checkedbags",
    "checkedbagincluded",
    "checkedbagsincluded",
    "hold",
    "holdbag",
    "holdbags",
    "checkedbaggage",
}
_CABIN_KEYS = {
    "cabin",
    "cabinbag",
    "carryon",
    "carryonbagsincluded",
    "carryonincluded",
    "hand",
    "handbag",
    "includedhandbags",
}
_CHECKED_TYPES = {"checked", "checkin", "check-in", "hold", "checkedbag", "checked_bag"}
_CABIN_TYPES = {"cabin", "carryon", "carry-on", "hand", "cabinbag", "handbag"}


def checked_bag_included(value: Any) -> bool | None:
    """True when a checked allowance is at least 15 kg.

    False when the payload says there is no checked bag. None when the
    allowance is missing or has no weight.
    """
    return _finish(_scan(value, kind="checked"))


def cabin_bag_included(value: Any) -> bool | None:
    return _finish(_scan(value, kind="cabin"))


def _finish(found: set[str]) -> bool | None:
    if "yes" in found:
        return True
    if "unknown" in found:
        return None
    if "no" in found:
        return False
    return None


def _norm_key(key: Any) -> str:
    return re.sub(r"[^a-z]", "", str(key).lower())


def _scan(value: Any, *, kind: str) -> set[str]:
    found: set[str] = set()
    if isinstance(value, list):
        for item in value:
            found |= _scan(item, kind=kind)
        return found
    if isinstance(value, dict):
        bag_type = str(
            value.get("type") or value.get("baggageType") or ""
        ).strip().lower().replace(" ", "").replace("_", "")
        types = _CHECKED_TYPES if kind == "checked" else _CABIN_TYPES
        if bag_type in types:
            found.add(_allowance_entry(value, kind=kind))
        keys = _CHECKED_KEYS if kind == "checked" else _CABIN_KEYS
        for key, child in value.items():
            if _norm_key(key) in keys:
                flag = _interpret_amount(child, kind=kind)
                if flag:
                    found.add(flag)
                elif isinstance(child, dict):
                    found.add(_allowance_entry(child, kind=kind))
                    found |= _scan(child, kind=kind)
                elif isinstance(child, list):
                    found |= _scan(child, kind=kind)
            elif isinstance(child, (dict, list)):
                found |= _scan(child, kind=kind)
        return found
    flag = _interpret_amount(value, kind=kind)
    if flag:
        found.add(flag)
    return found


def _allowance_entry(entry: dict[str, Any], *, kind: str) -> str:
    weight = entry.get("weightKg")
    if weight is None:
        weight = entry.get("weight") or entry.get("kg")
    if isinstance(weight, dict):
        weight = weight.get("value") or weight.get("amount") or weight.get("kg")
    qty = entry.get("quantity")
    if qty is None:
        qty = entry.get("pieces") if entry.get("pieces") is not None else entry.get("count")
    weight_flag = _interpret_amount(weight, kind=kind) if weight is not None else None
    qty_flag = _interpret_amount(qty, kind=kind) if qty is not None else None
    if weight_flag == "yes":
        return "yes"
    if weight_flag == "no" or qty_flag == "no":
        return "no"
    if qty_flag in {"yes", "unknown"} or weight_flag == "unknown":
        return "yes" if kind == "cabin" else "unknown"
    return "unknown"


def _interpret_amount(value: Any, *, kind: str) -> str | None:
    if value is None or isinstance(value, (dict, list)):
        return None
    if isinstance(value, bool):
        if kind == "cabin":
            return "yes" if value else "no"
        return "no" if value is False else "unknown"
    if isinstance(value, (int, float)):
        number = float(value)
        if number <= 0:
            return "no"
        if kind == "cabin":
            return "yes"
        if number >= CHECKED_KG:
            return "yes"
        if number == int(number) and number <= 3:
            return "unknown"
        return "no"
    text = str(value).strip()
    if not text:
        return None
    match = _KG_RE.search(text)
    if match:
        weight = float(match.group(1))
        if kind == "checked":
            return "yes" if weight >= CHECKED_KG else "no"
        return "yes" if weight > 0 else "no"
    lowered = text.lower()
    if lowered in {"0", "false", "no", "excluded", "none", "not included"}:
        return "no"
    if lowered in {"1", "true", "yes", "included"}:
        return "yes" if kind == "cabin" else "unknown"
    return None
