from unittest.mock import Mock

import pytest

from pc_maintenance.diskhog import operations


@pytest.mark.parametrize("is_directory", [True, False])
def test_finder_opens_directories_and_reveals_files(
    tmp_path, monkeypatch, is_directory
):
    path = tmp_path / "a path with spaces; echo hello"
    if is_directory:
        path.mkdir()
    else:
        path.write_text("example")
    run = Mock()
    monkeypatch.setattr(operations.subprocess, "run", run)

    operations.open_in_finder(str(path))

    command = ["open", str(path)] if is_directory else ["open", "-R", str(path)]
    run.assert_called_once_with(command, check=True, capture_output=True)
