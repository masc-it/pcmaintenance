import threading

import pytest

from pc_maintenance.diskhog.scanner import Scanner


@pytest.fixture
def scan():
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
