"""Parallel filesystem scanning and allocated-space accounting."""

from __future__ import annotations

import os
import queue
import stat as stat_mod
import threading
import time
from dataclasses import dataclass, field

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
    )

    def __init__(
        self,
        name: str,
        parent: "Node | None" = None,
        size: int = 0,
        is_dir: bool = False,
        opaque: bool = False,
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
class ScanResult:
    root: Node
    root_path: str
    files: int
    dirs: int
    errors: int
    duration: float
    timestamp: float


def _is_opaque(name: str, parent_path: str) -> bool:
    if name in OPAQUE_DIR_NAMES:
        return True
    # Rust: `target` is opaque only when it's a cargo build dir.
    if name == "target" and os.path.exists(os.path.join(parent_path, "Cargo.toml")):
        return True
    return False


class Scanner:
    """Parallel disk walker. Threads are daemonic; results via on_done()."""

    def __init__(
        self,
        root_path: str,
        *,
        workers: int = 8,
        min_file_size: int = 10 * 1024 * 1024,
        on_done=None,
    ) -> None:
        self.root_path = root_path
        self.workers = max(2, workers)
        self.min_file_size = min_file_size
        self.on_done = on_done
        st = os.lstat(root_path)
        self.dev = st.st_dev
        self.root = Node(root_path, is_dir=True)
        self.root.size = st.st_blocks * 512
        self.stats = ScanStats()
        self.cancelled = False
        self._q: queue.Queue = queue.Queue()
        self._lock = threading.Lock()
        self._inode_lock = threading.Lock()
        self._seen_inodes: set[tuple[int, int]] = set()

    # -- lifecycle ---------------------------------------------------------

    def start(self) -> None:
        self._q.put(("scan", self.root, self.root_path))
        for _ in range(self.workers):
            threading.Thread(target=self._worker, daemon=True).start()
        threading.Thread(target=self._monitor, daemon=True).start()

    def cancel(self) -> None:
        self.cancelled = True

    # -- internals ---------------------------------------------------------

    def _worker(self) -> None:
        q = self._q
        while True:
            task = q.get()
            if task is None:
                q.task_done()
                return
            try:
                if not self.cancelled:
                    kind, node, path = task
                    if kind == "scan":
                        self._scan_dir(node, path)
                    else:
                        self._scan_blob(node, path)
            except Exception:
                with self._lock:
                    self.stats.errors += 1
            finally:
                q.task_done()

    def _monitor(self) -> None:
        self._q.join()
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
                )
            )

    def _dedupe_hardlink(self, st: os.stat_result) -> bool:
        """True if this inode was already counted."""
        key = (st.st_dev, st.st_ino)
        with self._inode_lock:
            if key in self._seen_inodes:
                return True
            self._seen_inodes.add(key)
            return False

    def _scan_dir(self, node: Node, path: str) -> None:
        files = 0
        nbytes = 0
        errors = 0
        small_size = 0
        small_count = 0
        children: list[Node] = []
        try:
            entries = os.scandir(path)
        except OSError:
            with self._lock:
                self.stats.errors += 1
                self.stats.dirs += 1
            return
        with entries:
            for entry in entries:
                try:
                    st = entry.stat(follow_symlinks=False)
                except OSError:
                    errors += 1
                    continue
                mode = st.st_mode
                if stat_mod.S_ISDIR(mode):
                    if st.st_dev != self.dev:
                        continue  # different filesystem / mount point
                    opaque = _is_opaque(entry.name, path)
                    child = Node(
                        entry.name,
                        parent=node,
                        size=st.st_blocks * 512,
                        is_dir=True,
                        opaque=opaque,
                    )
                    children.append(child)
                    self._q.put(("blob" if opaque else "scan", child, entry.path))
                elif stat_mod.S_ISREG(mode):
                    if st.st_nlink > 1 and self._dedupe_hardlink(st):
                        continue
                    size = st.st_blocks * 512
                    files += 1
                    nbytes += size
                    if size >= self.min_file_size:
                        children.append(Node(entry.name, parent=node, size=size))
                    else:
                        small_size += size
                        small_count += 1
                else:
                    # symlinks, fifos, sockets: count footprint, aggregate
                    size = st.st_blocks * 512
                    files += 1
                    nbytes += size
                    small_size += size
                    small_count += 1
        node.children = children
        node.small_size = small_size
        node.small_count = small_count
        with self._lock:
            s = self.stats
            s.files += files
            s.bytes += nbytes
            s.dirs += 1
            s.errors += errors

    def _scan_blob(self, node: Node, path: str) -> None:
        """Size an opaque dir (node_modules, .git, ...) without storing nodes."""
        total = 0
        files = 0
        dirs = 0
        errors = 0
        stack = [path]
        while stack:
            p = stack.pop()
            try:
                with os.scandir(p) as entries:
                    dirs += 1
                    for entry in entries:
                        try:
                            st = entry.stat(follow_symlinks=False)
                        except OSError:
                            errors += 1
                            continue
                        if stat_mod.S_ISDIR(st.st_mode):
                            if st.st_dev == self.dev:
                                total += st.st_blocks * 512
                                stack.append(entry.path)
                        else:
                            if st.st_nlink > 1 and self._dedupe_hardlink(st):
                                continue
                            total += st.st_blocks * 512
                            files += 1
            except OSError:
                errors += 1
        node.size += total
        node.file_count = files
        with self._lock:
            s = self.stats
            s.files += files
            s.bytes += total
            s.dirs += dirs
            s.errors += errors


def _aggregate(root: Node) -> None:
    """Bottom-up size/file_count aggregation (iterative post-order)."""
    stack: list[tuple[Node, bool]] = [(root, False)]
    while stack:
        node, processed = stack.pop()
        if node.children is None:
            continue
        if processed:
            node.size += node.small_size + sum(c.size for c in node.children)
            node.file_count = node.small_count + sum(
                c.file_count for c in node.children
            )
        else:
            stack.append((node, True))
            for c in node.children:
                stack.append((c, False))
