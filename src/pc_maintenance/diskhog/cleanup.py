"""Find stale Python envs and node_modules, and move them to the Trash.

This is the only part of diskhog that changes the filesystem. It only
touches the two kinds of folders below, only after the user confirmed the
exact list, and moves them to the Trash instead of deleting them.
"""

from __future__ import annotations

import os
import stat as stat_mod
import subprocess
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field

from .operations import move_to_trash
from .scanner import Node, is_venv_layout

STALE_AFTER = 183 * 86400  # six months, in seconds

PYTHON_ENV = "Python env"
NODE_MODULES = "node_modules"


@dataclass(frozen=True)
class Candidate:
    node: Node
    kind: str
    path: str
    size: int
    mtime: int  # newest modification anywhere inside the folder


@dataclass
class CleanupReport:
    moved: list[Candidate] = field(default_factory=list)
    skipped: list[tuple[Candidate, str]] = field(default_factory=list)
    failed: list[tuple[Candidate, str]] = field(default_factory=list)

    @property
    def moved_size(self) -> int:
        return sum(c.size for c in self.moved)


def environment_kind(node: Node) -> str | None:
    if not node.opaque:
        return None
    if node.is_venv:
        return PYTHON_ENV
    if node.name == NODE_MODULES:
        return NODE_MODULES
    return None


def stale_environments(
    root: Node, *, now: float, older_than: float = STALE_AFTER
) -> list[Candidate]:
    """Environments whose contents have not changed for \`\`older_than\`\` seconds.

    Opaque folders are never descended into, so a \`\`node_modules\`\` nested in
    another one (or in a venv) is part of its parent, not a separate entry.
    """
    found: list[Candidate] = []
    stack = [root]
    while stack:
        node = stack.pop()
        for child in node.children or ():
            if not child.is_dir:
                continue
            kind = environment_kind(child)
            if kind is not None:
                # mtime 0 means unknown (e.g. an unreadable folder): leave it.
                if child.mtime and now - child.mtime >= older_than:
                    found.append(
                        Candidate(child, kind, child.path(), child.size, child.mtime)
                    )
            elif child.children:
                stack.append(child)
    found.sort(key=lambda c: c.size, reverse=True)
    return found


def recheck(candidate: Candidate, *, scanned_at: float) -> str | None:
    """Why the folder must be skipped now, or None if it is still safe."""
    try:
        st = os.lstat(candidate.path)
    except FileNotFoundError:
        return "no longer exists"
    except OSError as exc:
        return exc.strerror or "cannot be read"
    if not stat_mod.S_ISDIR(st.st_mode):
        return "no longer a folder"
    if candidate.kind == PYTHON_ENV:
        try:
            names = set(os.listdir(candidate.path))
        except OSError as exc:
            return exc.strerror or "cannot be read"
        if not is_venv_layout(names):
            return "no longer a Python env"
    if (
        candidate.kind == NODE_MODULES
        and os.path.basename(candidate.path) != NODE_MODULES
    ):
        return "no longer node_modules"
    if st.st_mtime > scanned_at:
        return "changed since the scan"
    return None


def run_cleanup(
    candidates: Iterable[Candidate],
    *,
    scanned_at: float,
    trash: Callable[[str], None] = move_to_trash,
) -> CleanupReport:
    report = CleanupReport()
    for candidate in candidates:
        reason = recheck(candidate, scanned_at=scanned_at)
        if reason is not None:
            report.skipped.append((candidate, reason))
            continue
        try:
            trash(candidate.path)
        except (OSError, subprocess.CalledProcessError) as exc:
            report.failed.append((candidate, _failure(exc)))
            continue
        report.moved.append(candidate)
    return report


def detach(node: Node) -> int:
    """Remove \`\`node\`\` from its scan tree; return the number of files removed."""
    parent = node.parent
    assert parent is not None and parent.children is not None
    parent.children.remove(node)
    ancestor: Node | None = parent
    while ancestor is not None:
        ancestor.size -= node.size
        ancestor.file_count -= node.file_count
        ancestor = ancestor.parent
    return node.file_count


def _failure(exc: BaseException) -> str:
    if isinstance(exc, subprocess.CalledProcessError):
        output = (exc.stderr or b"").decode(errors="replace").strip()
        return output.splitlines()[-1] if output else f"exit status {exc.returncode}"
    if isinstance(exc, OSError) and exc.strerror:
        return exc.strerror
    return type(exc).__name__
