import sys
import threading

import pytest

from pc_maintenance.diskhog import listing
from pc_maintenance.diskhog.scanner import Scanner

BACKENDS = ["portable"] + (["bulk"] if sys.platform == "darwin" else [])


@pytest.fixture(params=BACKENDS)
def list_dir_backend(request, monkeypatch):
    backend = (
        listing.list_dir_portable
        if request.param == "portable"
        else listing._BulkLister()
    )
    monkeypatch.setattr(listing, "list_dir", backend)
    return backend


@pytest.fixture
def scan(list_dir_backend):
    def run(path, **options):
        results = []
        done = threading.Event()

        def on_done(result):
            results.append(result)
            done.set()

        scanner = Scanner(str(path), on_done=on_done, **options)
        scanner.start()
        assert done.wait(timeout=10), "Scanner did not finish"
        assert scanner.stats.done
        return results[0]

    return run


@pytest.fixture
def scan_result(tmp_path, scan):
    root = tmp_path / "files"
    root.mkdir()
    (root / "example.txt").write_text("example\n")
    return scan(root, min_file_size=0)
