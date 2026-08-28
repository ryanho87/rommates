from __future__ import annotations

from typing import Mapping


def screenscraper_rating(node: Mapping[str, object]) -> float | None:
    """Read ScreenScraper's community note, which is documented on a 0–20 scale."""
    value = node.get("note")
    if value in (None, ""):
        return None
    try:
        rating = float(str(value).strip().replace(",", "."))
    except (TypeError, ValueError):
        return None
    return rating if 0 <= rating <= 20 else None


def screenscraper_top_staff(node: Mapping[str, object]) -> bool:
    value = str(node.get("topstaff") or "").strip().casefold()
    return value in {"1", "true", "yes"}
