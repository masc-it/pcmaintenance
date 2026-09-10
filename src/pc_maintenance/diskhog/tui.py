"""Textual disk-usage interface and scan coordination."""

from __future__ import annotations

import argparse
import os
import subprocess
import threading
import time

from rich.markup import escape
from rich.text import Text
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.widgets import (
    DataTable,
    Footer,
    Header,
    Static,
    TabbedContent,
    TabPane,
    Tree,
)

from .analysis import ML_BIN_MIN_SIZE, ML_FILE_EXTS, Hotspots, analyze
from .cache import load_cache, save_cache
from .formatting import human_age, human_size, pct, pretty_path
from .operations import open_in_finder
from .scanner import Node, Scanner, ScanResult

MAX_TREE_CHILDREN = 400  # cap children shown per tree node


MAX_HOTSPOT_INSTANCES = 100  # cap instances listed per category


SPINNER = "⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏"


CATEGORY_COLORS = {
    "Node modules": "yellow",
    "Python venvs": "green",
    "Build & tool caches": "cyan",
    "Git repos (.git)": "orange1",
    "Data dirs": "magenta",
    "ML models & datasets": "red",
    "System & app caches": "blue",
    "Toolchains & SDKs": "bright_cyan",
    "Docker & VMs": "bright_blue",
    "Trash": "grey62",
}


