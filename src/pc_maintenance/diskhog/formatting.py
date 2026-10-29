"""Human-readable sizes, ages, percentages, bars, and filesystem paths."""

from __future__ import annotations

import os
from pathlib import Path

# Decimal units, as in Finder, so sizes agree with what macOS shows.
_UNITS = ("B", "KB", "MB", "GB", "TB", "PB")


def human_size(n: float) -> str:
    for unit in _UNITS:
        if n < 1000 or unit == _UNITS[-1]:
            return f"{n:,.0f} B" if unit == "B" else f"{n:,.1f} {unit}"
        n /= 1000
    raise AssertionError("unreachable")


def pct(part: int, whole: int) -> str:
    return f"{part / whole:4.0%}" if whole else "  0%"


def pretty_path(p: str) -> str:
    home = str(Path.home())
    if p == home:
        return "~"
    if p.startswith(home + os.sep):
        return "~/" + os.path.relpath(p, home)
    return p


_AGE_STEPS = (
    (60, 1, "s"),
    (3600, 60, "m"),
    (86400, 3600, "h"),
    (30 * 86400, 86400, "d"),
    (365 * 86400, 30 * 86400, "mo"),
)


def human_age(seconds: float) -> str:
    """Compact age such as ``45s``, ``12m``, ``3h``, ``5d``, ``4mo``, ``2y``."""
    seconds = max(0.0, seconds)
    for limit, unit, suffix in _AGE_STEPS:
        if seconds < limit:
            return f"{int(seconds // unit)}{suffix}"
    return f"{int(seconds // (365 * 86400))}y"


_EIGHTHS = " ▏▎▍▌▋▊▉"


def share_bar(fraction: float, width: int) -> str:
    """A left-aligned bar of ``width`` cells with eighth-cell resolution."""
    fraction = min(1.0, max(0.0, fraction))
    eighths = round(fraction * width * 8)
    full, rest = divmod(eighths, 8)
    bar = "█" * full + (_EIGHTHS[rest] if rest else "")
    return bar.ljust(width)


def truncate_left(text: str, width: int) -> str:
    """Keep the end of ``text``, where paths carry the most information."""
    if width <= 0:
        return ""
    if len(text) <= width:
        return text
    return "…" + text[len(text) - width + 1 :]
