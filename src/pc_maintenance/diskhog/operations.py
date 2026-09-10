"""Open directories or reveal files in macOS Finder."""

from __future__ import annotations

import os
import subprocess


def open_in_finder(path: str) -> None:
    cmd = ["open", path] if os.path.isdir(path) else ["open", "-R", path]
    subprocess.run(cmd, check=True, capture_output=True)
