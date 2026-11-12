"""Offline tests for the RMP GraphQL client (httpx.MockTransport)."""

from __future__ import annotations

import json
import pathlib
import sys

import httpx
import pytest

ROOT = pathlib.Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from eagle_flightplan.rmp.cache import SqliteCache  # noqa: E402
from eagle_flightplan.rmp.client import (  # noqa: E402
    Blocked,
    GraphQLError,
    NotFound,
    RMPClient,
    RateLimited,
)
from eagle_flightplan.rmp import queries  # noqa: E402

FIXTURES = pathlib.Path(__file__).parent / "fixtures" / "rmp"


def load_fixture(name: str) -> dict:
    return json.loads((FIXTURES / name).read_text())


class Recorder:
    def __init__(self, *responses: object) -> None:
        self.responses = list(responses)
        self.requests: list[dict] = []
        self.sleeps: list[float] = []
        self.delays: list[float] = []

    def handler(self, request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        self.requests.append(body)
        response = self.responses.pop(0) if len(self.responses) > 1 else self.responses[0]
        if isinstance(response, httpx.Response):
            return response
        return httpx.Response(200, json=response)

    def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)

    def clock(self) -> float:
        return 0.0


def make_client(tmp_path, recorder: Recorder, **kwargs) -> RMPClient:
    cache = kwargs.pop("cache", None) or SqliteCache(tmp_path / "rmp.sqlite")
    return RMPClient(
        cache=cache,
        transport=httpx.MockTransport(recorder.handler),
        sleep=recorder.sleep,
        clock=recorder.clock,
        min_delay=0.0,
        max_delay=0.0,
        **kwargs,
    )


def test_search_matches_recorded_ground_truth(tmp_path):
    recorder = Recorder(load_fixture("search_jackie_glenny.json"))
    client = make_client(tmp_path, recorder)
    professors = client.search_professors("Jackie Glenny")

    assert [p.full_name for p in professors] == [
        "Jackie Glenny",
        "William Glenny",
        "Ed Glenny",
    ]
    jackie = professors[0]
    assert jackie.rmp_id == 1677178
    assert jackie.avg_rating == 2.3
    assert jackie.num_ratings == 31
    assert round(jackie.would_take_again_percent) == 31
    assert jackie.avg_difficulty == 4.3
    assert jackie.department == "Communication"
    assert jackie.profile_url.endswith("/professor/1677178")

    assert recorder.requests[0]["operationName"] == "TeacherSearchResultsPageQuery"
    assert recorder.requests[0]["variables"]["schoolID"] == "U2Nob29sLTcwNg=="
    assert recorder.requests[0]["variables"]["query"]["schoolID"] == "U2Nob29sLTcwNg=="
    client.close()


def test_search_without_school_sets_null_school_filter(tmp_path):
    recorder = Recorder(load_fixture("search_nameonly_jackie.json"))
    client = make_client(tmp_path, recorder)
    professors = client.search_professors("Jackie Glenny", school_id=None)

    variables = recorder.requests[0]["variables"]
    assert variables["schoolID"] is None
    assert "schoolID" not in variables["query"]
    assert variables["includeSchoolFilter"] is False
    assert professors[0].full_name == "Jackie Glenny"
    client.close()


def test_get_professor_detail(tmp_path):
    recorder = Recorder(load_fixture("teacher_1677178.json"))
    client = make_client(tmp_path, recorder)
    professor = client.get_professor(1677178)

    assert professor.full_name == "Jackie Glenny"
    assert professor.avg_rating == 2.3
    assert professor.num_ratings == 31
    assert professor.rating_distribution is not None
    assert professor.rating_distribution.total == 31
    assert len(professor.recent_ratings) == 5
    first = professor.recent_ratings[0]
    assert first.course == "SPE1075"
    assert first.clarity == 1.0
    assert first.helpfulness == 1.0
    assert first.date is not None and first.date.year == 2025
    assert "Tough grader" in first.tags
    assert professor.courses
    assert recorder.requests[0]["operationName"] == "TeacherRatingsPageQuery"
    client.close()


def test_get_ratings_operation(tmp_path):
    recorder = Recorder(load_fixture("ratings_1677178.json"))
    client = make_client(tmp_path, recorder)
    ratings = client.get_ratings(1677178, count=20)

    assert len(ratings) == 20
    assert ratings[0].course == "SPE1075"
    assert recorder.requests[0]["operationName"] == "RatingsListQuery"
    assert recorder.requests[0]["variables"]["count"] == 20
    client.close()


def test_get_school(tmp_path):
    recorder = Recorder(load_fixture("school_706.json"))
    client = make_client(tmp_path, recorder)
    school = client.get_school(706)

    assert school.legacy_id == 706
    assert school.name == "University of Northwestern St. Paul"
    assert school.state == "MN"
    assert school.city == "Roseville"
    client.close()


def test_cache_hit_avoids_second_request(tmp_path):
    recorder = Recorder(load_fixture("search_jackie_glenny.json"))
    client = make_client(tmp_path, recorder)

    first = client.search_professors("Jackie Glenny")
    second = client.search_professors("Jackie Glenny")

    assert len(recorder.requests) == 1
    assert [p.rmp_id for p in first] == [p.rmp_id for p in second]
    client.close()


def test_graphql_error_passthrough(tmp_path):
    recorder = Recorder(load_fixture("error_graphql.json"))
    client = make_client(tmp_path, recorder)

    with pytest.raises(GraphQLError) as excinfo:
        client.search_professors("Jackie Glenny")

    assert "Cannot query field 'bogus'" in str(excinfo.value)
    assert "second error" in str(excinfo.value)
    assert len(excinfo.value.errors) == 2
    client.close()


def test_429_is_retried_with_backoff(tmp_path):
    recorder = Recorder(
        httpx.Response(429, headers={"retry-after": "0"}, text="slow down"),
        load_fixture("search_jackie_glenny.json"),
    )
    client = make_client(tmp_path, recorder)
    professors = client.search_professors("Jackie Glenny")

    assert len(recorder.requests) == 2
    assert professors[0].rmp_id == 1677178
    client.close()


def test_429_exhausts_into_rate_limited(tmp_path):
    recorder = Recorder(
        httpx.Response(429, headers={"retry-after": "1"}, text="slow down")
    )
    client = make_client(tmp_path, recorder, max_retries=1)

    with pytest.raises(RateLimited) as excinfo:
        client.search_professors("Jackie Glenny")

    assert excinfo.value.retry_after == 1.0
    assert len(recorder.requests) == 2
    client.close()


def test_cloudflare_403_is_blocked(tmp_path):
    recorder = Recorder(
        httpx.Response(403, text="<html><title>Just a moment...</title></html>")
    )
    client = make_client(tmp_path, recorder)

    with pytest.raises(Blocked):
        client.search_professors("Jackie Glenny")
    client.close()


def test_missing_node_is_not_found(tmp_path):
    recorder = Recorder({"data": {"node": None}})
    client = make_client(tmp_path, recorder)

    with pytest.raises(NotFound):
        client.get_professor(123)
    client.close()


def test_operation_documents_are_the_extracted_ones():
    assert queries.SEARCH_PROFESSORS_QUERY.startswith(
        "query TeacherSearchResultsPageQuery("
    )
    assert "newSearch" in queries.SEARCH_PROFESSORS_QUERY
    assert queries.PROFESSOR_DETAIL_QUERY.startswith("query TeacherRatingsPageQuery(")
    assert queries.SCHOOL_QUERY.startswith("query SchoolRatingsPageQuery(")
