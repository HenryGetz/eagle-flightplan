"""RatingProvider cache honesty: real TTL, no negative caching, no legacy poisoning."""

from __future__ import annotations

import json

from flightplan.planner import RatingInfo, RatingProvider


def test_ttl_expires_and_refetches(tmp_path):
    now = [1_000_000.0]
    calls = {"n": 0}

    def lookup(name: str) -> RatingInfo:
        calls["n"] += 1
        return RatingInfo(name=name, rating=4.0 + calls["n"], num_ratings=10, source="rmp")

    provider = RatingProvider(
        path=tmp_path / "ratings.json",
        ttl_seconds=10,
        lookup=lookup,
        clock=lambda: now[0],
    )
    assert provider.get("Smith, James").rating == 5.0
    assert provider.get("Smith, James").rating == 5.0  # fresh cache
    assert calls["n"] == 1
    now[0] += 11
    assert provider.get("Smith, James").rating == 6.0  # expired
    assert calls["n"] == 2
    assert not (tmp_path / "ratings.json.tmp").exists()


def test_failures_are_never_cached(tmp_path):
    calls = {"n": 0}

    def lookup(name: str) -> RatingInfo:
        calls["n"] += 1
        return RatingInfo(name=name, note="RMP unavailable: timeout")

    path = tmp_path / "ratings.json"
    provider = RatingProvider(path=path, lookup=lookup)
    assert provider.get("Smith, James").note.startswith("RMP unavailable")
    assert provider.get("Smith, James").note.startswith("RMP unavailable")
    assert calls["n"] == 2
    stored = json.loads(path.read_text()) if path.exists() else {}
    assert "Smith, James" not in stored


def test_legacy_entries_without_timestamp_are_discarded(tmp_path):
    path = tmp_path / "ratings.json"
    path.write_text(
        json.dumps(
            {
                "Smith, James": {
                    "name": "Smith, James",
                    "rating": 5.0,
                    "num_ratings": 99,
                    "source": "rmp",
                    "note": "",
                }
            }
        )
    )
    calls = {"n": 0}

    def lookup(name: str) -> RatingInfo:
        calls["n"] += 1
        return RatingInfo(name=name, note="no RMP record")

    provider = RatingProvider(path=path, lookup=lookup)
    provider.get("Smith, James")
    assert calls["n"] == 1


def test_legacy_failure_entries_are_discarded(tmp_path):
    path = tmp_path / "ratings.json"
    path.write_text(
        json.dumps(
            {
                "Smith, James": {
                    "info": {
                        "name": "Smith, James",
                        "rating": None,
                        "num_ratings": 0,
                        "source": "unrated",
                        "note": "RMP unavailable: timeout",
                    },
                    "fetched_at": 1_000_000.0,
                }
            }
        )
    )
    calls = {"n": 0}

    def lookup(name: str) -> RatingInfo:
        calls["n"] += 1
        return RatingInfo(name=name, rating=4.0, num_ratings=5, source="rmp")

    provider = RatingProvider(path=path, lookup=lookup, clock=lambda: 1_000_001.0)
    info = provider.get("Smith, James")
    assert calls["n"] == 1
    assert info.rating == 4.0


def test_refresh_bypasses_fresh_cache(tmp_path):
    calls = {"n": 0}

    def lookup(name: str) -> RatingInfo:
        calls["n"] += 1
        return RatingInfo(name=name, rating=float(calls["n"]), num_ratings=5, source="rmp")

    provider = RatingProvider(path=tmp_path / "ratings.json", lookup=lookup)
    assert provider.get("Smith, James").rating == 1.0
    assert provider.get("Smith, James", refresh=True).rating == 2.0
    assert calls["n"] == 2


def test_flat_entry_is_read_without_a_lookup(tmp_path):
    path = tmp_path / "ratings.json"
    path.write_text(
        json.dumps(
            {
                "Smith, James": {
                    "name": "Smith, James",
                    "rating": 4.5,
                    "num_ratings": 12,
                    "source": "rmp",
                    "note": "",
                    "fetched_at": 1_000_000.0,
                }
            }
        )
    )

    def lookup(name):
        raise AssertionError("fresh flat cache should be used")

    provider = RatingProvider(path=path, lookup=lookup, clock=lambda: 1_000_001.0)
    assert provider.get("Smith, James").rating == 4.5


def test_nested_entry_is_read_for_transitional_compatibility(tmp_path):
    path = tmp_path / "ratings.json"
    path.write_text(
        json.dumps(
            {
                "Smith, James": {
                    "info": {
                        "name": "Smith, James",
                        "rating": 3.5,
                        "num_ratings": 8,
                        "source": "rmp",
                        "note": "",
                    },
                    "fetched_at": 1_000_000.0,
                }
            }
        )
    )

    def lookup(name):
        raise AssertionError("fresh nested cache should be read")

    provider = RatingProvider(path=path, lookup=lookup, clock=lambda: 1_000_001.0)
    assert provider.get("Smith, James").rating == 3.5


def test_writer_uses_flat_entries(tmp_path):
    path = tmp_path / "ratings.json"
    provider = RatingProvider(
        path=path,
        lookup=lambda name: RatingInfo(name=name, rating=4.0, num_ratings=5, source="rmp"),
        clock=lambda: 1_000_000.0,
    )
    provider.get("Smith, James")
    stored = json.loads(path.read_text())
    assert "info" not in stored["Smith, James"]
    assert stored["Smith, James"]["name"] == "Smith, James"
    assert stored["Smith, James"]["fetched_at"] == 1_000_000.0


def test_migration_backs_up_and_flattens_all_kept_entries(tmp_path):
    from flightplan.planner import migrate_ratings_file

    path = tmp_path / "ratings.json"
    path.write_text(
        json.dumps(
            {
                "Nested, Rated": {
                    "info": {
                        "name": "Nested, Rated",
                        "rating": 5.0,
                        "num_ratings": 9,
                        "source": "rmp",
                        "note": "",
                    },
                    "fetched_at": 1_000_000.0,
                },
                "Legacy, Flat": {
                    "name": "Legacy, Flat",
                    "rating": None,
                    "num_ratings": 0,
                    "source": "unrated",
                    "note": "no RMP record",
                },
                "Broken, Failure": {
                    "info": {"name": "Broken, Failure", "note": "RMP unavailable: timeout"},
                    "fetched_at": 1_000_000.0,
                },
            }
        )
    )
    backup = migrate_ratings_file(path)
    assert backup is not None and backup.exists()
    migrated = json.loads(path.read_text())
    assert set(migrated) == {"Nested, Rated", "Legacy, Flat"}
    for entry in migrated.values():
        assert "info" not in entry
        assert "name" in entry and "fetched_at" in entry
    assert migrated["Legacy, Flat"]["fetched_at"] == 0.0
