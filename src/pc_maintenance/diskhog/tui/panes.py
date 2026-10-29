"""The Browse, Hotspots, and Files views."""

from __future__ import annotations

import time
from dataclasses import dataclass

from rich.text import Text
from textual.app import ComposeResult
from textual.containers import Horizontal, Vertical
from textual.content import Content
from textual.widgets import DataTable

from ..analysis import Category, Hotspots
from ..formatting import (
    human_age,
    human_size,
    pct,
    pretty_path,
    share_bar,
    truncate_left,
)
from ..scanner import Node, ScanResult
from .rows import (
    BY_ITEMS,
    BY_NAME,
    BY_OLDEST,
    BY_PATH,
    BY_SIZE,
    Sort,
    arrange,
    browsable,
    names_from_root,
    resolve,
)
from .widgets import Column, Line, Table

MAX_ROWS = 2000  # beyond this, ask the user to filter instead of scrolling
SHARE_BAR_WIDTH = 10


@dataclass
class ScanView:
    """A scan plus everything derived from it, computed off the UI thread."""

    result: ScanResult
    hotspots: Hotspots
    categories: list[Category]
    hotspot_total: int
    min_file_size: int
    unreadable: frozenset[str]  # directories that could not be (fully) listed


@dataclass
class Selection:
    """What the bottom line shows for the highlighted row."""

    path: str
    note: str = ""


# -- cells -----------------------------------------------------------------


def size_cell(size: int) -> Text:
    return Text(human_size(size), justify="right")


def share_cell(size: int, whole: int, muted: str) -> Text:
    fraction = size / whole if whole else 0.0
    text = Text(share_bar(fraction, SHARE_BAR_WIDTH))
    text.append(pct(size, whole), style=muted)
    return text


def age_cell(mtime: int, now: float, muted: str) -> Text:
    age = human_age(now - mtime) if mtime else "—"
    return Text(age, style=muted, justify="right")


def count_cell(count: int, muted: str) -> Text:
    return Text(f"{count:,}", style=muted, justify="right")


def path_cell(path: str, width: int, muted: str) -> Text:
    """Folder muted, final component normal, truncated from the left."""
    shown = truncate_left(pretty_path(path), width)
    head, sep, tail = shown.rpartition("/")
    text = Text(head + sep, style=muted)
    text.append(tail)
    return text


def filter_status(query: str, shown: int, total: int) -> Content:
    return Content.assemble(
        (f"“{query}”", "bold"), f"  {shown:,} of {total:,}  ·  esc clears"
    )


def overflow_row(hidden: int, muted: str) -> dict[str, Text]:
    note = Text(f"{hidden:,} more — filter with / to narrow", style=f"{muted} italic")
    return {"name": note, "path": note}


# -- panes -----------------------------------------------------------------


class Pane(Vertical):
    """A header line over a table, with sorting and filtering."""

    SORTS: tuple[Sort, ...] = (BY_SIZE,)

    DEFAULT_CSS = """
    Pane { height: 1fr; }
    Pane > Line { color: $foreground; }
    """

    def __init__(self, *, id: str) -> None:
        super().__init__(id=id)
        self.view: ScanView | None = None
        self.sort_index = 0
        self.filter_text = ""

    @property
    def sort(self) -> Sort:
        return self.SORTS[self.sort_index]

    @property
    def main_table(self) -> Table:
        return self.query_one(Table)

    @property
    def muted(self) -> str:
        return self.app.muted  # type: ignore[attr-defined]

    @property
    def faint(self) -> str:
        return self.app.faint  # type: ignore[attr-defined]

    def cycle_sort(self) -> None:
        self.sort_index = (self.sort_index + 1) % len(self.SORTS)
        self.refresh_rows(keep=True)

    def sort_by(self, column: str) -> bool:
        for index, sort in enumerate(self.SORTS):
            if sort.column == column:
                self.sort_index = index
                self.refresh_rows(keep=True)
                return True
        return False

    def set_query(self, query: str) -> None:
        self.filter_text = query
        self.refresh_rows(keep=False)

    def on_resize(self) -> None:
        self.refresh_rows(keep=True)

    def focus_table(self) -> None:
        self.main_table.focus()

    # Subclasses implement the following.

    def load(self, view: ScanView, state: object | None) -> None:
        raise NotImplementedError

    def capture(self) -> object | None:
        raise NotImplementedError

    def refresh_rows(self, *, keep: bool) -> None:
        raise NotImplementedError

    def selected(self) -> Node | None:
        raise NotImplementedError

    def selection(self) -> Selection | None:
        node = self.selected()
        return Selection(node.path()) if node is not None else None


def _render_table(
    table: Table, sort: Sort | None, rows: list[dict], cursor: int
) -> None:
    table.layout_columns(sort)
    for cells in rows:
        table.add_cells(cells)
    table.select_index(cursor)


