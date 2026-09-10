#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.12"
# dependencies = ["pc-maintenance"]
#
# [tool.uv.sources]
# pc-maintenance = { path = "..", editable = true }
# ///
"""Run diskhog from this checkout using uv-managed dependencies."""

from pc_maintenance.diskhog.cli import main

if __name__ == "__main__":
    main()
