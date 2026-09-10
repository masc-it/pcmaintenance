"""Command-line arguments and headless scan reporting."""

from __future__ import annotations

import argparse
import os
import sys
import threading

from .analysis import analyze
from .formatting import human_size, pretty_path
from .scanner import Node, Scanner, ScanResult


def run_print_mode(opts: argparse.Namespace) -> None:
    done = threading.Event()
    holder: dict[str, ScanResult] = {}

    def on_done(result: ScanResult) -> None:
        holder["result"] = result
        done.set()

    scanner = Scanner(
        opts.path,
        workers=opts.workers,
        min_file_size=opts.min_file_mb * 1024 * 1024,
        on_done=on_done,
    )
    scanner.start()
    while not done.wait(timeout=1.0):
        s = scanner.stats
        print(
            f"\r  scanning… {s.dirs:,} dirs, {s.files:,} files, "
            f"{human_size(s.bytes)}   ",
            end="",
            file=sys.stderr,
            flush=True,
        )
    print(file=sys.stderr)
    result = holder["result"]
    root = result.root
    print(
        f"{opts.path}: {human_size(root.size)} in {result.files:,} files "
        f"({result.duration:.1f}s, {result.errors} errors)\n"
    )

    def print_level(node: Node, indent: int, depth: int) -> None:
        kids = sorted(node.children or [], key=lambda c: c.size, reverse=True)
        for c in kids[: opts.print_top]:
            mark = "/" if c.is_dir else ""
            print(f"{'  ' * indent}{human_size(c.size):>10}  {c.name}{mark}")
            if depth > 1 and c.is_dir and not c.opaque:
                print_level(c, indent + 1, depth - 1)

    print_level(root, 1, opts.print_depth)

    hotspots = analyze(root, top_n=20)
    print("\nHotspots:")
    ranked = sorted(
        hotspots.categories.items(),
        key=lambda kv: sum(n.size for n in kv[1]),
        reverse=True,
    )
    for cat, nodes in ranked:
        total = sum(n.size for n in nodes)
        print(f"  {human_size(total):>10}  {cat} ({len(nodes)} items)")
        for n in sorted(nodes, key=lambda x: x.size, reverse=True)[:5]:
            print(f"      {human_size(n.size):>10}  {pretty_path(n.path())}")

    print("\nLargest files:")
    for size, node in hotspots.top_files[:15]:
        print(f"  {human_size(size):>10}  {pretty_path(node.path())}")


def main() -> None:
    ap = argparse.ArgumentParser(
        prog="diskhog",
        description="Disk usage hotspot analyzer TUI for dev machines.",
    )
    ap.add_argument("path", nargs="?", default="~", help="root to scan (default: ~)")
    ap.add_argument("--workers", type=int, default=8, help="scanner threads")
    ap.add_argument(
        "--min-file-mb",
        type=int,
        default=10,
        help="files smaller than this are aggregated per-directory (default: 10)",
    )
    ap.add_argument(
        "--top-files", type=int, default=300, help="largest-files list size"
    )
    ap.add_argument(
        "--no-cache", action="store_true", help="ignore and don't write cache"
    )
    ap.add_argument(
        "--cache-ttl-hours",
        type=float,
        default=24,
        help="auto-rescan if cache is older than this (default: 24)",
    )
    ap.add_argument(
        "--print",
        dest="print_mode",
        action="store_true",
        help="headless: scan and print a summary instead of the TUI",
    )
    ap.add_argument("--print-depth", type=int, default=2, help="depth for --print")
    ap.add_argument("--print-top", type=int, default=10, help="items/level for --print")
    ap.add_argument(
        "--keys",
        dest="keys_mode",
        action="store_true",
        help="debug: show key names the terminal delivers, then exit",
    )
    opts = ap.parse_args()

    if opts.keys_mode:
        from .tui import KeyDebugApp

        KeyDebugApp().run()
        return

    opts.path = os.path.abspath(os.path.expanduser(opts.path))
    if not os.path.isdir(opts.path):
        ap.error(f"not a directory: {opts.path}")

    if opts.print_mode:
        run_print_mode(opts)
    else:
        from .tui import DiskHogApp

        DiskHogApp(opts).run()
