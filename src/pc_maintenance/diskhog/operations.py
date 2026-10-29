"""macOS integrations: Finder, the clipboard, and the Trash."""

from __future__ import annotations

import os
import subprocess

TRASH_TOOL = "/usr/bin/trash"  # macOS 15+

# Older macOS: ask Finder, which also records the original location.
_FINDER_TRASH = [
    "osascript",
    "-e",
    "on run argv",
    "-e",
    'tell application "Finder" to delete (POSIX file (item 1 of argv) as alias)',
    "-e",
    "end run",
]


def open_in_finder(path: str) -> None:
    cmd = ["open", path] if os.path.isdir(path) else ["open", "-R", path]
    subprocess.run(cmd, check=True, capture_output=True)


def copy_to_clipboard(text: str) -> None:
    subprocess.run(["pbcopy"], input=text.encode(), check=True, capture_output=True)


def move_to_trash(path: str) -> None:
    """Move ``path`` to the user's Trash, where it can be restored."""
    if os.path.exists(TRASH_TOOL):
        cmd = [TRASH_TOOL, "-s", path]
    else:
        cmd = [*_FINDER_TRASH, path]
    subprocess.run(cmd, check=True, capture_output=True)