def _index_of(items: list, item: object) -> int:
    for index, candidate in enumerate(items):
        if candidate is item:
            return index
    return 0


class BrowsePane(Pane):
    SORTS = (BY_SIZE, BY_OLDEST, BY_ITEMS, BY_NAME)
    COLUMNS = (
        Column("size", "SIZE", 9, "right"),
        Column("share", "SHARE", SHARE_BAR_WIDTH + 4, hide_below=64),
        Column("items", "ITEMS", 10, "right", hide_below=80),
        Column("modified", "MODIFIED", 8, "right", hide_below=48),
        Column("name", "NAME", None),
    )
    SMALL = object()  # row marker for the aggregated small files

    def __init__(self, *, id: str) -> None:
        super().__init__(id=id)
        self.current: Node | None = None
        self.rows: list[object] = []

    def compose(self) -> ComposeResult:
        yield Line()
        yield Table(self.COLUMNS)

    def show_loading(self, root_path: str) -> None:
        self.query_one(Line).show(Content.styled(pretty_path(root_path), "bold"))
        table = self.main_table
        table.layout_columns(self.sort)
        table.add_skeleton(self.faint)

    def load(self, view: ScanView, state: object | None) -> None:
        self.view = view
        root = view.result.root
        selected = None
        if isinstance(state, tuple):
            names, selected, self.sort_index, self.filter_text = state
            self.current = resolve(root, names)
        else:
            self.current = root
        self.refresh_rows(keep=False, select_name=selected)

    def capture(self) -> object | None:
        if self.current is None:
            return None
        node = self.selected()
        return (
            names_from_root(self.current),
            node.name if node is not None else None,
            self.sort_index,
            self.filter_text,
        )

    def refresh_rows(self, *, keep: bool, select_name: str | None = None) -> None:
        if self.current is None or self.view is None:
            return
        table = self.main_table
        row = table.cursor_row
        previous = self.rows[row] if keep and 0 <= row < len(self.rows) else None
        current = self.current
        nodes = arrange(current.children or (), self.sort, self.filter_text)
        self.rows = list(nodes[:MAX_ROWS])
        if current.small_count and not self.filter_text:
            self.rows.append(self.SMALL)
        cursor = 0
        if previous is not None:
            cursor = _index_of(self.rows, previous)
        elif select_name is not None:
            cursor = next(
                (
                    i
                    for i, n in enumerate(self.rows)
                    if getattr(n, "name", None) == select_name
                ),
                0,
            )
        now = time.time()
        table.layout_columns(self.sort)
        for row in self.rows:
            table.add_cells(self._cells(row, current, now, table.fill_width))
        if len(nodes) > MAX_ROWS:
            table.add_cells(overflow_row(len(nodes) - MAX_ROWS, self.muted))
        table.select_index(cursor)
        self._update_header(len(nodes))

    def _cells(self, row: object, parent: Node, now: float, width: int) -> dict:
        muted = self.muted
        if row is self.SMALL:
            size = parent.small_size
            mb = self.view.min_file_size // 1_000_000 if self.view else 0
            label = f"{parent.small_count:,} files under {mb} MB"
            return {
                "size": size_cell(size),
                "share": share_cell(size, parent.size, muted),
                "items": count_cell(parent.small_count, muted),
                "modified": Text("", justify="right"),
                "name": Text(label, style=f"{muted} italic"),
            }
        node: Node = row  # type: ignore[assignment]
        name = Text(node.name + ("/" if node.is_dir else ""))
        if node.opaque:
            name.append(" ⊘", style=muted)
        if node.is_dir and node.path() in self.view.unreadable:
            name.append(" ⚠ unreadable", style=self.app.warning)  # type: ignore[attr-defined]
        name.truncate(width, overflow="ellipsis")
        return {
            "size": size_cell(node.size),
            "share": share_cell(node.size, parent.size, muted),
            "items": count_cell(node.file_count, muted) if node.is_dir else Text(""),
            "modified": age_cell(node.mtime, now, muted),
            "name": name,
        }

    def _update_header(self, matches: int) -> None:
        current = self.current
        assert current is not None
        head, sep, tail = pretty_path(current.path()).rstrip("/").rpartition("/")
        left = Content.assemble((head + sep, self.muted), (tail, "bold"))
        total = len(current.children or ()) + (1 if current.small_count else 0)
        if self.filter_text:
            right = filter_status(self.filter_text, matches, total)
        else:
            right = Content(
                f"{human_size(current.size)}  ·  {current.file_count:,} files"
            )
        self.query_one(Line).show(left, right)

    def selected(self) -> Node | None:
        row = self.main_table.cursor_row
        if 0 <= row < len(self.rows):
            node = self.rows[row]
            return node if isinstance(node, Node) else None
        return None

    def selection(self) -> Selection | None:
        row = self.main_table.cursor_row
        if not (0 <= row < len(self.rows)) or self.current is None:
            return None
        node = self.rows[row]
        if node is self.SMALL:
            return Selection(
                self.current.path(), "small files in this folder, combined"
            )
        assert isinstance(node, Node)
        note = "sized, not browsable" if node.opaque else ""
        return Selection(node.path(), note)

    # -- navigation --------------------------------------------------------

    def can_enter(self) -> bool:
        node = self.selected()
        return node is not None and browsable(node)

    def can_leave(self) -> bool:
        return self.current is not None and self.current.parent is not None

    def enter(self) -> None:
        node = self.selected()
        if node is not None and browsable(node):
            self.current = node
            self.filter_text = ""
            self.refresh_rows(keep=False)

    def leave(self) -> None:
        if self.current is None or self.current.parent is None:
            return
        child, self.current = self.current, self.current.parent
        self.filter_text = ""
        self.refresh_rows(keep=False, select_name=child.name)

    def reveal(self, node: Node) -> None:
        """Show ``node`` in its parent directory."""
        parent = node.parent
        if parent is None:
            self.current = node
            self.filter_text = ""
            self.refresh_rows(keep=False)
            return
        self.current = parent
        self.filter_text = ""
        self.refresh_rows(keep=False)
        self.main_table.select_index(_index_of(self.rows, node))


