"""Best-effort persistence of local scan results."""

from __future__ import annotations

import hashlib
import pickle
import sys
from pathlib import Path

from .scanner import ScanResult

CACHE_VERSION = 2


CACHE_DIR = Path.home() / ".cache" / "diskhog"


def _cache_file(root_path: str) -> Path:
    h = hashlib.sha1(root_path.encode()).hexdigest()[:16]
    # A new namespace skips legacy pickles before deserializing moved classes.
    return CACHE_DIR / f"scan-v{CACHE_VERSION}-{h}.pkl"


def load_cache(root_path: str) -> ScanResult | None:
    try:
        with open(_cache_file(root_path), "rb") as f:
            version, result = pickle.load(f)
        if version != CACHE_VERSION or result.root_path != root_path:
            return None
        return result
    except Exception:
        return None


def save_cache(result: ScanResult) -> None:
    try:
        CACHE_DIR.mkdir(parents=True, exist_ok=True)
        sys.setrecursionlimit(max(sys.getrecursionlimit(), 100_000))
        tmp = _cache_file(result.root_path).with_suffix(".tmp")
        with open(tmp, "wb") as f:
            pickle.dump((CACHE_VERSION, result), f, protocol=pickle.HIGHEST_PROTOCOL)
        tmp.replace(_cache_file(result.root_path))
    except Exception:
        pass  # cache is best-effort
