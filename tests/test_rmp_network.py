"""Live smoke test. Skipped unless ``RMP_RUN_NETWORK=1``.

Run with: ``RMP_RUN_NETWORK=1 pytest tests/test_rmp_network.py -m network``
"""

from __future__ import annotations

import os
import pathlib
import sys

import pytest

ROOT = pathlib.Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from unw_planner.rmp.cache import SqliteCache  # noqa: E402
from unw_planner.rmp.client import RMPClient  # noqa: E402

pytestmark = [
    pytest.mark.network,
    pytest.mark.skipif(
        os.environ.get("RMP_RUN_NETWORK") != "1",
        reason="set RMP_RUN_NETWORK=1 to hit the live Rate My Professors API",
    ),
]


def test_live_search_jackie_glenny(tmp_path):
    cache = SqliteCache(tmp_path / "net.sqlite")
    with RMPClient(cache=cache) as client:
        professors = client.search_professors("Jackie Glenny")
    assert professors[0].rmp_id == 1677178
    assert professors[0].avg_rating == 2.3
    assert professors[0].num_ratings == 31
    assert round(professors[0].would_take_again_percent) == 31
    assert professors[0].avg_difficulty == 4.3


def test_live_school_lookup():
    with RMPClient() as client:
        school = client.get_school(706)
    assert school.name == "University of Northwestern St. Paul"