class HotspotsPane(Pane):
    SORTS = (BY_SIZE, BY_OLDEST, BY_PATH)
    CATEGORY_COLUMNS = (
        Column("size", "SIZE", 9, "right"),
        Column("name", "CATEGORY", 20),
        Column("items", "ITEMS", 5, "right"),
    )
    ITEM_COLUMNS = (
        Column("size", "SIZE", 9, "right"),
        Column("modified", "MODIFIED", 8, "right", hide_below=40),
        Column("path", "PATH", None),
    )
    DEFAULT_CSS = """
    HotspotsPane > #lists { height: 1fr; }
    HotspotsPane #categories { width: 45; }
    """

    def __init__(self, *, id: str) -> None:
        super().__init__(id=id)
        self.shown_categories: list[Category] = []
        self.items: list[Node] = []
        self.items_category: str | None = None  # category the items belong to

    def compose(self) -> ComposeResult:
        yield Line()
        with Horizontal(id="lists"):
            yield Table(self.CATEGORY_COLUMNS, id="categories")
            yield Table(self.ITEM_COLUMNS, id="instances")

    @property
    def categories_table(self) -> Table:
        return self.query_one("#categories", Table)

    @property
    def items_table(self) -> Table:
        return self.query_one("#instances", Table)

    @property
    def main_table(self) -> Table:
        return self.categories_table

    @property
    def items_focused(self) -> bool:
        return self.items_table.has_focus

    def show_loading(self) -> None:
        self.query_one(Line).show("")
        for table in (self.categories_table, self.items_table):
            table.layout_columns(None)
            table.add_skeleton(self.faint)

    def load(self, view: ScanView, state: object | None) -> None:
        self.view = view
        category = path = None
        if isinstance(state, tuple):
            category, path, self.sort_index, self.filter_text = state
        self._refresh(category, path)

    def capture(self) -> object | None:
        if self.view is None:
            return None
        category = self._category()
        node = self.selected_item()
        return (
            category.name if category else None,
            node.path() if node else None,
            self.sort_index,
            self.filter_text,
        )

    def refresh_rows(self, *, keep: bool) -> None:
        if self.view is None:
            return
        category = self._category() if keep else None
        node = self.selected_item() if keep else None
        self._refresh(
            category.name if category else None, node.path() if node else None
        )

    def _refresh(self, category_name: str | None, item_path: str | None) -> None:
        assert self.view is not None
        self.shown_categories = []
        for category in self.view.categories:
            nodes = arrange(category.nodes, BY_SIZE, self.filter_text, _display_path)
            if nodes:
                self.shown_categories.append(
                    Category(category.name, sum(n.size for n in nodes), nodes)
                )
        if self.filter_text:
            self.shown_categories.sort(key=lambda c: c.size, reverse=True)
        rows = [
            {
                "size": size_cell(c.size),
                "name": Text(c.name),
                "items": count_cell(len(c.nodes), self.muted),
            }
            for c in self.shown_categories
        ]
        cursor = next(
            (i for i, c in enumerate(self.shown_categories) if c.name == category_name),
            0,
        )
        _render_table(self.categories_table, None, rows, cursor)
        self._refresh_items(item_path)
        self._update_header()

    def _refresh_items(self, item_path: str | None = None) -> None:
        category = self._category()
        nodes = arrange(category.nodes, self.sort) if category else []
        self.items_category = category.name if category else None
        self.items = nodes[:MAX_ROWS]
        table = self.items_table
        table.layout_columns(self.sort)
        now = time.time()
        muted = self.muted
        for node in self.items:
            table.add_cells(
                {
                    "size": size_cell(node.size),
                    "modified": age_cell(node.mtime, now, muted),
                    "path": path_cell(node.path(), table.fill_width, muted),
                }
            )
        if len(nodes) > MAX_ROWS:
            table.add_cells(overflow_row(len(nodes) - MAX_ROWS, muted))
        cursor = 0
        if item_path is not None:
            cursor = next(
                (i for i, n in enumerate(self.items) if n.path() == item_path), 0
            )
        table.select_index(cursor)

    def _update_header(self) -> None:
        assert self.view is not None
        whole = self.view.result.root.size
        total = self.view.hotspot_total
        left = Content.assemble(
            (human_size(total), "bold"),
            (f"  in hotspots  ·  {pct(total, whole).strip()} of scan", self.muted),
        )
        right: Content | str = ""
        if self.filter_text:
            shown = sum(len(c.nodes) for c in self.shown_categories)
            everything = sum(len(c.nodes) for c in self.view.categories)
            right = filter_status(self.filter_text, shown, everything)
        self.query_one(Line).show(left, right)

    def _category(self) -> Category | None:
        row = self.categories_table.cursor_row
        return (
            self.shown_categories[row]
            if 0 <= row < len(self.shown_categories)
            else None
        )

    def selected_item(self) -> Node | None:
        row = self.items_table.cursor_row
        return self.items[row] if 0 <= row < len(self.items) else None

    def selected(self) -> Node | None:
        return self.selected_item() if self.items_focused else None

    def selection(self) -> Selection | None:
        if self.items_focused:
            return super().selection()
        category = self._category()
        if category is None:
            return None
        count = len(category.nodes)
        return Selection(
            category.name, f"{count:,} {'item' if count == 1 else 'items'}"
        )

    def on_data_table_row_highlighted(self, event: DataTable.RowHighlighted) -> None:
        if event.data_table is self.categories_table:
            category = self._category()
            if (category.name if category else None) != self.items_category:
                self._refresh_items()

    def focus_items(self) -> None:
        if self.items:
            self.items_table.focus()

    def focus_categories(self) -> None:
        self.categories_table.focus()


