"""Textual disk-usage inspector and scan coordination."""

from __future__ import annotations

import argparse
import os
import shutil
import subprocess
import threading
import time

from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.color import Color
from textual.containers import Horizontal
from textual.content import Content
from textual.widgets import ContentSwitcher, DataTable, Footer, Input, Static

from ..analysis import analyze, hotspot_total, ranked_categories
from ..cache import load_cache, save_cache
from ..cleanup import Candidate, CleanupReport, detach, run_cleanup, stale_environments
from ..formatting import human_age, human_size, pretty_path
from ..operations import copy_to_clipboard, move_to_trash, open_in_finder
from ..scanner import Scanner, ScanResult
from .panes import BrowsePane, FilesPane, HotspotsPane, Pane, ScanView
from .widgets import CleanupDialog, Clickable, ErrorsDialog, HelpDialog, meter

SPINNER = "⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏"

TABS = (("browse", "Browse"), ("hotspots", "Hotspots"), ("files", "Files"))

# Actions whose availability depends on the view, focus, or selection.
CONTEXTUAL = {
    "open",
    "back",
    "goto",
    "reveal",
    "copy_path",
    "sort",
    "filter",
    "errors",
    "clear_filter",
    "cleanup",
    "rescan",
}


def build_view(result: ScanResult, *, top_files: int, min_file_size: int) -> ScanView:
    hotspots = analyze(result.root, top_n=top_files)
    return ScanView(
        result=result,
        hotspots=hotspots,
        categories=ranked_categories(hotspots),
        hotspot_total=hotspot_total(hotspots),
        min_file_size=min_file_size,
        unreadable=frozenset(error.path for error in result.error_paths),
    )


