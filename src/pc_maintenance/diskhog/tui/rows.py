"""Sorting, filtering, and location helpers for the views (no Textual)."""

from __future__ import annotations

from collections.abc import Callable, Iterable
from dataclasses import dataclass

from ..scanner import Node


@dataclass(frozen=True)
class Sort:
    column: str  # key of the column that shows the sort indicator
    ascending: bool
    key: Callable[[Node], object]


BY_SIZE = Sort("size", False, lambda n: -n.size)
BY_OLDEST = Sort("modified", True, lambda n: n.mtime)
BY_ITEMS = Sort("items", False, lambda n: -n.file_count)
BY_NAME = Sort("name", True, lambda n: n.name.casefold())
BY_PATH = Sort("path", True, lambda n: n.path().casefold())


def arrange(
    nodes: Iterable[Node],
    sort: Sort,
    query: str = "",
    text: Callable[[Node], str] = lambda n: n.name,
) -> list[Node]:
    """Filter by case-insensitive substring, then sort (ties: largest first)."""
    needle = query.casefold()
    matches = (
        [n for n in nodes if needle in text(n).casefold()] if needle else list(nodes)
    )
    matches.sort(key=lambda n: -n.size)
    matches.sort(key=sort.key)
    return matches


def names_from_root(node: Node) -> list[str]:
    names: list[str] = []
    while node.parent is not None:
        names.append(node.name)
        node = node.parent
    return names[::-1]


def resolve(root: Node, names: list[str]) -> Node:
    """The deepest existing directory along ``names`` in a (new) scan tree."""
    node = root
    for name in names:
        child = next(
            (c for c in node.children or () if c.is_dir and c.name == name), None
        )
        if child is None or child.children is None:
            break
        node = child
    return node


def browsable(node: Node) -> bool:
    return node.is_dir and node.children is not None