def _display_path(node: Node) -> str:
    return pretty_path(node.path())


class FilesPane(Pane):
    SORTS = (BY_SIZE, BY_OLDEST, BY_PATH)
    COLUMNS = (
        Column("size", "SIZE", 9, "right"),
        Column("modified", "MODIFIED", 8, "right", hide_below=40),
        Column("path", "PATH", None),
    )

    def __init__(self, *, id: str) -> None:
        super().__init__(id=id)
        self.files: list[Node] = []

    def compose(self) -> ComposeResult:
        yield Line()
        yield Table(self.COLUMNS)

    def show_loading(self) -> None:
        self.query_one(Line).show("")
        self.main_table.layout_columns(self.sort)
        self.main_table.add_skeleton(self.faint)

    def load(self, view: ScanView, state: object | None) -> None:
        self.view = view
        path = None
        if isinstance(state, tuple):
            path, self.sort_index, self.filter_text = state
        self._refresh(path)

    def capture(self) -> object | None:
        if self.view is None:
            return None
        node = self.selected()
        return (node.path() if node else None, self.sort_index, self.filter_text)

    def refresh_rows(self, *, keep: bool) -> None:
        if self.view is None:
            return
        node = self.selected() if keep else None
        self._refresh(node.path() if node else None)

    def _refresh(self, select_path: str | None) -> None:
        assert self.view is not None
        everything = [node for _, node in self.view.hotspots.top_files]
        self.files = arrange(everything, self.sort, self.filter_text, _display_path)
        table = self.main_table
        table.layout_columns(self.sort)
        now = time.time()
        muted = self.muted
        for node in self.files:
            table.add_cells(
                {
                    "size": size_cell(node.size),
                    "modified": age_cell(node.mtime, now, muted),
                    "path": path_cell(node.path(), table.fill_width, muted),
                }
            )
        cursor = 0
        if select_path is not None:
            cursor = next(
                (i for i, n in enumerate(self.files) if n.path() == select_path), 0
            )
        table.select_index(cursor)
        result = self.view.result
        left = Content.assemble(
            (f"Largest {len(everything):,}", "bold"),
            (f"  of {result.files:,} files", self.muted),
        )
        right = (
            filter_status(self.filter_text, len(self.files), len(everything))
            if self.filter_text
            else ""
        )
        self.query_one(Line).show(left, right)

    def selected(self) -> Node | None:
        row = self.main_table.cursor_row
        return self.files[row] if 0 <= row < len(self.files) else None
