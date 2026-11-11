"""SQLite response cache for Rate My Professors queries.

Layout: ``.state/rmp.sqlite`` (overridable with ``RMP_CACHE_PATH``). The
directory is shared with the planner's other state file, so this module only
ever creates the *directory* and its own database file; it never truncates or
rewrites anything else in ``.state``.
"""

from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import threading
import time
from pathlib import Path
from typing import Any, Callable, Mapping

DEFAULT_TTL_SECONDS = 7 * 24 * 60 * 60
DEFAULT_CACHE_PATH = Path(".state") / "rmp.sqlite"


def default_cache_path() -> Path:
    override = os.environ.get("RMP_CACHE_PATH")
    return Path(override) if override else DEFAULT_CACHE_PATH


def make_key(query: str, operation_name: str, variables: Mapping[str, Any]) -> str:
    """Stable hash of an operation plus its variables."""
    payload = json.dumps(
        {"operation": operation_name, "query": query, "variables": variables},
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


class SqliteCache:
    """Small, thread-safe key/value cache with per-entry TTL.

    Values are stored as JSON so the cache survives process restarts. A
    corrupt/expired entry is treated as a miss instead of raising.
    """

    def __init__(
        self,
        path: str | Path | None = None,
        *,
        ttl: int = DEFAULT_TTL_SECONDS,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self.path = Path(path) if path is not None else default_cache_path()
        self.ttl = ttl
        self._clock = clock
        self._lock = threading.Lock()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(str(self.path), check_same_thread=False)
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA synchronous=NORMAL")
        self._conn.execute(
            """
            CREATE TABLE IF NOT EXISTS rmp_cache (
                key TEXT PRIMARY KEY,
                payload TEXT NOT NULL,
                created_at REAL NOT NULL
            )
            """
        )
        self._conn.commit()

    def get(self, key: str) -> Any | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT payload, created_at FROM rmp_cache WHERE key = ?", (key,)
            ).fetchone()
            if row is None:
                return None
            payload, created_at = row
            if self._clock() - float(created_at) > self.ttl:
                self._conn.execute("DELETE FROM rmp_cache WHERE key = ?", (key,))
                self._conn.commit()
                return None
        try:
            return json.loads(payload)
        except (TypeError, json.JSONDecodeError):
            return None

    def set(self, key: str, value: Any) -> None:
        payload = json.dumps(value, ensure_ascii=False)
        with self._lock:
            self._conn.execute(
                """
                INSERT INTO rmp_cache (key, payload, created_at)
                VALUES (?, ?, ?)
                ON CONFLICT(key) DO UPDATE SET
                    payload = excluded.payload,
                    created_at = excluded.created_at
                """,
                (key, payload, self._clock()),
            )
            self._conn.commit()

    def delete(self, key: str) -> None:
        with self._lock:
            self._conn.execute("DELETE FROM rmp_cache WHERE key = ?", (key,))
            self._conn.commit()

    def clear(self) -> None:
        with self._lock:
            self._conn.execute("DELETE FROM rmp_cache")
            self._conn.commit()

    def close(self) -> None:
        with self._lock:
            self._conn.close()

    def __enter__(self) -> "SqliteCache":
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.close()
