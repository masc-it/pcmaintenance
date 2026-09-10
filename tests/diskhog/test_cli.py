import subprocess
import sys
from pathlib import Path

import pytest

from pc_maintenance.diskhog import cli


@pytest.mark.parametrize("entry_point", ["module", "console"])
def test_entry_points_from_another_directory(tmp_path, entry_point):
    if entry_point == "module":
        command = [sys.executable, "-m", "pc_maintenance.diskhog"]
    else:
        command = [str(Path(sys.executable).with_name("diskhog"))]
    (tmp_path / "example.txt").write_text("example")

    result = subprocess.run(
        [*command, str(tmp_path), "--print", "--min-file-mb", "0"],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        timeout=20,
        check=True,
    )

    assert "1 files" in result.stdout
    assert "example.txt" in result.stdout
    assert "Hotspots:" in result.stdout
    assert "Largest files:" in result.stdout


def test_missing_directory_is_rejected(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(sys, "argv", ["diskhog", str(tmp_path / "missing"), "--print"])

    with pytest.raises(SystemExit) as exc:
        cli.main()

    assert exc.value.code == 2
    assert "not a directory:" in capsys.readouterr().err


def test_headless_import_does_not_load_textual(tmp_path):
    result = subprocess.run(
        [
            sys.executable,
            "-c",
            "import sys; from pc_maintenance.diskhog import cli; "
            "assert 'textual' not in sys.modules; "
            "assert 'rich' not in sys.modules",
        ],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        timeout=20,
    )

    assert result.returncode == 0, result.stderr
