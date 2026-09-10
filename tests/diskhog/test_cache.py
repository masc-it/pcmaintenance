import hashlib
import pickle
from unittest.mock import Mock

import pytest

from pc_maintenance.diskhog import cache


@pytest.fixture(autouse=True)
def cache_directory(tmp_path, monkeypatch):
    directory = tmp_path / "cache"
    monkeypatch.setattr(cache, "CACHE_DIR", directory)
    return directory


def test_round_trip_preserves_tree_and_parent_links(scan_result):
    cache.save_cache(scan_result)
    loaded = cache.load_cache(scan_result.root_path)

    assert loaded is not None
    assert loaded.root_path == scan_result.root_path
    assert loaded.files == scan_result.files
    assert loaded.timestamp == scan_result.timestamp
    assert loaded.root.size == scan_result.root.size
    assert loaded.root.children[0].parent is loaded.root
    assert loaded.root.children[0].path() == scan_result.root.children[0].path()
    assert not list(cache.CACHE_DIR.glob("*.tmp"))


def test_missing_cache():
    assert cache.load_cache("/missing") is None


def test_corrupt_cache_is_ignored(cache_directory):
    cache_directory.mkdir()
    cache._cache_file("/scan").write_bytes(b"not a pickle")

    assert cache.load_cache("/scan") is None


@pytest.mark.parametrize("mismatch", ["version", "root"])
def test_incompatible_cache_is_ignored(scan_result, cache_directory, mismatch):
    cache_directory.mkdir()
    root_path = scan_result.root_path
    version = cache.CACHE_VERSION
    if mismatch == "version":
        version -= 1
    else:
        scan_result.root_path = "/different-root"
    cache._cache_file(root_path).write_bytes(pickle.dumps((version, scan_result)))

    assert cache.load_cache(root_path) is None


def test_legacy_cache_is_never_deserialized(cache_directory, monkeypatch):
    cache_directory.mkdir()
    root_path = "/scan"
    digest = hashlib.sha1(root_path.encode()).hexdigest()[:16]
    legacy = cache_directory / f"scan-{digest}.pkl"
    legacy.write_bytes(b"legacy pickle with old module paths")
    load = Mock()
    monkeypatch.setattr(cache.pickle, "load", load)

    assert cache.load_cache(root_path) is None
    load.assert_not_called()
    assert legacy.exists()
