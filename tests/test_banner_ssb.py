import json
from pathlib import Path

import httpx
import pytest

from unw_planner.auth import SessionExpiredError
from unw_planner.banner.ssb import SSBClient, TermNotSelectedError

BASE = "https://registration-ss.oci.unw.edu/StudentRegistrationSsb/ssb"
FIXTURES = Path(__file__).parent / "fixtures"


def search_payload() -> dict:
    return json.loads((FIXTURES / "search_CYS_202620.json").read_text())


class Recorder:
    def __init__(self) -> None:
        self.urls: list[str] = []
        self.bodies: list[str] = []

    def handler(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path.replace("/StudentRegistrationSsb/ssb", "")
        self.urls.append(request.url.path)
        if path == "/classSearch/getTerms":
            return httpx.Response(200, json=[{"code": "202620", "description": "Spring 2027"}])
        if path == "/term/search":
            self.bodies.append(request.content.decode())
            return httpx.Response(200, json={"fwdURL": "/classSearch/classSearch"})
        if path == "/searchResults/searchResults":
            return httpx.Response(200, json=search_payload())
        if path == "/searchResults/getFacultyMeetingTimes":
            return httpx.Response(
                200,
                json={"fmt": [{"meetingTime": {"beginTime": "1245", "endTime": "1425",
                                               "tuesday": True, "thursday": True}}]},
            )
        if path == "/searchResults/getSectionAttributes":
            return httpx.Response(
                200,
                text="<section><span class='attribute-text'>Computing Data Math Sci Dept  CDMS<br/></span></section>",
                headers={"content-type": "text/html;charset=UTF-8"},
            )
        if path == "/plan/selectPlan":
            return httpx.Response(
                200,
                text="<html><body>guest</body></html>",
                headers={"content-type": "text/html;charset=UTF-8"},
            )
        return httpx.Response(404)


@pytest.fixture()
def client():
    recorder = Recorder()
    http = httpx.Client(transport=httpx.MockTransport(recorder.handler), base_url=BASE)
    return SSBClient(http), recorder


def test_get_terms(client):
    ssb, _ = client
    terms = ssb.get_terms()
    assert [(t.code, t.description) for t in terms] == [("202620", "Spring 2027")]
    assert terms[0].season == "Spring"


def test_next_term_skips_view_only():
    http = httpx.Client(
        transport=httpx.MockTransport(
            lambda request: httpx.Response(
                200,
                json=[
                    {"code": "202630", "description": "Summer 2027"},
                    {"code": "202530", "description": "Summer 2026 (View Only)"},
                ],
            )
        ),
        base_url=BASE,
    )
    assert SSBClient(http).next_term().code == "202630"


def test_search_requires_term(client):
    ssb, _ = client
    with pytest.raises(TermNotSelectedError):
        ssb.search(subject="CYS")


def test_set_term_then_search_filters_open_client_side(client):
    ssb, recorder = client
    ssb.set_term("202620")
    assert "term=202620" in recorder.bodies[0]
    page = ssb.search(subject="CYS", course_number="2081")
    assert page.total_count == 2
    assert [section.crn for section in page.sections] == ["22445", "22891"]
    query = [url for url in recorder.urls if url.endswith("/searchResults/searchResults")]
    assert query, "searchResults was not called"


def test_meeting_times_and_attributes(client):
    ssb, _ = client
    ssb.set_term("202620")
    meetings = ssb.meeting_times("22445")
    assert meetings[0].begin_time == "1245"
    assert [day.value for day in meetings[0].days] == ["Tue", "Thu"]
    attributes = ssb.attributes("22445")
    assert attributes and attributes[0].code == "CDMS"


def test_html_where_json_expected_is_session_expiry(client):
    ssb, _ = client
    ssb.set_term("202620")
    with pytest.raises(SessionExpiredError):
        ssb._get_json("/plan/selectPlan")
