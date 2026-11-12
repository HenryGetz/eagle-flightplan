"""Offline tests for the SQLite RMP cache."""

from __future__ import annotations

import pathlib
import sys

ROOT = pathlib.Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from eagle_flightplan.rmp.cache import SqliteCache, make_key  # noqa: E402


def test_set_get_roundtrip(tmp_path):
    cache = SqliteCache(tmp_path / "rmp.sqlite")
    cache.set("key", {"answer": [1, 2, 3]})
    assert cache.get("key") == {"answer": [1, 2, 3]}
    assert cache.get("missing") is None
    cache.close()


def test_ttl_expiry(tmp_path):
    now = [1000.0]
    cache = SqliteCache(tmp_path / "rmp.sqlite", ttl=60, clock=lambda: now[0])
    cache.set("key", "value")
    assert cache.get("key") == "value"

    now[0] += 61
    assert cache.get("key") is None
    # Expired entry was removed, not merely hidden.
    now[0] -= 61
    assert cache.get("key") is None
    cache.close()


def test_existing_state_directory_is_preserved(tmp_path):
    state = tmp_path / ".state"
    state.mkdir()
    sentinel = state / "unw_state.json"
    sentinel.write_text('{"other": "worker"}')

    cache = SqliteCache(state / "rmp.sqlite")
    cache.set("k", {"v": 1})
    assert cache.get("k") == {"v": 1}
    cache.close()

    assert sentinel.read_text() == '{"other": "worker"}'
    assert (state / "rmp.sqlite").exists()


def test_wal_mode_is_enabled(tmp_path):
    cache = SqliteCache(tmp_path / "rmp.sqlite")
    mode = cache._conn.execute("PRAGMA journal_mode").fetchone()[0]
    assert mode.lower() == "wal"
    cache.close()


def test_key_is_order_independent():
    left = make_key("query", "Op", {"b": 2, "a": {"y": 1, "x": 2}})
    right = make_key("query", "Op", {"a": {"x": 2, "y": 1}, "b": 2})
    assert left == right
    assert left != make_key("query", "OtherOp", {"a": 1, "b": 2})
