import argparse
import asyncio
from unittest.mock import Mock

import pytest

from pc_maintenance.diskhog.tui import app as tui
from pc_maintenance.diskhog.tui.panes import BrowsePane, FilesPane
from pc_maintenance.diskhog.tui.widgets import CleanupDialog
from tests.diskhog.test_cleanup import make_env, make_modules, make_old


def options(root_path, *, no_cache):
    return argparse.Namespace(
        path=root_path,
        no_cache=no_cache,
        cache_ttl_hours=24,
        workers=2,
        min_file_mb=0,
        top_files=300,
    )


@pytest.fixture
def tree_result(tmp_path, scan):
    root = tmp_path / "root"
    (root / "big").mkdir(parents=True)
    (root / "big" / "large.bin").write_bytes(b"x" * 400_000)
    (root / "big" / "small.txt").write_text("small")
    (root / "small").mkdir()
    (root / "small" / "note.txt").write_text("note")
    (root / "node_modules" / "pkg").mkdir(parents=True)
    (root / "node_modules" / "pkg" / "index.js").write_text("x" * 50_000)
    return scan(root, min_file_size=0)


def run(app, exercise):
    async def main():
        async with app.run_test(size=(120, 40)) as pilot:
            for _ in range(100):
                if app.view is not None and not app.scanning:
                    break
                await pilot.pause(0.05)
            assert app.view is not None
            await pilot.pause()
            await exercise(pilot)

    asyncio.run(main())


def cached_app(result, monkeypatch):
    monkeypatch.setattr(tui, "load_cache", lambda path: result)
    return tui.DiskHogApp(options(result.root_path, no_cache=False))


def browse_names(app):
    pane = app.query_one(BrowsePane)
    return pane.current.name, [getattr(r, "name", None) for r in pane.rows]


def test_cached_scan_views_and_reveal(tree_result, monkeypatch):
    app = cached_app(tree_result, monkeypatch)
    open_in_finder = Mock()
    monkeypatch.setattr(tui, "open_in_finder", open_in_finder)

    async def exercise(pilot):
        assert app.result_is_cached
        assert app._scanner is None
        assert browse_names(app)[1] == ["big", "node_modules", "small"]
        await pilot.press("2")
        assert app.tab == "hotspots"
        await pilot.press("3")
        assert app.tab == "files"
        files = app.query_one(FilesPane).files
        assert files[0].name == "large.bin"
        await pilot.press("o")
        open_in_finder.assert_called_once_with(files[0].path())
        notify = Mock()
        monkeypatch.setattr(app, "notify", notify)
        await pilot.press("d")
        notify.assert_not_called()
        await pilot.press("1")
        assert app.tab == "browse"

    run(app, exercise)


def test_no_permanent_delete_binding_or_action():
    # The only filesystem change is the confirmed move of stale envs to Trash.
    assert all(binding.key != "d" for binding in tui.DiskHogApp.BINDINGS)
    assert not hasattr(tui.DiskHogApp, "action_delete_selected")


def test_fresh_scan_populates_ui_without_writing_cache(tree_result, monkeypatch):
    save = Mock()
    monkeypatch.setattr(tui, "save_cache", save)
    app = tui.DiskHogApp(options(tree_result.root_path, no_cache=True))

    async def exercise(pilot):
        assert not app.result_is_cached
        assert app.result.files == 4
        assert len(app.query_one(FilesPane).files) == 3  # opaque contents excluded
        save.assert_not_called()

    run(app, exercise)


def test_open_and_back_restore_the_cursor(tree_result, monkeypatch):
    app = cached_app(tree_result, monkeypatch)

    async def exercise(pilot):
        pane = app.query_one(BrowsePane)
        await pilot.press("enter")
        assert browse_names(app) == ("big", ["large.bin", "small.txt"])
        await pilot.press("right")  # files cannot be opened
        assert pane.current.name == "big"
        await pilot.press("left")
        assert pane.current is tree_result.root
        assert pane.selected().name == "big"
        await pilot.press("down", "right")  # node_modules is opaque
        assert pane.current is tree_result.root
        assert pane.selection().note == "sized, not browsable"

    run(app, exercise)