class DiskHogApp(App):
    TITLE = "diskhog"

    CSS = """
    Screen { background: $background; }
    #top {
        height: 1;
        padding: 0 1;
        background: $panel;
        & > Clickable { width: auto; }
        & > .tab { color: $text-muted; padding-right: 3; }
        & > .tab:hover { color: $foreground; }
        & > .tab.-active { color: $foreground; text-style: bold; }
        & > #status { width: 1fr; text-align: right; color: $text-muted; }
        & > #unreadable { display: none; color: $warning; padding-left: 3; }
        & > #unreadable.-shown { display: block; }
        & > #unreadable:hover { text-style: underline; }
        & > #disk { width: auto; padding-left: 4; }
    }
    #views { height: 1fr; }
    #views.-stale Table { text-opacity: 55%; }
    BrowsePane > Line, HotspotsPane > Line, FilesPane > Line { margin-top: 1; }
    #context {
        height: 1;
        padding: 0 1;
        color: $text-muted;
        text-wrap: nowrap;
        text-overflow: ellipsis;
    }
    #filterbar {
        display: none;
        height: 1;
        padding: 0 1;
        background: $boost;
        & > Static { width: 2; color: $text-muted; }
        & > Input { border: none; padding: 0; height: 1; background: transparent; }
    }
    .-filtering #filterbar { display: block; }
    .-filtering #context { display: none; }
    """

    # Navigation keys are priority bindings so that they reach the app before
    # the focused table; check_action() lets them fall through when they do
    # not apply (for example while typing in the filter).
    BINDINGS = [
        Binding("left,h,backspace", "back", "Back", key_display="←", priority=True),
        Binding("right,l,enter", "open", "Open", key_display="→", priority=True),
        Binding("enter,g", "goto", "Show in Browse", key_display="↵", priority=True),
        Binding("slash", "filter", "Filter", key_display="/"),
        Binding("s", "sort", "Sort"),
        Binding("o", "reveal", "Reveal in Finder"),
        Binding("x", "cleanup", "Clean up"),
        Binding("question_mark", "help", "Help", key_display="?"),
        Binding("escape", "clear_filter", "Clear filter", key_display="esc"),
        Binding("c", "copy_path", "Copy path", show=False),
        Binding("r", "rescan", "Rescan", show=False),
        Binding("e", "errors", "Unreadable paths", show=False),
        Binding("1", "show_tab('browse')", "Browse", show=False),
        Binding("2", "show_tab('hotspots')", "Hotspots", show=False),
        Binding("3", "show_tab('files')", "Files", show=False),
        Binding("q", "quit", "Quit", show=False),
    ]

    def __init__(self, opts: argparse.Namespace) -> None:
        super().__init__()
        self.opts = opts
        self.root_path: str = opts.path
        self.min_file_size = opts.min_file_mb * 1_000_000
        self.view: ScanView | None = None
        self.result_is_cached = False
        # Concrete theme colours: Rich table cells cannot use CSS variables.
        self.muted = "dim"
        self.faint = "dim"
        self.warning = "yellow"
        self._scanner: Scanner | None = None
        self._estimate: int | None = None  # previous total, for scan progress
        self._binding_state: tuple | None = None
        self._cleaning = False

    @property
    def result(self) -> ScanResult | None:
        return self.view.result if self.view else None

    # -- layout ------------------------------------------------------------

    def compose(self) -> ComposeResult:
        with Horizontal(id="top"):
            for tab, label in TABS:
                yield Clickable(
                    f"app.show_tab('{tab}')", id=f"tab-{tab}", classes="tab"
                )
            yield Static(id="status")
            yield Clickable("app.errors", id="unreadable")
            yield Static(id="disk")
        with ContentSwitcher(id="views", initial="browse"):
            yield BrowsePane(id="browse")
            yield HotspotsPane(id="hotspots")
            yield FilesPane(id="files")
        yield Static(id="context")
        with Horizontal(id="filterbar"):
            yield Static("/")
            yield Input(id="filter", compact=True)
        yield Footer(show_command_palette=False)

    def on_mount(self) -> None:
        variables = self.get_css_variables()
        foreground = Color.parse(variables["foreground"])
        background = Color.parse(variables["background"])
        self.muted = foreground.blend(background, 0.4).hex
        self.faint = foreground.blend(background, 0.75).hex
        self.warning = Color.parse(variables["warning"]).hex
        self.query_one(BrowsePane).show_loading(self.root_path)
        self.query_one(HotspotsPane).show_loading()
        self.query_one(FilesPane).show_loading()
        self.query_one(BrowsePane).focus_table()
        self.set_interval(0.25, self._refresh_top)
        self._refresh_top()
        if self.opts.no_cache:
            self._start_scan()
        else:
            threading.Thread(target=self._load_cached, daemon=True).start()

    # -- scan lifecycle ------------------------------------------------------

    def _load_cached(self) -> None:
        cached = load_cache(self.root_path)
        view = None
        if cached is not None:
            view = build_view(
                cached, top_files=self.opts.top_files, min_file_size=self.min_file_size
            )
        self.call_from_thread(self._on_cache_loaded, view)

    def _on_cache_loaded(self, view: ScanView | None) -> None:
        if view is not None and self.view is None:
            self.result_is_cached = True
            self._apply(view)
        age = time.time() - view.result.timestamp if view else None
        if age is None or age > self.opts.cache_ttl_hours * 3600:
            if not self.scanning:
                self._start_scan()

    def _start_scan(self) -> None:
        if self._cleaning:
            self.notify("Moving folders to the Trash; rescan when it finishes.")
            return
        if self.scanning:
            self.notify("Already scanning.", severity="warning")
            return

        def on_done(result: ScanResult) -> None:
            view = build_view(
                result, top_files=self.opts.top_files, min_file_size=self.min_file_size
            )
            self.call_from_thread(self._on_scan_done, view)

        self._estimate = self.result.root.size if self.result else None
        self._scanner = Scanner(
            self.root_path,
            workers=self.opts.workers,
            min_file_size=self.min_file_size,
            on_done=on_done,
        )
        self._scanner.start()
        if self.view is not None:
            self.query_one("#views").add_class("-stale")
        self._refresh_top()

    @property
    def scanning(self) -> bool:
        return self._scanner is not None and not self._scanner.stats.done

    def _on_scan_done(self, view: ScanView) -> None:
        self.result_is_cached = False
        self.query_one("#views").remove_class("-stale")
        self._apply(view)
        if not self.opts.no_cache:
            threading.Thread(
                target=save_cache, args=(view.result,), daemon=True
            ).start()

    def _apply(self, view: ScanView) -> None:
        """Show a scan, keeping each view's location, sort, and filter."""
        panes = list(self.query(Pane))
        states = [pane.capture() for pane in panes]
        self.view = view
        for pane, state in zip(panes, states):
            pane.load(view, state)
        self._refresh_top()
        self._refresh_context()
        self._refresh_bindings_if_changed()

    # -- top line ------------------------------------------------------------

    def _refresh_top(self) -> None:
        active = self.tab
        for index, (tab, label) in enumerate(TABS, start=1):
            widget = self.query_one(f"#tab-{tab}", Clickable)
            widget.set_class(tab == active, "-active")
            name = (label, "underline") if tab == active else label
            widget.update(Content.assemble(name, (f" {index}", self.faint)))
        # Narrow terminals keep the tabs and drop secondary status first.
        width = self.size.width
        status = self._status()
        if width < 84 and not self.scanning:
            status = Content("")
        self.query_one("#status", Static).update(status)
        errors = self.view.result.errors if self.view and not self.scanning else 0
        unreadable = self.query_one("#unreadable", Clickable)
        unreadable.set_class(bool(errors), "-shown")
        unreadable.update(f"⚠ {errors:,} unreadable")
        disk = shutil.disk_usage(self.root_path)
        free = f"{human_size(disk.free)} free"
        if width >= 100:
            free = Content.assemble(
                f"{free} of {human_size(disk.total)}  ",
                meter(1 - disk.free / disk.total, 8, self.faint),
            )
        self.query_one("#disk", Static).update(free)

    def _status(self) -> Content:
        scanner = self._scanner
        if self.scanning:
            assert scanner is not None
            s = scanner.stats
            elapsed = time.monotonic() - s.started
            parts: list = [SPINNER[int(elapsed * 10) % len(SPINNER)], " Scanning"]
            if self._estimate:
                fraction = min(0.99, s.bytes / self._estimate)
                parts += [f"  {fraction:.0%} ", meter(fraction, 10, self.faint)]
            if self.size.width >= 100:
                parts.append(f"  {s.files:,} files")
            return Content.assemble(*parts)
        if self.view is not None:
            age = time.time() - self.view.result.timestamp
            return Content(
                "scanned " + ("just now" if age < 60 else f"{human_age(age)} ago")
            )
        return Content("Loading last scan…")

    # -- bottom line ---------------------------------------------------------

    @property
    def tab(self) -> str:
        return self.query_one(ContentSwitcher).current or "browse"

    @property
    def pane(self) -> Pane:
        return self.query_one(f"#{self.tab}", Pane)

    def _refresh_context(self) -> None:
        context = self.query_one("#context", Static)
        selection = self.pane.selection() if self.view else None
        if selection is None:
            context.update("")
            return
        parts: list = [(pretty_path(selection.path), "$foreground")]
        if selection.note:
            parts.append(f"  ·  {selection.note}")
        context.update(Content.assemble(*parts))

    # -- events --------------------------------------------------------------

    def on_data_table_row_highlighted(self, event: DataTable.RowHighlighted) -> None:
        self._refresh_context()
        self._refresh_bindings_if_changed()

    def on_data_table_row_selected(self, event: DataTable.RowSelected) -> None:
        # Mouse clicks on the highlighted row; Enter is handled by bindings.
        if self.check_action("open", ()):
            self.action_open()
        elif self.check_action("goto", ()):
            self.action_goto()

    def on_data_table_header_selected(self, event: DataTable.HeaderSelected) -> None:
        if event.data_table.id == "categories" or self.view is None:
            return
        if self.pane.sort_by(str(event.column_key.value)):
            self._refresh_context()

    def on_resize(self) -> None:
        self._refresh_top()

    def on_descendant_focus(self) -> None:
        self._refresh_context()
        self._refresh_bindings_if_changed()

    def on_input_changed(self, event: Input.Changed) -> None:
        if self.view is not None:
            self.pane.set_query(event.value)
            self._refresh_context()

    def on_input_submitted(self, event: Input.Submitted) -> None:
        self._close_filter()
        self.pane.focus_table()

    # -- bindings --------------------------------------------------------------

    def check_action(self, action: str, parameters: tuple[object, ...]) -> bool | None:
        """Hide actions that do not apply to the view; disable inapplicable ones.

        False hides the key and lets it fall through to the focused widget;
        None shows it disabled.
        """
        if action not in CONTEXTUAL:
            return True
        if action in ("open", "back", "goto") and len(self.screen_stack) > 1:
            return False  # a dialog is open: let it have the keys
        if action == "rescan":
            return None if self._cleaning else True
        if action == "cleanup":
            if self.tab != "hotspots":
                return False
            busy = self.view is None or self.scanning or self._cleaning
            return None if busy else True
        typing = isinstance(self.focused, Input)
        tab = self.tab
        if action in ("open", "back", "goto"):
            if typing:
                return False
            if action == "goto" and tab == "browse":
                return False
            if action in ("open", "back") and tab == "files":
                return False
        if action in ("errors", "clear_filter"):
            if self.view is None:
                return False
            if action == "errors":
                return bool(self.view.result.errors)
            return bool(self.pane.filter_text) or typing
        if self.view is None:
            return None
        pane = self.pane
        if isinstance(pane, BrowsePane):
            if action == "open":
                return True if pane.can_enter() else None
            if action == "back":
                return True if pane.can_leave() else None
        if isinstance(pane, HotspotsPane):
            if action == "open":
                return None if pane.items_focused else True
            if action == "back":
                return True if pane.items_focused else None
        if action in ("goto", "reveal", "copy_path"):
            return True if pane.selected() is not None else None
        return True

    def _refresh_bindings_if_changed(self) -> None:
        state = tuple(
            self.check_action(action, ()) for action in sorted(CONTEXTUAL)
        ) + (self.tab,)
        if state != self._binding_state:
            self._binding_state = state
            self.refresh_bindings()

    # -- actions ---------------------------------------------------------------

    def action_open(self) -> None:
        pane = self.pane
        if isinstance(pane, BrowsePane):
            pane.enter()
        elif isinstance(pane, HotspotsPane):
            pane.focus_items()
        self._refresh_context()
        self._refresh_bindings_if_changed()

    def action_back(self) -> None:
        pane = self.pane
        if isinstance(pane, BrowsePane):
            pane.leave()
        elif isinstance(pane, HotspotsPane):
            pane.focus_categories()
        self._refresh_context()
        self._refresh_bindings_if_changed()

    def action_goto(self) -> None:
        node = self.pane.selected()
        if node is None:
            return
        self.query_one(BrowsePane).reveal(node)
        self.action_show_tab("browse")

    def action_sort(self) -> None:
        self.pane.cycle_sort()
        self._refresh_context()

    def action_filter(self) -> None:
        filter_input = self.query_one("#filter", Input)
        filter_input.value = self.pane.filter_text
        self.screen.add_class("-filtering")
        filter_input.focus()

    def action_clear_filter(self) -> None:
        self._close_filter()
        if self.pane.filter_text:
            self.pane.set_query("")
        self.pane.focus_table()
        self._refresh_context()

    def _close_filter(self) -> None:
        self.screen.remove_class("-filtering")

    def action_show_tab(self, tab: str) -> None:
        self._close_filter()
        self.query_one(ContentSwitcher).current = tab
        self.pane.focus_table()
        self._refresh_top()
        self._refresh_context()
        self._refresh_bindings_if_changed()

    def action_rescan(self) -> None:
        self._start_scan()

    # -- cleanup ---------------------------------------------------------------

    def action_cleanup(self) -> None:
        if self.view is None or self.scanning or self._cleaning:
            return
        now = time.time()
        candidates = stale_environments(self.view.result.root, now=now)
        self.push_screen(CleanupDialog(candidates, now), self._on_cleanup_confirmed)

    def _on_cleanup_confirmed(self, chosen: list[Candidate] | None) -> None:
        if not chosen or self.view is None:
            return
        self._cleaning = True
        self._refresh_bindings_if_changed()
        noun = "folder" if len(chosen) == 1 else "folders"
        self.notify(f"Moving {len(chosen)} {noun} to the Trash…")
        scanned_at = self.view.result.timestamp

        def work() -> None:
            report = run_cleanup(chosen, scanned_at=scanned_at, trash=move_to_trash)
            self.call_from_thread(self._on_cleanup_done, report)

        threading.Thread(target=work, daemon=True).start()

    def _on_cleanup_done(self, report: CleanupReport) -> None:
        self._cleaning = False
        assert self.view is not None
        result = self.view.result
        for candidate in report.moved:
            result.files -= detach(candidate.node)
        if report.moved:
            self._apply(
                build_view(
                    result,
                    top_files=self.opts.top_files,
                    min_file_size=self.min_file_size,
                )
            )
            if not self.opts.no_cache:
                threading.Thread(target=save_cache, args=(result,), daemon=True).start()
        self._refresh_bindings_if_changed()
        self._notify_cleanup(report)

    def _notify_cleanup(self, report: CleanupReport) -> None:
        lines = []
        if report.moved:
            count = len(report.moved)
            noun = "folder" if count == 1 else "folders"
            lines.append(
                f"Moved {count} {noun} ({human_size(report.moved_size)}) to the "
                "Trash. Empty the Trash to free the space."
            )
        for candidate, reason in report.skipped[:3]:
            lines.append(f"Skipped {pretty_path(candidate.path)}: {reason}")
        for candidate, reason in report.failed[:3]:
            lines.append(f"Failed {pretty_path(candidate.path)}: {reason}")
        hidden = len(report.skipped) + len(report.failed) - 6
        if hidden > 0:
            lines.append(f"…and {hidden} more")
        severity = "error" if report.failed else "information"
        if not report.moved and not report.failed:
            severity = "warning"
        self.notify("\n".join(lines), severity=severity, timeout=12)

    def action_help(self) -> None:
        self.push_screen(HelpDialog())

    def action_errors(self) -> None:
        if self.view is not None:
            result = self.view.result
            self.push_screen(ErrorsDialog(result.error_paths, result.errors))

    def action_reveal(self) -> None:
        node = self.pane.selected()
        if node is None:
            return
        path = node.path()
        if not os.path.exists(path):
            self.notify(f"No longer exists: {pretty_path(path)}", severity="error")
            return
        try:
            open_in_finder(path)
        except (OSError, subprocess.CalledProcessError) as exc:
            self.notify(f"Could not open Finder: {exc}", severity="error")

    def action_copy_path(self) -> None:
        node = self.pane.selected()
        if node is None:
            return
        try:
            copy_to_clipboard(node.path())
        except (OSError, subprocess.CalledProcessError) as exc:
            self.notify(f"Could not copy: {exc}", severity="error")
            return
        self.notify(f"Copied {pretty_path(node.path())}")
