"""Parallel filesystem scanning and allocated-space accounting."""

from __future__ import annotations

import os
import queue
import threading
import time
from dataclasses import dataclass, field
from operator import itemgetter

from . import listing
from .listing import DIRECTORY, REGULAR

OPAQUE_DIR_NAMES = {
    "node_modules",
    ".git",
    ".hg",
    ".svn",
    ".venv",
    "venv",
    "__pycache__",
    ".mypy_cache",
    ".pytest_cache",
    ".ruff_cache",
    ".tox",
    ".nox",
    ".gradle",
    ".terraform",
    ".pixi",
}


MAX_ERROR_PATHS = 500  # keep the first unreadable paths for the error report

VENV_MARKER = "pyvenv.cfg"  # present in every venv / virtualenv / uv env

# Everything venv, virtualenv, and uv create at the top of an environment.
# A folder with other entries (say, ``python -m venv .`` in a project root)
# also holds the user's files, so it is never treated as an environment.
VENV_LAYOUT = frozenset(
    {
        VENV_MARKER,
        "bin",
        "Scripts",
        "lib",
        "Lib",
        "lib64",
        "include",
        "Include",
        "share",
        "etc",
        "man",
        ".gitignore",
        ".lock",
        "CACHEDIR.TAG",
    }
)

_entry_name = itemgetter(0)


def is_venv_layout(names: set[str]) -> bool:
    return VENV_MARKER in names and names <= VENV_LAYOUT


class Node:
    """One directory or (large) file. Small files are aggregated in parent."""

    __slots__ = (
        "name",
        "parent",
        "size",
        "is_dir",
        "opaque",
        "children",
        "small_size",
        "small_count",
        "file_count",
        "mtime",
        "is_venv",
    )

    def __init__(
        self,
        name: str,
        parent: "Node | None" = None,
        size: int = 0,
        is_dir: bool = False,
        opaque: bool = False,
        mtime: int = 0,
    ) -> None:
        self.name = name
        self.parent = parent
        self.size = size
        self.is_dir = is_dir
        self.opaque = opaque
        self.children: list[Node] | None = [] if (is_dir and not opaque) else None
        self.small_size = 0
        self.small_count = 0
        self.file_count = 0 if is_dir else 1
        # Newest modification time (epoch seconds) anywhere in the subtree.
        self.mtime = mtime
        self.is_venv = False  # an opaque Python environment (has pyvenv.cfg)

    def path(self) -> str:
        parts: list[str] = []
        n: Node | None = self
        while n is not None:
            parts.append(n.name)
            n = n.parent
        return os.path.join(*reversed(parts))


@dataclass
class ScanStats:
    dirs: int = 0
    files: int = 0
    bytes: int = 0
    errors: int = 0
    done: bool = False
    started: float = field(default_factory=time.monotonic)
    duration: float = 0.0


@dataclass
class ScanError:
    path: str
    reason: str


@dataclass
class ScanResult:
    root: Node
    root_path: str
    files: int
    dirs: int
    errors: int
    duration: float
    timestamp: float
    error_paths: list[ScanError] = field(default_factory=list)


def _child_path(parent: str, name: str) -> str:
    return parent + name if parent.endswith(os.sep) else parent + os.sep + name