class DiskHogApp(App):
    TITLE = "diskhog"

    CSS = """
    #status {
        height: 1;
        padding: 0 1;
        background: $panel;
    }
    #selpath {
        height: 1;
        padding: 0 1;
        color: $text-muted;
    }
    Tree {
        padding: 0 1;
    }
    """

    BINDINGS = [
        Binding("q", "quit", "Quit"),
        Binding("r", "rescan", "Rescan"),
        Binding("1", "show_tab('tab-tree')", "Tree"),
        Binding("2", "show_tab('tab-hotspots')", "Hotspots"),
        Binding("3", "show_tab('tab-files')", "Big files"),
        Binding("o", "open_selected", "Open in Finder"),
        # Finder-style navigation (option+arrow; cmd+arrow is typically
        # consumed by the terminal, e.g. Ghostty's jump_to_prompt).
        Binding("alt+down", "descend", "Enter dir", show=False),
        Binding("alt+up", "ascend", "Parent dir", show=False),
    ]

    def __init__(self, opts: argparse.Namespace) -> None:
        super().__init__()
        self.opts = opts
        self.root_path: str = opts.path
        self.result: ScanResult | None = None
        self.hotspots: Hotspots | None = None
        self.result_is_cached = False
        self._scanner: Scanner | None = None
        self._files_paths: list[str] = []

    # -- layout ------------------------------------------------------------

    def compose(self) -> ComposeResult:
        yield Header()
        yield Static("", id="status")
        yield Static("", id="selpath")
        with TabbedContent(initial="tab-tree"):
            with TabPane("Tree", id="tab-tree"):
                yield Tree(self.root_path, id="dirtree")
            with TabPane("Hotspots", id="tab-hotspots"):
                yield Tree("Hotspots", id="hotspot-tree")
            with TabPane("Largest files", id="tab-files"):
                yield DataTable(id="files-table")
        yield Footer()

    def on_mount(self) -> None:
        table = self.query_one("#files-table", DataTable)
        table.cursor_type = "row"
        table.zebra_stripes = True
        table.add_columns("Size", "Type", "Path")

        cached = None if self.opts.no_cache else load_cache(self.root_path)
        if cached is not None:
            self.result_is_cached = True
            self._apply_result(cached, save=False)
        age = time.time() - cached.timestamp if cached else None
        if cached is None or age > self.opts.cache_ttl_hours * 3600:
            self._start_scan()
        self.set_interval(0.5, self._refresh_status)
        self._refresh_status()

    # -- scanning ----------------------------------------------------------

    def _start_scan(self) -> None:
        if self._scanner is not None and not self._scanner.stats.done:
            self.notify("A scan is already running.", severity="warning")
            return

        def on_done(result: ScanResult) -> None:
            self.call_from_thread(self._on_scan_done, result)

        self._scanner = Scanner(
            self.root_path,
            workers=self.opts.workers,
            min_file_size=self.opts.min_file_mb * 1024 * 1024,
            on_done=on_done,
        )
        self._scanner.start()

    def _on_scan_done(self, result: ScanResult) -> None:
        self.result_is_cached = False
        self._apply_result(result, save=not self.opts.no_cache)
        self.notify(
            f"Scan complete: {human_size(result.root.size)} "
            f"in {result.files:,} files ({result.duration:.1f}s)"
        )

    def _apply_result(self, result: ScanResult, *, save: bool) -> None:
        self.result = result
        self.hotspots = analyze(result.root, top_n=self.opts.top_files)
        self._rebuild_dir_tree()
        self._rebuild_hotspots()
        self._rebuild_files_table()
        self._refresh_status()
        if save:
            threading.Thread(target=save_cache, args=(result,), daemon=True).start()

    # -- status line -------------------------------------------------------

    def _refresh_status(self) -> None:
        status = self.query_one("#status", Static)
        scanner = self._scanner
        if scanner is not None and not scanner.stats.done:
            s = scanner.stats
            elapsed = time.monotonic() - s.started
            frame = SPINNER[int(elapsed * 10) % len(SPINNER)]
            err = f"  [red]{s.errors} errors[/]" if s.errors else ""
            status.update(
                f"[yellow]{frame} Scanning[/] {escape(self.root_path)} — "
                f"{s.dirs:,} dirs · {s.files:,} files · "
                f"[b]{human_size(s.bytes)}[/] · {elapsed:,.0f}s{err}"
            )
            return
        if self.result is not None:
            r = self.result
            age = time.time() - r.timestamp
            cache_note = (
                f"  [dim](cached {human_age(age)} ago — press r to rescan)[/]"
                if self.result_is_cached
                else ""
            )
            err = f"  [red]{r.errors} errors[/]" if r.errors else ""
            status.update(
                f"[green]✓[/] {escape(self.root_path)} — [b]{human_size(r.root.size)}[/] "
                f"in {r.files:,} files · scanned in {r.duration:.1f}s{err}{cache_note}"
            )
        else:
            status.update("[dim]No scan data yet.[/]")

    # -- directory tree tab --------------------------------------------------

    def _rebuild_dir_tree(self) -> None:
        assert self.result is not None
        tree = self.query_one("#dirtree", Tree)
        root = self.result.root
        label = f"[b]{human_size(root.size):>9}[/]  {escape(self.root_path)}"
        tree.reset(label, data={"n": root, "loaded": False})
        self._populate_children(tree.root)
        tree.root.expand()

    def _populate_children(self, tree_node) -> None:
        data = tree_node.data
        if data is None or data["loaded"]:
            return
        n: Node = data["n"]
        parent_size = max(n.size, 1)
        entries: list[tuple[int, Node | None]] = [
            (c.size, c) for c in (n.children or [])
        ]
        if n.small_count:
            entries.append((n.small_size, None))  # aggregated small files
        entries.sort(key=lambda t: t[0], reverse=True)
        shown = entries[:MAX_TREE_CHILDREN]
        for size, child in shown:
            if child is None:
                tree_node.add_leaf(
                    f"[dim]{human_size(size):>9}  ▏{pct(size, parent_size)}[/] "
                    f"[dim]({n.small_count:,} files < {self.opts.min_file_mb} MB)[/]"
                )
                continue
            label = self._node_label(child, parent_size)
            if child.is_dir and not child.opaque and child.children:
                tree_node.add(label, data={"n": child, "loaded": False})
            else:
                tree_node.add_leaf(label, data={"n": child, "loaded": True})
        hidden = len(entries) - len(shown)
        if hidden > 0:
            tree_node.add_leaf(f"[dim]… +{hidden:,} more items[/]")
        data["loaded"] = True

    def _node_label(self, n: Node, parent_size: int) -> str:
        frac = n.size / parent_size if parent_size else 0.0
        width = 12
        filled = min(width, round(frac * width))
        bar = "█" * filled + "[dim]" + "·" * (width - filled) + "[/]"
        if frac >= 0.5:
            color = "red"
        elif frac >= 0.2:
            color = "yellow"
        elif frac >= 0.05:
            color = "cyan"
        else:
            color = "dim"
        name = escape(n.name)
        if n.is_dir:
            name_color = "magenta" if n.opaque else "bold"
            suffix = "/"
            extra = f" [dim]({n.file_count:,} files)[/]" if n.opaque else ""
        else:
            name_color = "white"
            suffix = ""
            extra = ""
        return (
            f"[{color}]{human_size(n.size):>9}[/] {bar} "
            f"[dim]{frac:4.0%}[/] [{name_color}]{name}{suffix}[/]{extra}"
        )

    # -- hotspots tab --------------------------------------------------------

    def _rebuild_hotspots(self) -> None:
        assert self.hotspots is not None and self.result is not None
        tree = self.query_one("#hotspot-tree", Tree)
        total_size = max(self.result.root.size, 1)
        tree.reset("[b]Hotspots[/] [dim](disk usage — categories may overlap)[/]")
        ranked = sorted(
            self.hotspots.categories.items(),
            key=lambda kv: sum(n.size for n in kv[1]),
            reverse=True,
        )
        for cat, nodes in ranked:
            total = sum(n.size for n in nodes)
            color = CATEGORY_COLORS.get(cat, "white")
            cat_node = tree.root.add(
                f"[b {color}]{human_size(total):>9}[/] "
                f"[dim]{pct(total, total_size)} of scan[/]  "
                f"[b]{cat}[/] [dim]({len(nodes)} items)[/]"
            )
            for n in sorted(nodes, key=lambda x: x.size, reverse=True)[
                :MAX_HOTSPOT_INSTANCES
            ]:
                p = n.path()
                cat_node.add_leaf(
                    f"[{color}]{human_size(n.size):>9}[/]  {escape(pretty_path(p))}",
                    data={"path": p, "size": n.size},
                )
            if len(nodes) > MAX_HOTSPOT_INSTANCES:
                cat_node.add_leaf(
                    f"[dim]… +{len(nodes) - MAX_HOTSPOT_INSTANCES} more[/]"
                )
        tree.root.expand()

    # -- largest files tab ---------------------------------------------------

    def _rebuild_files_table(self) -> None:
        assert self.hotspots is not None
        table = self.query_one("#files-table", DataTable)
        table.clear()
        self._files_paths = []
        for size, node in self.hotspots.top_files:
            p = node.path()
            ext = os.path.splitext(node.name)[1].lower().lstrip(".") or "-"
            is_model = f".{ext}" in ML_FILE_EXTS or (
                ext == "bin" and size >= ML_BIN_MIN_SIZE
            )
            type_text = Text(ext, style="red" if is_model else "dim")
            table.add_row(
                Text(human_size(size), justify="right"),
                type_text,
                pretty_path(p),
            )
            self._files_paths.append(p)

    # -- events --------------------------------------------------------------

    def on_tree_node_expanded(self, event: Tree.NodeExpanded) -> None:
        if event.control.id == "dirtree":
            self._populate_children(event.node)

    def on_tree_node_highlighted(self, event: Tree.NodeHighlighted) -> None:
        selpath = self.query_one("#selpath", Static)
        data = event.node.data
        if not data:
            selpath.update("")
            return
        if "n" in data:
            n: Node = data["n"]
            selpath.update(f"[dim]{escape(n.path())}[/]  [b]{human_size(n.size)}[/]")
        elif "path" in data:
            selpath.update(
                f"[dim]{escape(data['path'])}[/]  [b]{human_size(data['size'])}[/]"
            )

    # -- actions ---------------------------------------------------------------

    def _active_tree(self) -> Tree | None:
        active = self.query_one(TabbedContent).active
        if active == "tab-tree":
            return self.query_one("#dirtree", Tree)
        if active == "tab-hotspots":
            return self.query_one("#hotspot-tree", Tree)
        return None

    def action_descend(self) -> None:
        """alt+down: open the selected tree item and step into it."""
        tree = self._active_tree()
        if tree is None:
            return
        node = tree.cursor_node
        if node is None or not node.allow_expand:
            return
        if tree.id == "dirtree":
            self._populate_children(node)  # ensure children exist synchronously
        if not node.is_expanded:
            node.expand()
        if node.children:
            # Tree lines are rebuilt on refresh; move the cursor after that.
            self.call_after_refresh(tree.move_cursor, node.children[0])

    def action_ascend(self) -> None:
        """alt+up: leave the current item — cursor to parent, closing it."""
        tree = self._active_tree()
        if tree is None:
            return
        node = tree.cursor_node
        if node is None or node.parent is None:
            return
        parent = node.parent
        if parent is not tree.root:  # never collapse the root away
            parent.collapse()
        tree.move_cursor(parent)

    def action_show_tab(self, tab: str) -> None:
        self.query_one(TabbedContent).active = tab

    def action_rescan(self) -> None:
        self._start_scan()
        self._refresh_status()

    def action_open_selected(self) -> None:
        path = self._selected_path()
        if path is None:
            self.notify("Nothing selected.", severity="warning")
            return
        if not os.path.exists(path):
            self.notify(f"Path no longer exists: {path}", severity="error")
            return
        try:
            open_in_finder(path)
            self.notify(f"Opened in Finder: {pretty_path(path)}")
        except (OSError, subprocess.CalledProcessError) as exc:
            self.notify(f"Could not open: {exc}", severity="error")

    def _selected_path(self) -> str | None:
        active = self.query_one(TabbedContent).active
        if active == "tab-tree":
            node = self.query_one("#dirtree", Tree).cursor_node
            if node and node.data and "n" in node.data:
                return node.data["n"].path()
        elif active == "tab-hotspots":
            node = self.query_one("#hotspot-tree", Tree).cursor_node
            if node and node.data and "path" in node.data:
                return node.data["path"]
        elif active == "tab-files":
            table = self.query_one("#files-table", DataTable)
            row = table.cursor_row
            if 0 <= row < len(self._files_paths):
                return self._files_paths[row]
        return None


class KeyDebugApp(App):
    TITLE = "diskhog key debug"
    BINDINGS = [Binding("ctrl+c", "quit", "Quit")]
    CSS = "Static { padding: 1 2; }"

    def __init__(self) -> None:
        super().__init__()
        self._keys: list[str] = []

    def compose(self) -> ComposeResult:
        yield Header()
        yield Static(
            "Press keys to see what the terminal delivers "
            "(cmd+↓ should show [b]super+down[/]). ctrl+c to quit.\n",
            id="keylog",
        )
        yield Footer()

    def on_key(self, event) -> None:
        self._keys.append(event.key)
        log = "\n".join(f"[b]{escape(k)}[/]" for k in self._keys[-20:])
        self.query_one("#keylog", Static).update(
            "Press keys to see what the terminal delivers "
            "(cmd+↓ should show [b]super+down[/]). ctrl+c to quit.\n\n" + log
        )
