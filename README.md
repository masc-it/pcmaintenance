# pc_maintenance

Utilities for keeping a dev machine healthy.

## diskhog — disk usage hotspot TUI

A disk-usage viewer for development machines. Its only change to the
filesystem is an explicit, confirmed move of stale Python envs and
`node_modules` to the Trash.

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
diskhog-tui                 # scan ~ (default)
diskhog-tui ~/projects
diskhog-tui --print         # headless summary to stdout
diskhog-tui --no-cache      # ignore/skip the startup cache
```

The top line shows the three views, scan status, and free disk space. The
line above the footer shows the full path of the highlighted item, and the
footer lists the keys that apply to the current view.

| Key | Action |
| --- | --- |
| `1` `2` `3` | Browse, Hotspots, Files |
| `→` `↵` / `←` | open a folder / go back (Hotspots: move between lists) |
| `↵` | in Hotspots and Files, show the item in Browse |
| `/`, `esc` | filter the current list, clear the filter |
| `s` | cycle the sort (or click a column title) |
| `o`, `c` | reveal in Finder, copy the path |
| `x` | in Hotspots, move stale envs to the Trash (see below) |
| `r`, `e` | rescan, list unreadable paths |
| `?`, `q` | help, quit |

### What it does

- **Browse** — one folder at a time, sorted by size, with share of the
  parent, item count, and last modification. Files under `--min-file-mb`
  (default 10) are combined into one row per folder. Folders marked `⊘`
  (`node_modules`, `.venv`, `.git`, …) are sized but not browsable.
- **Hotspots** — known space hogs by category: `node_modules`, `.venv`,
  `__pycache__`/tool caches, `.git`, `data/` & `data.tmp/`, HuggingFace
  models, `.gguf` / `.safetensors` files, ollama, `~/.cache` &
  `~/Library/Caches`, docker, toolchains, Trash. Sort by `MODIFIED` to find
  stale ones. The total counts nested instances once.
- **Files** — the largest files by real disk usage.

### Notes

- Measures real disk usage (allocated blocks), counts hardlinks once, skips
  other mount points, never follows symlinks. Sizes use decimal units, as in
  Finder.
- On macOS the scanner lists directories with `getattrlistbulk(2)`, which
  scanned a 4.7M-file home directory about 2.5× faster than `scandir` plus
  `lstat` (38s vs 93s). Other systems use the portable path.
- Unreadable folders are marked in Browse; `e` lists them. If access was
  denied, give the terminal Full Disk Access and rescan.
- Cache: the last scan shows immediately with its age; a fresh scan starts
  automatically if it is older than `--cache-ttl-hours` (24h). While a
  rescan runs the old data stays visible, dimmed, and the views keep their
  folder, selection, sort, and filter when it finishes. Stored in
  `~/.cache/diskhog/`. The cache uses pickle: do not load caches from
  untrusted sources or share cache files, which contain private paths.
- Nothing is ever deleted. The app writes its own scan cache unless
  `--no-cache` is used, and moves folders to the Trash only as described
  below.

### Moving stale environments to the Trash

`x` in Hotspots lists every Python env and `node_modules` whose contents
have not changed for 6 months, largest first. Nothing happens until you
confirm; `space` deselects a row and `a` toggles all.

- **Python envs** are folders with `pyvenv.cfg` and only the standard env
  layout (`bin`, `lib`, `include`, …), whatever their name. A folder that
  also holds other files, such as a project where `python -m venv .` was
  run, is never offered.
- **`node_modules`**: only the outermost one per project.
- Right before moving, each folder is checked again and skipped if it is
  gone, no longer an env, or changed since the scan.
- Folders go to the macOS Trash (`/usr/bin/trash`, or Finder on macOS
  older than 15), so they can be restored. The space is freed when the
  Trash is emptied. Rebuild an env with `uv sync` or `npm install`.

## Project structure

```text
apps/diskhog.py                    # uv launcher for this checkout
src/pc_maintenance/diskhog/
    __main__.py                   # python -m entry point
    listing.py                    # directory listing (getattrlistbulk / scandir)
    scanner.py                    # scan data model and parallel traversal
    analysis.py                   # hotspot categories and largest files
    cache.py                      # local scan persistence
    cleanup.py                    # stale env detection and moving to Trash
    operations.py                 # Finder, clipboard, and Trash integration
    formatting.py                 # sizes, ages, bars, and paths
    cli.py                        # arguments and headless output
    tui/
        app.py                    # frame, scan coordination, and actions
        panes.py                  # Browse, Hotspots, and Files views
        rows.py                   # sorting, filtering, and locations
        widgets.py                # tables, lines, and dialogs
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
