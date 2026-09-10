import os

from pc_maintenance.diskhog.scanner import Scanner


def allocated(path):
    return path.lstat().st_blocks * 512


def test_nested_files_and_small_file_aggregation(tmp_path, scan):
    small = tmp_path / "small.txt"
    small.write_text("small")
    directory = tmp_path / "nested"
    directory.mkdir()
    large = directory / "large.bin"
    large.write_bytes(b"x" * (1024 * 1024))

    result = scan(tmp_path, min_file_size=64 * 1024)

    assert result.files == 2
    assert result.dirs == 2
    assert result.errors == 0
    assert result.root.file_count == 2
    assert result.root.small_count == 1
    assert result.root.small_size == allocated(small)
    assert result.root.size == sum(map(allocated, [tmp_path, small, directory, large]))
    nested = result.root.children[0]
    assert nested.name == "nested"
    assert nested.children[0].path() == str(large)


def test_opaque_directories_are_sized_without_children(tmp_path, scan):
    modules = tmp_path / "node_modules"
    modules.mkdir()
    package = modules / "package"
    package.mkdir()
    file = package / "index.js"
    file.write_text("export default 1;")

    result = scan(tmp_path, min_file_size=0)

    node = result.root.children[0]
    assert node.opaque
    assert node.children is None
    assert node.file_count == 1
    assert node.size == sum(map(allocated, [modules, package, file]))
    assert result.dirs == 3


def test_hardlinks_are_counted_once_across_opaque_and_regular_dirs(tmp_path, scan):
    file = tmp_path / "original.bin"
    file.write_bytes(b"x" * 8192)
    modules = tmp_path / "node_modules"
    modules.mkdir()
    os.link(file, modules / "linked.bin")

    result = scan(tmp_path, min_file_size=0)

    assert result.files == 1
    assert result.root.file_count == 1
    assert result.root.size == sum(map(allocated, [tmp_path, modules, file]))


def test_symlinked_directories_are_not_followed(tmp_path, scan):
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "private.txt").write_text("must not be scanned")
    root = tmp_path / "root"
    root.mkdir()
    link = root / "link"
    link.symlink_to(outside, target_is_directory=True)

    result = scan(root, min_file_size=0)

    assert result.dirs == 1
    assert result.files == 1
    assert result.root.children == []
    assert result.root.small_count == 1
    assert result.root.size == allocated(root) + allocated(link)


def test_sparse_files_use_allocated_size(tmp_path, scan):
    file = tmp_path / "sparse.bin"
    with file.open("wb") as stream:
        stream.seek(64 * 1024 * 1024)
        stream.write(b"x")

    result = scan(tmp_path, min_file_size=0)

    assert result.root.children[0].size == allocated(file)
    assert result.root.size == allocated(tmp_path) + allocated(file)


def test_unreadable_directory_is_reported(tmp_path, scan, monkeypatch):
    blocked = tmp_path / "blocked"
    blocked.mkdir()
    scandir = os.scandir

    def deny_blocked(path):
        if os.fspath(path) == str(blocked):
            raise PermissionError("test denial")
        return scandir(path)

    monkeypatch.setattr(os, "scandir", deny_blocked)
    result = scan(tmp_path)

    assert result.errors == 1
    assert result.dirs == 2


def test_cancelled_scan_does_not_call_completion(tmp_path):
    from unittest.mock import Mock

    on_done = Mock()
    scanner = Scanner(str(tmp_path), on_done=on_done)
    scanner.cancel()
    scanner._monitor()

    assert scanner.stats.done
    on_done.assert_not_called()
