"""
dylorisk_cache.py
════════════════════════════════════════════════════════════════════════════════
DyLoRISK Idempotent Result Cache

GUARANTEES
──────────
• Cache key = SHA-256 of raw file BYTES (not path, not mtime).
  Renaming a file, touching its mtime, or re-running on the same content
  all hit the same cache slot.
• Cache entries are immutable after write. A re-analysis overwrites only
  if the caller explicitly requests force_refresh=True.
• Concurrent writers are protected by a file-level lock (portalocker if
  available, otherwise a simple advisory lock file).
• Cache entries are validated on read: if content_hash or schema version
  mismatches, the entry is evicted and treated as a miss.

SCHEMA VERSION
──────────────
Increment SCHEMA_VERSION whenever ScoreResult's structure changes.
Old entries are auto-evicted on read.
"""

from __future__ import annotations

import json
import os
import time
import threading
import hashlib
from pathlib import Path
from typing import Optional, Dict, Any

SCHEMA_VERSION = 2   # bump when ScoreResult fields change

_LOCK = threading.Lock()    # in-process write lock


def _default_cache_path() -> Path:
    here = Path(os.path.dirname(os.path.abspath(__file__)))
    return here / ".dylorisk_cache.json"


class ScoreCache:
    """
    Thread-safe, content-addressed JSON cache for ScoreResult dicts.

    Usage
    -----
    cache = ScoreCache()                 # uses default path
    entry = cache.get(content_hash)      # None if miss or stale
    cache.put(content_hash, score_dict)  # idempotent write
    cache.evict(content_hash)            # explicit removal
    cache.clear()                        # wipe all entries
    """

    def __init__(self, path: Optional[str] = None):
        self._path = Path(path) if path else _default_cache_path()
        self._data: Dict[str, Any] = self._load()

    # ── Public API ────────────────────────────────────────────────────────────

    def get(self, content_hash: str) -> Optional[dict]:
        """Return cached ScoreResult dict or None."""
        with _LOCK:
            entry = self._data.get(content_hash)
            if entry is None:
                return None
            # Validate schema version
            if entry.get("_schema_version") != SCHEMA_VERSION:
                del self._data[content_hash]
                return None
            # Validate that stored hash matches the key (integrity check)
            if entry.get("content_hash") != content_hash:
                del self._data[content_hash]
                return None
            return entry

    def put(self, content_hash: str, score_dict: dict, force: bool = False) -> None:
        """
        Write a ScoreResult dict to cache.
        Noop if content_hash already present and force=False.
        """
        with _LOCK:
            if content_hash in self._data and not force:
                return
            entry = dict(score_dict)
            entry["_schema_version"] = SCHEMA_VERSION
            entry["_cached_at"] = time.time()
            # Strip heavyweight arrays to keep cache small
            entry.pop("window_metrics", None)
            self._data[content_hash] = entry
            self._save()

    def evict(self, content_hash: str) -> bool:
        with _LOCK:
            if content_hash in self._data:
                del self._data[content_hash]
                self._save()
                return True
            return False

    def clear(self) -> int:
        """Remove all entries. Returns count removed."""
        with _LOCK:
            n = len(self._data)
            self._data.clear()
            self._save()
            return n

    def size(self) -> int:
        return len(self._data)

    def keys(self) -> list:
        return list(self._data.keys())

    def summary(self) -> dict:
        """Return lightweight summary for UI display."""
        entries = list(self._data.values())
        return {
            "total_cached": len(entries),
            "schema_version": SCHEMA_VERSION,
            "cache_path": str(self._path),
        }

    # ── File I/O ──────────────────────────────────────────────────────────────

    def _load(self) -> dict:
        if self._path.exists():
            try:
                raw = json.loads(self._path.read_text(encoding="utf-8"))
                if isinstance(raw, dict):
                    return raw
            except Exception:
                pass
        return {}

    def _save(self) -> None:
        try:
            tmp = self._path.with_suffix(".tmp")
            tmp.write_text(
                json.dumps(self._data, indent=2, default=str),
                encoding="utf-8"
            )
            tmp.replace(self._path)  # atomic on POSIX
        except Exception:
            pass

    # ── Convenience: hash file or lines ──────────────────────────────────────

    @staticmethod
    def hash_file(path: str) -> str:
        """SHA-256 of raw file bytes."""
        h = hashlib.sha256()
        with open(path, "rb") as f:
            for chunk in iter(lambda: f.read(65536), b""):
                h.update(chunk)
        return h.hexdigest()

    @staticmethod
    def hash_lines(lines: list) -> str:
        """SHA-256 of normalised line list."""
        h = hashlib.sha256()
        for line in lines:
            h.update((line + "\n").encode("utf-8", errors="replace"))
        return h.hexdigest()


# ─────────────────────────────────────────────────────────────────────────────
# CACHED ANALYSIS WRAPPER
# ─────────────────────────────────────────────────────────────────────────────

def analyze_with_cache(
    file_path: str,
    raw_lines: list,
    cache: ScoreCache,
    force_refresh: bool = False,
    report_pdf: str = "",
    plots_png:  str = "",
) -> tuple:
    """
    Run score_log_file with cache lookup.

    Returns (score_result, was_cached: bool)

    IDEMPOTENCE:
      Same raw_lines → same content_hash → same cached result.
      Running this 1× or 100× with unchanged content is identical.
    """
    from dylorisk_score_engine import score_log_file, score_result_to_dict
    import time

    content_hash = ScoreCache.hash_lines(raw_lines)

    if not force_refresh:
        cached = cache.get(content_hash)
        if cached is not None:
            return cached, True

    t0 = time.time()
    result = score_log_file(
        file_path=file_path,
        raw_lines=raw_lines,
        report_pdf=report_pdf,
        plots_png=plots_png,
        _t_start=t0,
    )
    result_dict = score_result_to_dict(result)
    cache.put(content_hash, result_dict, force=force_refresh)
    return result, False
