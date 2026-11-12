"""Offline tests for name normalization and conservative matching."""

from __future__ import annotations

import json
import pathlib
import sys

import pytest

ROOT = pathlib.Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from eagle_flightplan.rmp.client import _parse_professor  # noqa: E402
from eagle_flightplan.rmp.match import (  # noqa: E402
    match_professor,
    normalize_name,
    rank_candidates,
)
from eagle_flightplan.rmp.models import Professor  # noqa: E402

FIXTURES = pathlib.Path(__file__).parent / "fixtures" / "rmp"


def load_fixture(name: str) -> dict:
    return json.loads((FIXTURES / name).read_text())


def search_professors(fixture: str) -> list[Professor]:
    data = load_fixture(fixture)["data"]["search"]["teachers"]["edges"]
    return [_parse_professor(edge["node"]) for edge in data]


GLENNYS = search_professors("search_jackie_glenny.json")


class StubClient:
    def __init__(self, professors: list[Professor]) -> None:
        self.professors = professors
        self.calls: list[tuple[str, int]] = []

    def search_professors(self, name: str, school_id: int = 706) -> list[Professor]:
        self.calls.append((name, school_id))
        return self.professors

    def close(self) -> None:
        pass


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("  Jackie   GLENNY Jr. ", "jackie glenny"),
        ("O'Brien", "obrien"),
        ("Smith-Jones", "smith jones"),
        ("Glenny, Jackie", "jackie glenny"),
        ("Dr. José Núñez III", "jose nunez"),
        ("Anne-Marie  de la Cruz", "anne marie de la cruz"),
        ("", ""),
    ],
)
def test_normalize_name(raw, expected):
    assert normalize_name(raw) == expected


def test_exact_match():
    match = match_professor("Jackie Glenny", client=StubClient(GLENNYS))
    assert match.professor is not None
    assert match.professor.rmp_id == 1677178
    assert match.confidence == 0.99
    assert match.reason == "exact first and last name match"


def test_suffix_is_ignored():
    match = rank_candidates("Jackie Glenny Jr.", GLENNYS)
    assert match.professor is not None
    assert match.professor.rmp_id == 1677178


def test_first_initial_match():
    match = rank_candidates("J. Glenny", GLENNYS)
    assert match.professor is not None
    assert match.professor.rmp_id == 1677178
    assert match.confidence == 0.85
    assert match.reason == "first-initial match"


def test_initial_that_could_be_two_people_is_ambiguous():
    johns = search_professors("search_john.json")
    match = rank_candidates("J. Printy", johns)
    assert match.professor is None
    assert match.confidence == 0.5
    assert len(match.alternatives) == 2


def test_nickname_match():
    match = rank_candidates("Jack Glenny", GLENNYS)
    assert match.professor is not None
    assert match.professor.rmp_id == 1677178
    assert match.confidence == 0.92
    assert match.reason == "nickname equivalent first name"


def test_nickname_with_apostrophe_surname():
    professor = Professor.model_validate(
        {
            "id": "Teacher-1",
            "legacyId": 1,
            "firstName": "Michael",
            "lastName": "O'Brien",
            "numRatings": 5,
            "avgRating": 4.0,
        }
    )
    match = rank_candidates("Mike O'Brien", [professor])
    assert match.professor is not None
    assert match.confidence == 0.92


def test_hyphenated_surname():
    professor = Professor.model_validate(
        {
            "id": "Teacher-2",
            "legacyId": 2,
            "firstName": "Jackie",
            "lastName": "Smith-Jones",
            "numRatings": 5,
            "avgRating": 4.0,
        }
    )
    match = rank_candidates("Jackie Smith-Jones", [professor])
    assert match.professor is not None
    assert match.confidence == 0.99


def test_surname_only_ambiguous_returns_alternatives_not_a_guess():
    match = match_professor("Glenny", client=StubClient(GLENNYS))
    assert match.professor is None
    assert match.confidence == 0.4
    assert {p.rmp_id for p in match.alternatives} == {1677178, 2359094, 2328488}
    assert "ambiguous" in match.reason


def test_tied_exact_names_are_ambiguous():
    johns = search_professors("search_john.json")
    match = rank_candidates("John Printy", johns)
    assert match.professor is None
    assert match.confidence == 0.5
    assert len(match.alternatives) == 2
    assert "ambiguous" in match.reason


def test_no_match():
    match = match_professor("Zelda Fitzgerald", client=StubClient(GLENNYS))
    assert match.professor is None
    assert match.confidence == 0.0
    assert match.alternatives == []
    assert "no professor matched" in match.reason


def test_zero_ratings_means_null_rating_and_adjusted_confidence():
    node = load_fixture("teacher_zero_ratings.json")["data"]["node"]
    professor = _parse_professor(node)
    assert professor.num_ratings == 0
    assert professor.avg_rating is None
    assert professor.avg_difficulty is None
    assert professor.would_take_again_percent is None

    match = rank_candidates("New Instructor", [professor])
    assert match.professor is not None
    assert match.professor.avg_rating is None
    assert match.confidence < 0.99
    assert match.confidence == pytest.approx(0.792)
    assert "no ratings" in match.reason