class Scanner:
    """Parallel disk walker. Threads are daemonic; results via on_done().

    Each task lists one directory. Opaque directories (node_modules, .git, …)
    are sized without keeping nodes, but their subdirectories are still
    queued as separate tasks so one huge opaque tree does not serialize the
    end of the scan.
    """

    def __init__(
        self,
        root_path: str,
        *,
        workers: int = 8,
        min_file_size: int = 10_000_000,
        on_done=None,
    ) -> None:
        self.root_path = root_path
        self.workers = max(2, workers)
        self.min_file_size = min_file_size
        self.on_done = on_done
        self._list_dir = listing.list_dir
        st = os.lstat(root_path)
        self.dev = st.st_dev
        self.root = Node(root_path, is_dir=True, mtime=int(st.st_mtime))
        self.root.size = st.st_blocks * 512
        self.stats = ScanStats()
        self.cancelled = False
        self._q: queue.SimpleQueue = queue.SimpleQueue()
        self._pending = 0
        self._pending_lock = threading.Lock()
        self._finished = threading.Event()
        self._lock = threading.Lock()
        self._inode_lock = threading.Lock()
        self._seen_inodes: set[tuple[int, int]] = set()
        self._error_paths: list[ScanError] = []

    # -- lifecycle ---------------------------------------------------------

    def start(self) -> None:
        self._submit([(self.root, self.root_path, None)])
        for _ in range(self.workers):
            threading.Thread(target=self._worker, daemon=True).start()
        threading.Thread(target=self._monitor, daemon=True).start()

    def cancel(self) -> None:
        self.cancelled = True

    # -- task bookkeeping --------------------------------------------------

    def _submit(self, tasks: list) -> None:
        if not tasks:
            return
        with self._pending_lock:
            self._pending += len(tasks)
        for task in tasks:
            self._q.put(task)

    def _task_done(self) -> None:
        with self._pending_lock:
            self._pending -= 1
            finished = self._pending == 0
        if finished:
            self._finished.set()

    def _worker(self) -> None:
        q = self._q
        while True:
            task = q.get()
            if task is None:
                return
            node, path, owner = task
            try:
                if not self.cancelled:
                    if owner is None:
                        self._scan_dir(node, path)
                    else:
                        self._scan_opaque(owner, node, path)
            except Exception as exc:
                self._record_errors(1, path, exc)
            finally:
                self._task_done()

    def _monitor(self) -> None:
        if self._pending:
            self._finished.wait()
        for _ in range(self.workers):
            self._q.put(None)
        _aggregate(self.root)
        s = self.stats
        s.duration = time.monotonic() - s.started
        s.done = True
        if self.on_done is not None and not self.cancelled:
            self.on_done(
                ScanResult(
                    root=self.root,
                    root_path=self.root_path,
                    files=s.files,
                    dirs=s.dirs,
                    errors=s.errors,
                    duration=s.duration,
                    timestamp=time.time(),
                    error_paths=list(self._error_paths),
                )
            )

    # -- accounting --------------------------------------------------------

    def _record_errors(self, count: int, path: str, exc: BaseException | str) -> None:
        reason = exc if isinstance(exc, str) else _reason(exc)
        with self._lock:
            self.stats.errors += count
            if len(self._error_paths) < MAX_ERROR_PATHS:
                self._error_paths.append(ScanError(path, reason))

    def _already_counted(self, dev: int, inode: int) -> bool:
        key = (dev, inode)
        with self._inode_lock:
            if key in self._seen_inodes:
                return True
            self._seen_inodes.add(key)
            return False

    def _list(self, path: str) -> list | None:
        try:
            entries, failures = self._list_dir(path)
        except OSError as exc:
            self._record_errors(1, path, exc)
            return None
        if failures:
            self._record_errors(failures, path, f"{failures} unreadable entries")
        return entries

    # -- directory tasks ---------------------------------------------------

    def _scan_dir(self, node: Node, path: str) -> None:
        entries = self._list(path)
        if entries is None:
            with self._lock:
                self.stats.dirs += 1
            return
        if (
            node.parent is not None
            and VENV_MARKER in map(_entry_name, entries)
            and is_venv_layout(set(map(_entry_name, entries)))
        ):
            # A Python env under a custom name: size it like ``.venv``.
            node.opaque = True
            node.is_venv = True
            node.children = None
            self._add_opaque_level(node, path, entries)
            return
        files = nbytes = small_size = small_count = 0
        newest = node.mtime
        children: list[Node] = []
        tasks: list = []
        names: set[str] | None = None
        for name, kind, dev, inode, links, allocated, mtime in entries:
            if mtime > newest:
                newest = mtime
            if kind == DIRECTORY:
                if dev != self.dev:
                    continue  # different filesystem / mount point
                opaque = name in OPAQUE_DIR_NAMES
                if name == "target":
                    # Rust: `target` is opaque only when it's a cargo build dir.
                    if names is None:
                        names = {entry[0] for entry in entries}
                    opaque = "Cargo.toml" in names
                child = Node(name, node, allocated, True, opaque, mtime)
                children.append(child)
                tasks.append(
                    (child, _child_path(path, name), child if opaque else None)
                )
                continue
            if links > 1 and kind == REGULAR and self._already_counted(dev, inode):
                continue
            files += 1
            nbytes += allocated
            if kind == REGULAR and allocated >= self.min_file_size:
                children.append(Node(name, node, allocated, mtime=mtime))
            else:
                # small files, symlinks, fifos, sockets: aggregated footprint
                small_size += allocated
                small_count += 1
        node.children = children
        node.small_size = small_size
        node.small_count = small_count
        node.mtime = newest
        self._submit(tasks)
        with self._lock:
            s = self.stats
            s.files += files
            s.bytes += nbytes
            s.dirs += 1

    def _scan_opaque(self, owner: Node, node: Node | None, path: str) -> None:
        """List one directory of an opaque tree; ``node`` is set at its top."""
        entries = self._list(path)
        if entries is None:
            return
        if node is owner and is_venv_layout(set(map(_entry_name, entries))):
            owner.is_venv = True
        self._add_opaque_level(owner, path, entries)

    def _add_opaque_level(self, owner: Node, path: str, entries: list) -> None:
        """Add one listed level of an opaque tree to its owner's totals."""
        total = files = 0
        newest = 0
        tasks: list = []
        for name, kind, dev, inode, links, allocated, mtime in entries:
            if mtime > newest:
                newest = mtime
            if kind == DIRECTORY:
                if dev == self.dev:
                    total += allocated
                    tasks.append((None, _child_path(path, name), owner))
                continue
            if links > 1 and self._already_counted(dev, inode):
                continue
            total += allocated
            files += 1
        self._submit(tasks)
        with self._lock:
            owner.size += total
            owner.file_count += files
            if newest > owner.mtime:
                owner.mtime = newest
            s = self.stats
            s.files += files
            s.bytes += total
            s.dirs += 1


def _reason(exc: BaseException) -> str:
    if isinstance(exc, OSError) and exc.strerror:
        return exc.strerror
    return type(exc).__name__


def _aggregate(root: Node) -> None:
    """Bottom-up size, file count, and mtime aggregation (post-order)."""
    stack: list[tuple[Node, bool]] = [(root, False)]
    while stack:
        node, processed = stack.pop()
        if node.children is None:
            continue
        if processed:
            children = node.children
            node.size += node.small_size + sum(c.size for c in children)
            node.file_count = node.small_count + sum(c.file_count for c in children)
            newest = max((c.mtime for c in children), default=0)
            if newest > node.mtime:
                node.mtime = newest
        else:
            stack.append((node, True))
            for c in node.children:
                stack.append((c, False))
