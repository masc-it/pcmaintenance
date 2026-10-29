from pathlib import Path

import pytest

from pc_maintenance.diskhog.formatting import (
    human_age,
    human_size,
    pct,
    pretty_path,
    share_bar,
    truncate_left,
)


@pytest.mark.parametrize(
    ("size", "expected"),
    [(0, "0 B"), (999, "999 B"), (1000, "1.0 KB"), (1_500_000_000, "1.5 GB")],
)
def test_human_size(size, expected):
    assert human_size(size) == expected


@pytest.mark.parametrize(
    ("seconds", "expected"),
    [
        (30, "30s"),
        (120, "2m"),
        (7200, "2h"),
        (172800, "2d"),
        (90 * 86400, "3mo"),
        (800 * 86400, "2y"),
    ],
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


def test_share_bar_has_fixed_width_and_eighth_resolution():
    assert share_bar(0, 4) == "    "
    assert share_bar(1, 4) == "████"
    assert share_bar(0.5, 4) == "██  "
    assert share_bar(1 / 32, 4) == "▏   "


def test_truncate_left_keeps_the_end():
    assert truncate_left("~/a/b/c", 10) == "~/a/b/c"
    assert truncate_left("~/projects/foo/bar", 8) == "…foo/bar"
