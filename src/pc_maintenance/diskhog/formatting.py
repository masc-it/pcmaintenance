"""Human-readable sizes, ages, percentages, and filesystem paths."""

from __future__ import annotations

import os
from pathlib import Path


def human_size(n: float) -> str:
    for unit in ("B", "KB", "MB", "GB", "TB", "PB"):
        if n < 1024 or unit == "PB":
            return f"{n:,.0f} B" if unit == "B" else f"{n:,.1f} {unit}"
        n /= 1024
    return "?"  # unreachable


def pct(part: int, whole: int) -> str:
    return f"{part / whole:4.0%}" if whole else "  0%"


def pretty_path(p: str) -> str:
    home = str(Path.home())
    if p == home:
        return "~"
    if p.startswith(home + os.sep):
        return "~/" + os.path.relpath(p, home)
    return p


def human_age(seconds: float) -> str:
    if seconds < 90:
        return f"{seconds:.0f}s"
    if seconds < 5400:
        return f"{seconds / 60:.0f}m"
    if seconds < 129600:
        return f"{seconds / 3600:.1f}h"
    return f"{seconds / 86400:.1f}d"
