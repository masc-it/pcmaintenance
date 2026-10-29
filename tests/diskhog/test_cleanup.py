import os
import subprocess
import time

import pytest

from pc_maintenance.diskhog import operations
from pc_maintenance.diskhog.cleanup import (
    NODE_MODULES,
    PYTHON_ENV,
    detach,
    recheck,
    run_cleanup,
    stale_environments,
)

OLD = time.time() - 400 * 86400


def make_old(path):
    """Backdate a tree bottom-up so parent mtimes are not bumped again."""
    for dirpath, dirnames, filenames in os.walk(path, topdown=False):
        for name in filenames:
            os.utime(os.path.join(dirpath, name), (OLD, OLD))
        os.utime(dirpath, (OLD, OLD))


def make_env(path):
    (path / "lib").mkdir(parents=True)
    (path / "pyvenv.cfg").write_text("home = /usr/bin\n")
    (path / "lib" / "site.py").write_text("x" * 10_000)
    return path


def make_modules(path):
    (path / "pkg" / "node_modules" / "nested").mkdir(parents=True)
    (path / "pkg" / "index.js").write_text("x" * 10_000)
    return path


@pytest.fixture
def workspace(tmp_path):
    old = tmp_path / "old"
    make_env(old / ".venv")
    make_env(old / "env-custom")
    make_modules(old / "node_modules")
    (old / "venv").mkdir()  # named like an env, but no pyvenv.cfg
    (old / "venv" / "notes.txt").write_text("not an env")
    (old / ".git").mkdir()
    (old / ".git" / "HEAD").write_text("ref")
    project_env = make_env(old / "project-is-env")  # python -m venv . in a project
    (project_env / "main.py").write_text("print('my code')")
    named_env = make_env(old / "app" / ".venv")
    (named_env / "notes.md").write_text("user file inside .venv")
    make_old(old)
    recent = tmp_path / "recent"
    make_env(recent / ".venv")
    make_modules(recent / "node_modules")
    return tmp_path


def by_path(candidates):
    return {
        os.path.relpath(c.path, c.node.parent.parent.path()): c.kind for c in candidates
    }


def test_scanner_marks_python_envs_by_marker_file(workspace, scan):
    result = scan(workspace)
    old = next(c for c in result.root.children if c.name == "old")
    kids = {c.name: c for c in old.children}

    assert kids[".venv"].is_venv
    assert kids["env-custom"].is_venv
    assert kids["env-custom"].opaque  # sized like .venv, not browsable
    assert kids["env-custom"].children is None
    assert kids["env-custom"].file_count == 2
    assert not kids["venv"].is_venv
    assert not kids["node_modules"].is_venv
    assert not kids["project-is-env"].is_venv
    assert not kids["project-is-env"].opaque  # still browsable: holds user files
    app_venv = next(c for c in kids["app"].children if c.name == ".venv")
    assert app_venv.opaque and not app_venv.is_venv


def test_only_old_envs_and_node_modules_are_candidates(workspace, scan):
    result = scan(workspace)

    candidates = stale_environments(result.root, now=time.time())

    assert by_path(candidates) == {
        "old/.venv": PYTHON_ENV,
        "old/env-custom": PYTHON_ENV,
        "old/node_modules": NODE_MODULES,
    }
    assert [c.size for c in candidates] == sorted(
        (c.size for c in candidates), reverse=True
    )


def test_recheck_skips_folders_that_changed_or_vanished(workspace, scan):
    result = scan(workspace)
    candidates = {c.path: c for c in stale_environments(result.root, now=time.time())}
    env = candidates[str(workspace / "old" / ".venv")]
    modules = candidates[str(workspace / "old" / "node_modules")]
    custom = candidates[str(workspace / "old" / "env-custom")]

    assert recheck(env, scanned_at=result.timestamp) is None
    (workspace / "old" / "node_modules" / "new-pkg").mkdir()
    assert recheck(modules, scanned_at=result.timestamp) == "changed since the scan"
    (workspace / "old" / "env-custom" / "main.py").write_text("new user file")
    assert recheck(custom, scanned_at=result.timestamp) == "no longer a Python env"
    os.rename(workspace / "old" / ".venv", workspace / "old" / "moved")
    assert recheck(env, scanned_at=result.timestamp) == "no longer exists"


def test_run_cleanup_reports_moved_skipped_and_failed(workspace, scan):
    result = scan(workspace)
    candidates = stale_environments(result.root, now=time.time())
    failing = str(workspace / "old" / "env-custom")
    (workspace / "old" / "node_modules" / "new-pkg").mkdir()
    trashed = []

    def trash(path):
        if path == failing:
            raise subprocess.CalledProcessError(5, "trash", stderr=b"oops\nDenied")
        trashed.append(path)

    report = run_cleanup(candidates, scanned_at=result.timestamp, trash=trash)

    assert trashed == [str(workspace / "old" / ".venv")]
    assert [c.path for c in report.moved] == trashed
    assert [(c.kind, r) for c, r in report.skipped] == [
        (NODE_MODULES, "changed since the scan")
    ]
    assert [(c.path, r) for c, r in report.failed] == [(failing, "Denied")]


def test_detach_removes_node_and_updates_ancestors(workspace, scan):
    result = scan(workspace)
    root_size, root_files = result.root.size, result.root.file_count
    env = next(
        c
        for c in stale_environments(result.root, now=time.time())
        if c.kind == PYTHON_ENV
    )
    parent = env.node.parent

    removed = detach(env.node)

    assert removed == env.node.file_count
    assert env.node not in parent.children
    assert result.root.size == root_size - env.size
    assert result.root.file_count == root_files - removed


@pytest.mark.parametrize("has_tool", [True, False])
def test_move_to_trash_uses_trash_tool_or_finder(monkeypatch, has_tool):
    calls = []
    monkeypatch.setattr(
        operations.os.path, "exists", lambda p: has_tool and p == operations.TRASH_TOOL
    )
    monkeypatch.setattr(
        operations.subprocess, "run", lambda cmd, **kw: calls.append((cmd, kw))
    )

    operations.move_to_trash("/tmp/a path; rm -rf ~")

    cmd, kwargs = calls[0]
    assert cmd[-1] == "/tmp/a path; rm -rf ~"  # passed as an argument, not code
    assert cmd[0] == (operations.TRASH_TOOL if has_tool else "osascript")
    assert kwargs == {"check": True, "capture_output": True}