def test_files_entry_is_shown_in_browse(tree_result, monkeypatch):
    app = cached_app(tree_result, monkeypatch)

    async def exercise(pilot):
        await pilot.press("3", "enter")
        pane = app.query_one(BrowsePane)
        assert app.tab == "browse"
        assert pane.current.name == "big"
        assert pane.selected().name == "large.bin"

    run(app, exercise)


def test_filter_narrows_rows_and_escape_clears(tree_result, monkeypatch):
    app = cached_app(tree_result, monkeypatch)

    async def exercise(pilot):
        await pilot.press("slash", "s", "m")
        assert browse_names(app)[1] == ["small"]
        await pilot.press("enter")  # keep the filter, return to the table
        assert browse_names(app)[1] == ["small"]
        await pilot.press("escape")
        assert browse_names(app)[1] == ["big", "node_modules", "small"]

    run(app, exercise)


def test_rescan_keeps_location_and_selection(tree_result, scan, monkeypatch):
    app = cached_app(tree_result, monkeypatch)
    monkeypatch.setattr(tui, "save_cache", Mock())

    async def exercise(pilot):
        await pilot.press("enter", "down")
        fresh = scan(tree_result.root_path, min_file_size=0)
        app._on_scan_done(tui.build_view(fresh, top_files=300, min_file_size=0))
        await pilot.pause()
        pane = app.query_one(BrowsePane)
        assert pane.current is not tree_result.root.children[0]  # new tree
        assert browse_names(app)[0] == "big"
        assert pane.selected().name == "small.txt"

    run(app, exercise)


@pytest.fixture
def stale_result(tmp_path, scan):
    root = tmp_path / "root"
    make_env(root / "a" / ".venv")
    make_modules(root / "b" / "node_modules")
    (root / "b" / "node_modules" / "pkg" / "big.js").write_text("x" * 200_000)
    make_old(root)
    return scan(root, min_file_size=0)


def test_cleanup_previews_and_trashes_only_confirmed(stale_result, monkeypatch):
    app = cached_app(stale_result, monkeypatch)
    trash = Mock()
    monkeypatch.setattr(tui, "move_to_trash", trash)
    monkeypatch.setattr(tui, "save_cache", Mock())
    total = stale_result.root.size

    async def exercise(pilot):
        await pilot.press("x")  # only on the Hotspots view
        assert not isinstance(app.screen, CleanupDialog)
        await pilot.press("2", "x")
        await pilot.pause()
        dialog = app.screen
        assert isinstance(dialog, CleanupDialog)
        assert [c.kind for c in dialog.candidates] == ["node_modules", "Python env"]
        await pilot.press("space")  # deselect the largest (node_modules)
        await pilot.press("enter")
        for _ in range(50):
            if trash.called and not app._cleaning:
                break
            await pilot.pause(0.05)
        trash.assert_called_once_with(dialog.candidates[1].path)
        removed = dialog.candidates[1]
        assert app.result.root.size == total - removed.size
        assert removed.node not in removed.node.parent.children

    run(app, exercise)


def test_cleanup_cancel_and_empty_state(tree_result, stale_result, monkeypatch):
    app = cached_app(stale_result, monkeypatch)
    trash = Mock()
    monkeypatch.setattr(tui, "move_to_trash", trash)

    async def exercise(pilot):
        await pilot.press("2", "x")
        await pilot.pause()
        await pilot.press("escape")
        assert not isinstance(app.screen, CleanupDialog)
        app.view.result.root.children.clear()  # nothing left to clean
        await pilot.press("x")
        await pilot.pause()
        assert app.screen.candidates == []
        await pilot.press("enter")
        assert not isinstance(app.screen, CleanupDialog)
        trash.assert_not_called()

    run(app, exercise)
