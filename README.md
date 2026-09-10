# pc_maintenance

Utilities for keeping a dev machine healthy.

## diskhog — disk usage hotspot TUI

A read-only disk-usage viewer for development machines.

The PEP 723 launcher (`apps/diskhog.py`) uses `uv` to install the local
package and its dependencies. Keep the launcher inside the checkout; it
can be invoked from any working directory. Requires Python 3.12+ and `uv`.
Finder integration requires macOS.

Add to `~/.zshrc`, replacing `/path/to/pc_maintenance` with your checkout path:

```sh
alias diskhog-tui='uv run --script /path/to/pc_maintenance/apps/diskhog.py'
```

From the repository root, you can also use:

```sh
uv run diskhog --help
uv run python -m pc_maintenance.diskhog --help
```

To install the command for use outside the checkout:

```sh
uv tool install .
diskhog --help
```

### Usage

```sh
diskhog-tui                 # scan ~ (default), TUI
diskhog-tui ~/projects
diskhog-tui --print         # headless summary to stdout
diskhog-tui --no-cache      # ignore/skip the startup cache
```

Keys: `1/2/3` switch tabs (Tree / Hotspots / Largest files), `r` rescan,
`o` open selected item in Finder (files are revealed/selected),
`option+↓` open/enter the selected dir, `option+↑` back out to its parent,
`q` quit.

### What it does

- **Tree tab** — directory tree sorted by size, with per-entry share bars.
  Files under `--min-file-mb` (default 10) are aggregated per directory.
- **Hotspots tab** — disk usage grouped by category across the whole tree:
  `node_modules`, `.venv`, `__pycache__`/tool caches, `.git`, `data/` &
  `data.tmp/`, HuggingFace hub models, llama.cpp `.gguf` / `.safetensors`
  files, ollama, `~/.cache` & `~/Library/Caches`, docker, toolchains, Trash.
- **Largest files tab** — top files by real disk usage.

### Notes

- Measures real disk usage (`st_blocks`), counts hardlinks once, skips
  other mount points, never follows symlinks. Opaque dirs (`node_modules`,
  `.venv`, `.git`, …) are sized but not browsable — by design.
- Cache: last scan shows instantly on startup with an age badge; a fresh
  scan auto-starts if older than `--cache-ttl-hours` (24h). The cache is
  never trusted as current — `r` always rescans. Stored in `~/.cache/diskhog/`.
  Caches from the original standalone script are ignored after the module
  migration; the first run rescans. Old cache files are left untouched.
  The cache still uses pickle: do not load caches from untrusted sources or
  share cache files, which contain private filesystem paths.
- Viewer only: scanned files are never modified or deleted. The app writes
  its own scan cache unless `--no-cache` is used.

## Project structure

```text
apps/diskhog.py                    # uv launcher for this checkout
src/pc_maintenance/diskhog/
    __main__.py                   # python -m entry point
    scanner.py                    # scan data model and filesystem traversal
    analysis.py                   # hotspot categories and largest files
    cache.py                      # local scan persistence
    operations.py                 # open directories or reveal files in Finder
    formatting.py                 # display sizes, ages, percentages, and paths
    tui.py                        # Textual interface and scan coordination
    cli.py                        # arguments and headless output
tests/diskhog/                     # regression tests
```

The scanner, analysis, cache, and headless CLI do not import Textual.

## Development

```sh
uv run pytest
uv run ruff check apps src tests
uv run ruff format --check apps src tests
uv build
```

## License

Licensed under the [Apache License 2.0](LICENSE).
