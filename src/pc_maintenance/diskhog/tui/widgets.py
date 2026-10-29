"""Shared widgets: aligned tables, one-line bars, and modal dialogs."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass

from rich.text import Text
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical
from textual.content import Content
from textual.coordinate import Coordinate
from textual.screen import ModalScreen
from textual.widgets import Button, DataTable, Static

from ..cleanup import Candidate
from ..formatting import human_age, human_size, pretty_path, share_bar, truncate_left
from ..scanner import ScanError
from .rows import Sort

SKELETON_ROWS = 12


@dataclass(frozen=True)
class Column:
    key: str
    label: str
    width: int | None  # None: take the remaining width
    justify: str = "left"
    hide_below: int = 0  # drop the column when the table is narrower


class Table(DataTable):
    """A row-cursor table with fixed columns and one flexible column."""

    DEFAULT_CSS = """
    Table {
        height: 1fr;
        scrollbar-size-vertical: 1;
        background: $background;
        & > .datatable--header {
            background: $background;
            color: $text-muted;
            text-style: none;
        }
        & > .datatable--header-hover {
            background: $boost;
        }
        &:blur > .datatable--cursor {
            background: $boost;
            color: $foreground;
        }
    }
    """

    def __init__(self, columns: Sequence[Column], **kwargs) -> None:
        super().__init__(cursor_type="row", cell_padding=1, **kwargs)
        self.all_columns = tuple(columns)
        self.shown: tuple[Column, ...] = ()
        self.fill_width = 0

    def layout_columns(self, sort: Sort | None) -> None:
        width = self.size.width or self.app.size.width
        shown = tuple(c for c in self.all_columns if width >= c.hide_below)
        fixed = sum(c.width or 0 for c in shown) + 2 * len(shown) + 1
        self.fill_width = max(12, width - fixed)
        self.shown = shown
        self.show_cursor = True
        self.clear(columns=True)
        for column in shown:
            label = column.label
            if sort is not None and sort.column == column.key:
                label += " ↑" if sort.ascending else " ↓"
            self.add_column(
                Text(label, justify=column.justify),
                width=column.width or self.fill_width,
                key=column.key,
            )

    def add_cells(self, cells: Mapping[str, object]) -> None:
        self.add_row(*(cells.get(c.key, "") for c in self.shown))

    def add_skeleton(self, muted: str) -> None:
        """Placeholder rows with the final column geometry."""
        self.show_cursor = False
        for row in range(SKELETON_ROWS):
            cells = {}
            for column in self.shown:
                width = column.width or max(8, self.fill_width * (3 + row % 4) // 8)
                cells[column.key] = Text("░" * width, style=muted)
            self.add_cells(cells)

    def select_index(self, index: int) -> None:
        if self.row_count:
            self.move_cursor(row=max(0, min(index, self.row_count - 1)))


class Clickable(Static):
    """A label that runs an app action when clicked."""

    def __init__(self, action: str, **kwargs) -> None:
        super().__init__(**kwargs)
        self.action = action

    async def on_click(self) -> None:
        await self.app.run_action(self.action)


class Line(Horizontal):
    """One line with left-aligned and right-aligned text."""

    DEFAULT_CSS = """
    Line {
        height: 1;
        padding: 0 1;
        & > .left { width: 1fr; text-wrap: nowrap; text-overflow: ellipsis; }
        & > .right { width: auto; color: $text-muted; padding-left: 2; }
    }
    """

    def compose(self) -> ComposeResult:
        yield Static(classes="left")
        yield Static(classes="right")

    def show(self, left: Content | str = "", right: Content | str = "") -> None:
        self.query_one(".left", Static).update(left)
        self.query_one(".right", Static).update(right)


def meter(fraction: float, width: int, track: str) -> Content:
    """A filled bar over a faint track, e.g. ``████▌░░░``."""
    filled = share_bar(fraction, width).rstrip()
    return Content.assemble(filled, ("░" * (width - len(filled)), track))


class Dialog(ModalScreen[None]):
    DEFAULT_CSS = """
    Dialog {
        align: center middle;
        background: $background 60%;
        & > Vertical {
            width: 72;
            max-width: 95%;
            height: auto;
            max-height: 90%;
            padding: 1 2;
            background: $surface;
        }
        .title { text-style: bold; padding-bottom: 1; }
        .hint { color: $text-muted; padding-top: 1; }
    }
    """
    BINDINGS = [
        Binding("escape,q,question_mark", "dismiss", "Close", show=False),
    ]


HELP = [
    ("Navigate", [("↑ ↓", "move"), ("→ ↵", "open"), ("←", "back"), ("1 2 3", "views")]),
    ("Find", [("/", "filter"), ("esc", "clear filter"), ("s", "sort")]),
    (
        "Act",
        [("↵", "show in Browse"), ("o", "reveal in Finder"), ("c", "copy path")],
    ),
    ("Scan", [("r", "rescan"), ("e", "unreadable paths"), ("q", "quit")]),
    ("Hotspots", [("x", "move stale envs to Trash")]),
]


class HelpDialog(Dialog):
    def compose(self) -> ComposeResult:
        parts: list[tuple[str, str] | str] = []
        for index, (group, keys) in enumerate(HELP):
            if index:
                parts.append("\n")
            parts.append((f"{group:<10}", self.app.muted))  # type: ignore[attr-defined]
            for position, (key, label) in enumerate(keys):
                if position:
                    parts.append("   ")
                parts.append((key, "bold"))
                parts.append(f" {label}")
        text = Content.assemble(*parts)
        with Vertical():
            yield Static("Keys", classes="title")
            yield Static(text)
            yield Static("Click a column title to sort by it.", classes="hint")


class ErrorsDialog(Dialog):
    DEFAULT_CSS = """
    ErrorsDialog > Vertical { width: 100; }
    ErrorsDialog DataTable { width: 100%; background: $surface; }
    """

    def __init__(self, errors: Sequence[ScanError], total: int) -> None:
        super().__init__()
        self.errors = errors
        self.total = total

    def compose(self) -> ComposeResult:
        noun = "path" if self.total == 1 else "paths"
        listed = len(self.errors)
        more = f", first {listed} listed" if listed < self.total else ""
        table = DataTable(cursor_type="none", show_header=False)
        table.styles.height = min(listed, 20) or 1
        reason_width = max((len(e.reason) for e in self.errors), default=0)
        path_width = max(20, min(100, self.app.size.width) - 12 - reason_width)
        table.add_columns("Path", "Reason")
        for error in self.errors:
            table.add_row(
                Text(truncate_left(pretty_path(error.path), path_width)),
                Text(error.reason, style=self.app.muted),  # type: ignore[attr-defined]
            )
        with Vertical():
            yield Static(f"{self.total:,} unreadable {noun}{more}", classes="title")
            yield table
            yield Static(
                "Sizes exclude these. If access was denied, give your terminal "
                "Full Disk Access in System Settings › Privacy & Security, "
                "then press r to rescan.",
                classes="hint",
            )


class CleanupDialog(Dialog):
    """Preview stale environments; dismiss with the confirmed selection."""

    COLUMNS = (
        Column("check", "", 1),
        Column("size", "SIZE", 9, "right"),
        Column("modified", "MODIFIED", 8, "right"),
        Column("kind", "KIND", 12),
        Column("path", "PATH", None),
    )
    DEFAULT_CSS = """
    CleanupDialog > Vertical { width: 110; }
    CleanupDialog .subtitle { color: $text-muted; padding-bottom: 1; }
    CleanupDialog Table { height: auto; max-height: 18; background: $surface; }
    CleanupDialog #actions { height: auto; padding-top: 1; }
    CleanupDialog #actions > Static { width: 1fr; color: $text-muted; padding-top: 1; }
    CleanupDialog Button { margin-left: 2; }
    """
    BINDINGS = [
        Binding("space", "toggle", "Toggle", show=False, priority=True),
        Binding("a", "toggle_all", "All", show=False, priority=True),
        Binding("enter", "confirm", "Move to Trash", show=False, priority=True),
    ]

    def __init__(self, candidates: Sequence[Candidate], now: float) -> None:
        super().__init__()
        self.candidates = list(candidates)
        self.chosen = set(range(len(self.candidates)))
        self.now = now

    def compose(self) -> ComposeResult:
        with Vertical():
            yield Static("Move stale environments to Trash", classes="title")
            if not self.candidates:
                yield Static("No Python envs or node_modules unchanged for 6 months.")
                with Horizontal(id="actions"):
                    yield Static("")
                    yield Button("Close", id="cancel")
                return
            yield Static(
                "Python envs and node_modules unchanged for 6 months. "
                "Restore them from the Trash, or rebuild with uv sync or npm install.",
                classes="subtitle",
            )
            yield Table(self.COLUMNS)
            with Horizontal(id="actions"):
                yield Static("space select   a all")
                yield Button("Cancel  esc", id="cancel")
                yield Button("", id="confirm", variant="error")

    def on_mount(self) -> None:
        if self.candidates:
            self.call_after_refresh(self._render_rows)
            self._update_summary()

    def _render_rows(self) -> None:
        table = self.query_one(Table)
        table.layout_columns(None)
        muted = self.app.muted  # type: ignore[attr-defined]
        for index, candidate in enumerate(self.candidates):
            table.add_cells(
                {
                    "check": self._mark(index),
                    "size": Text(human_size(candidate.size), justify="right"),
                    "modified": Text(
                        human_age(self.now - candidate.mtime),
                        style=muted,
                        justify="right",
                    ),
                    "kind": Text(candidate.kind, style=muted),
                    "path": Text(
                        truncate_left(pretty_path(candidate.path), table.fill_width)
                    ),
                }
            )
        table.focus()

    def _mark(self, index: int) -> Text:
        return Text("✓" if index in self.chosen else "·", style="bold")

    def _update_summary(self) -> None:
        count = len(self.chosen)
        size = sum(self.candidates[i].size for i in self.chosen)
        noun = "folder" if count == 1 else "folders"
        button = self.query_one("#confirm", Button)
        button.label = f"Move {count} {noun} · {human_size(size)} to Trash  ↵"
        button.disabled = count == 0

    def action_toggle(self) -> None:
        table = self.query_one(Table)
        index = table.cursor_row
        if not 0 <= index < len(self.candidates):
            return
        self.chosen ^= {index}
        table.update_cell_at(Coordinate(index, 0), self._mark(index))
        self._update_summary()
        table.move_cursor(row=index + 1)

    def action_toggle_all(self) -> None:
        if len(self.chosen) == len(self.candidates):
            self.chosen.clear()
        else:
            self.chosen = set(range(len(self.candidates)))
        table = self.query_one(Table)
        for index in range(len(self.candidates)):
            table.update_cell_at(Coordinate(index, 0), self._mark(index))
        self._update_summary()

    def action_confirm(self) -> None:
        if not self.candidates:
            self.dismiss(None)
        elif self.chosen:
            self.dismiss([self.candidates[i] for i in sorted(self.chosen)])

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "confirm":
            self.action_confirm()
        else:
            self.dismiss(None)

    def check_action(self, action: str, parameters: tuple[object, ...]) -> bool | None:
        if action in ("toggle", "toggle_all"):
            return bool(self.candidates)
        return True
