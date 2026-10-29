"""Hotspot classification and largest-file selection from a scan tree."""

from __future__ import annotations

import heapq
import itertools
import os
from collections import defaultdict
from dataclasses import dataclass

from .scanner import Node

MIN_HOTSPOT_SIZE = 1 * 1024 * 1024  # ignore hotspot instances < 1 MiB


NAME_CATEGORIES = {
    "node_modules": "Node modules",
    ".venv": "Python venvs",
    "venv": "Python venvs",
    ".git": "Git repos (.git)",
    "__pycache__": "Build & tool caches",
    ".mypy_cache": "Build & tool caches",
    ".pytest_cache": "Build & tool caches",
    ".ruff_cache": "Build & tool caches",
    ".tox": "Build & tool caches",
    ".nox": "Build & tool caches",
    ".gradle": "Build & tool caches",
    ".terraform": "Build & tool caches",
    "data": "Data dirs",
    "data.tmp": "Data dirs",
    "dataset": "Data dirs",
    "datasets": "Data dirs",
}


SPECIAL_HOTSPOTS = [
    (".cache", "System & app caches", True, {"huggingface"}),
    ("Library/Caches", "System & app caches", True, None),
    (".npm", "System & app caches", False, None),
    (".yarn", "System & app caches", False, None),
    ("Library/pnpm", "System & app caches", False, None),
    (".cargo/registry", "System & app caches", False, None),
    ("go/pkg/mod", "System & app caches", False, None),
    (".Trash", "Trash", False, None),
    (".ollama", "ML models & datasets", False, None),
    (".lmstudio", "ML models & datasets", False, None),
    (".nvm", "Toolchains & SDKs", False, None),
    (".pyenv", "Toolchains & SDKs", False, None),
    (".rustup", "Toolchains & SDKs", False, None),
    (".rbenv", "Toolchains & SDKs", False, None),
    ("Library/Android", "Toolchains & SDKs", False, None),
    ("Library/Developer/Xcode/DerivedData", "Build & tool caches", False, None),
    ("Library/Developer/CoreSimulator", "Toolchains & SDKs", False, None),
    (".docker", "Docker & VMs", False, None),
    ("Library/Containers/com.docker.docker", "Docker & VMs", False, None),
    (".colima", "Docker & VMs", False, None),
    (".lima", "Docker & VMs", False, None),
]


ML_FILE_EXTS = {
    ".gguf",
    ".ggml",
    ".safetensors",
    ".pt",
    ".pth",
    ".onnx",
    ".ckpt",
    ".h5",
    ".tflite",
    ".mlmodel",
    ".npz",
}


ML_FILE_MIN_SIZE = 50 * 1024 * 1024


ML_BIN_MIN_SIZE = 100 * 1024 * 1024  # ".bin" only counts as a model if big


@dataclass
class Hotspots:
    categories: dict[str, list[Node]]  # category -> nodes (unsorted)
    top_files: list[tuple[int, Node]]  # (size, node) sorted desc


@dataclass
class Category:
    name: str
    size: int
    nodes: list[Node]  # sorted by size, largest first


def ranked_categories(hotspots: Hotspots) -> list[Category]:
    ranked = [
        Category(name, sum(n.size for n in nodes), sorted(nodes, key=_by_size))
        for name, nodes in hotspots.categories.items()
    ]
    ranked.sort(key=lambda c: c.size, reverse=True)
    return ranked


def hotspot_total(hotspots: Hotspots) -> int:
    """Space used by all hotspots, counting nested instances only once.

    Categories overlap (a ``data/`` dir may contain a ``.git``), so the sum
    of category sizes can exceed the real total.
    """
    members = {id(n) for nodes in hotspots.categories.values() for n in nodes}
    total = 0
    for nodes in hotspots.categories.values():
        for node in nodes:
            parent = node.parent
            while parent is not None and id(parent) not in members:
                parent = parent.parent
            if parent is None:
                total += node.size
    return total


def _by_size(node: Node) -> int:
    return -node.size


def _node_at(root: Node, rel: str) -> Node | None:
    n: Node | None = root
    for part in rel.split("/"):
        if n is None or n.children is None:
            return None
        n = next((c for c in n.children if c.is_dir and c.name == part), None)
    return n


def analyze(root: Node, top_n: int = 300) -> Hotspots:
    cats: dict[str, list[Node]] = defaultdict(list)
    seen: set[int] = set()

    def register(cat: str, node: Node | None) -> None:
        if node is None or node.size < MIN_HOTSPOT_SIZE or id(node) in seen:
            return
        seen.add(id(node))
        cats[cat].append(node)

    # Specific well-known paths first (more precise than the name walk).
    for rel, cat, expand, exclude in SPECIAL_HOTSPOTS:
        n = _node_at(root, rel)
        if n is None:
            continue
        if expand:
            for c in n.children or []:
                if c.is_dir and (not exclude or c.name not in exclude):
                    register(cat, c)
        else:
            register(cat, n)

    # HuggingFace cache: one instance per model / dataset.
    hf = _node_at(root, ".cache/huggingface")
    if hf is not None:
        for c in hf.children or []:
            if c.name == "hub" and c.children:
                for m in c.children:
                    register("ML models & datasets", m)
            elif c.is_dir:
                register("ML models & datasets", c)

    # Full walk: name-based dir categories + big-file collection.
    heap: list[tuple[int, int, Node]] = []
    counter = itertools.count()
    stack = [root]
    while stack:
        node = stack.pop()
        for c in node.children or []:
            if c.is_dir:
                cat = NAME_CATEGORIES.get(c.name)
                if cat is None and c.opaque and c.name == "target":
                    cat = "Build & tool caches"
                if cat:
                    register(cat, c)
                if c.children:
                    stack.append(c)
            else:
                heapq.heappush(heap, (c.size, next(counter), c))
                if len(heap) > top_n:
                    heapq.heappop(heap)
                ext = os.path.splitext(c.name)[1].lower()
                if (ext in ML_FILE_EXTS and c.size >= ML_FILE_MIN_SIZE) or (
                    ext == ".bin" and c.size >= ML_BIN_MIN_SIZE
                ):
                    register("ML models & datasets", c)

    top_files = sorted(
        ((size, node) for size, _, node in heap), key=lambda t: t[0], reverse=True
    )
    return Hotspots(categories=dict(cats), top_files=top_files)
