import argparse
import asyncio
from unittest.mock import Mock

from textual.widgets import DataTable, TabbedContent, Tree

from pc_maintenance.diskhog import tui


def options(root_path, *, no_cache):
    return argparse.Namespace(
        path=root_path,
        no_cache=no_cache,
        cache_ttl_hours=24,
        workers=2,
        min_file_mb=0,
        top_files=300,
    )


def test_cached_scan_tabs_and_actions(scan_result, monkeypatch):
    monkeypatch.setattr(tui, "load_cache", lambda path: scan_result)
    app = tui.DiskHogApp(options(scan_result.root_path, no_cache=False))
    open_in_finder = Mock()
    monkeypatch.setattr(tui, "open_in_finder", open_in_finder)

    async def exercise():
        async with app.run_test(size=(120, 40)) as pilot:
            assert app.result_is_cached
            assert app._scanner is None
            assert len(app.query_one("#dirtree", Tree).root.children) == 1
            await pilot.press("2")
            assert app.query_one(TabbedContent).active == "tab-hotspots"
            await pilot.press("3")
            assert app.query_one(TabbedContent).active == "tab-files"
            assert app.query_one(DataTable).row_count == 1
            await pilot.press("o")
            selected = scan_result.root.children[0].path()
            open_in_finder.assert_called_once_with(selected)
            notify = Mock()
            monkeypatch.setattr(app, "notify", notify)
            await pilot.press("d")
            notify.assert_not_called()
            await pilot.press("1")
            assert app.query_one(TabbedContent).active == "tab-tree"

    asyncio.run(exercise())


def test_viewer_has_no_delete_binding_or_action():
    assert all(binding.key != "d" for binding in tui.DiskHogApp.BINDINGS)
    assert not hasattr(tui.DiskHogApp, "action_delete_selected")


def test_fresh_scan_populates_ui_without_writing_cache(scan_result, monkeypatch):
    save = Mock()
    monkeypatch.setattr(tui, "save_cache", save)
    app = tui.DiskHogApp(options(scan_result.root_path, no_cache=True))

    async def exercise():
        async with app.run_test(size=(120, 40)) as pilot:
            for _ in range(100):
                if app.result is not None:
                    break
                await pilot.pause(0.05)
            assert app.result is not None
            assert app.result.files == 1
            assert not app.result_is_cached
            assert app.query_one(DataTable).row_count == 1
            save.assert_not_called()

    asyncio.run(exercise())
