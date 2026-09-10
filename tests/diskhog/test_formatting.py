from pathlib import Path

import pytest

from pc_maintenance.diskhog.formatting import human_age, human_size, pct, pretty_path


@pytest.mark.parametrize(
    ("size", "expected"),
    [(0, "0 B"), (1023, "1,023 B"), (1024, "1.0 KB"), (1024**3, "1.0 GB")],
)
def test_human_size(size, expected):
    assert human_size(size) == expected


@pytest.mark.parametrize(
    ("seconds", "expected"),
    [(30, "30s"), (120, "2m"), (7200, "2.0h"), (172800, "2.0d")],
)
def test_human_age(seconds, expected):
    assert human_age(seconds) == expected


def test_pretty_path_only_abbreviates_home(monkeypatch):
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: Path("/home/example")))

    assert pretty_path("/home/example") == "~"
    assert pretty_path("/home/example/projects") == "~/projects"
    assert pretty_path("/home/example-other") == "/home/example-other"
    assert pretty_path("/other") == "/other"


def test_percentage():
    assert pct(1, 4).strip() == "25%"
    assert pct(0, 0).strip() == "0%"
