import json
from pathlib import Path

import httpx
import pytest

from eagle_flightplan.auth import SessionExpiredError
from eagle_flightplan.banner.plan import (
    DuplicateCourseError,
    Plan,
    PlanAheadUnavailable,
    PlanClient,
    PlanNotFoundError,
    _extract_json_block,
)
from eagle_flightplan.banner.ssb import SSBClient

FIXTURES = Path(__file__).parent / "fixtures"
BASE = "https://registration-ss.oci.unw.edu/StudentRegistrationSsb/ssb"


def plan_header(courses: list[dict] | None = None) -> dict:
    return {
        "id": 58990,
        "description": "Plan A - CS/CYBR priority",
        "term": "202620",
        "termDescription": "Spring 2027",
        "sequenceNumber": 1,
        "preferredIndicator": True,
        "planCourses": courses or [],
        "totalPlannedCreditHours": 4.0,
    }


def course_row(course_id: int = 206646, crn: str | None = "22445") -> dict:
    return {
        "id": course_id,
        "subject": "CYS",
        "courseNumber": "2081",
        "courseTitle": "Data Communications",
        "courseReferenceNumber": crn,
        "term": "202620",
        "creditHours": 4.0,
        "planStatus": "Planned",
        "section": "01",
    }


class Recorder:
    def __init__(self) -> None:
        self.requests: list[tuple[str, str, str]] = []
        self.courses: list[dict] = []
        self.create_calls = 0

    def handler(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path.replace("/StudentRegistrationSsb/ssb", "")
        body = request.content.decode()
        self.requests.append((request.method, path, body))

        if path == "/term/search":
            return httpx.Response(200, json={"fwdURL": "/ssb/plan/selectPlan"})
        if path == "/plan/selectPlan":
            return httpx.Response(
                200,
                text=(FIXTURES / "plan_select.html").read_text(),
                headers={"content-type": "text/html;charset=UTF-8"},
            )
        if path == "/plan/plan":
            return httpx.Response(
                200,
                text="<script>window.bootstraps={selectedPlan:{}}</script>",
                headers={"content-type": "text/html;charset=UTF-8"},
            )
        if path == "/plan/addPlanItem":
            return httpx.Response(
                200,
                json={
                    "success": True,
                    "model": {
                        "subject": "CYS",
                        "courseNumber": "2081",
                        "courseTitle": "Data Communications",
                        "term": "202620",
                        "tuid": -1,
                        "planStatus": "Pending",
                    },
                },
            )
        if path == "/plan/submitPlan/batch":
            payload = json.loads(body)
            if payload["create"]:
                self.create_calls += 1
                self.courses = [course_row()]
            if payload["update"]:
                self.courses = []
            return httpx.Response(
                200, json={"success": True, "data": {"planHeader": plan_header(self.courses)}}
            )
        if path == "/plan/delete":
            return httpx.Response(
                200, json={"success": True, "data": [], "message": "Plan successfully deleted."}
            )
        return httpx.Response(404, json={"success": False})


@pytest.fixture()
def client() -> tuple[PlanClient, Recorder]:
    recorder = Recorder()
    transport = httpx.MockTransport(recorder.handler)
    http = httpx.Client(transport=transport, base_url=BASE)
    return PlanClient(SSBClient(http, min_interval=0)), recorder


def test_bootstrap_extraction_reads_real_plans():
    html = (FIXTURES / "plan_select.html").read_text()
    plans = _extract_json_block(html, "plans")
    assert isinstance(plans, list) and plans
    assert plans[0]["id"] == 58990
    assert plans[1]["sequenceNumber"] == 2


def test_plan_model_maps_wire_keys():
    plan = Plan.from_api(plan_header([course_row()]))
    assert plan.id == 58990
    assert plan.name == "Plan A - CS/CYBR priority"
    assert plan.term == "202620"
    assert plan.preferred is True
    assert plan.sequence_number == 1
    assert plan.courses[0].code == "CYS 2081"
    assert plan.courses[0].crn == "22445"


def test_create_plan_posts_header_description(client):
    plans, recorder = client
    plan = plans.create_plan("Plan A - CS/CYBR priority", "202620")
    assert plan.id == 58990
    batch = [r for r in recorder.requests if r[1] == "/plan/submitPlan/batch"]
    assert batch
    body = json.loads(batch[0][2])
    assert body["create"] == [{"headerDescription": "Plan A - CS/CYBR priority", "headerComment": ""}]
    assert body["uniqueSessionId"]


def test_create_plan_rejects_bad_names(client):
    plans, _ = client
    with pytest.raises(Exception):
        plans.create_plan("   ", "202620")
    with pytest.raises(Exception):
        plans.create_plan("x" * 81, "202620")


def test_add_course_by_crn_persists_draft_model(client):
    plans, recorder = client
    added = plans.add_course(58990, "202620", crn="22445")
    assert added.id == 206646
    assert recorder.requests[0][:2] == ("POST", "/term/search")
    assert any(r[1] == "/plan/plan" for r in recorder.requests)
    add_item = [r for r in recorder.requests if r[1] == "/plan/addPlanItem"]
    assert add_item and "courseReferenceNumber=22445" in add_item[0][2]
    batch = [json.loads(r[2]) for r in recorder.requests if r[1] == "/plan/submitPlan/batch"]
    assert batch[-1]["create"][0]["tuid"] == -1


def test_add_course_duplicate_guard(client):
    plans, recorder = client
    plans.add_course(58990, "202620", crn="22445")
    with pytest.raises(DuplicateCourseError):
        plans.add_course(58990, "202620", crn="22445")
    assert len([r for r in recorder.requests if r[1] == "/plan/addPlanItem"]) == 1


def test_add_course_requires_identifier(client):
    plans, _ = client
    with pytest.raises(Exception):
        plans.add_course(58990, "202620")


def test_remove_course_sends_delete_action(client):
    plans, recorder = client
    plans.add_course(58990, "202620", crn="22445")
    updated = plans.remove_course(58990, "202620", crn="22445")
    assert updated.courses == []
    update_bodies = [
        json.loads(r[2]) for r in recorder.requests
        if r[1] == "/plan/submitPlan/batch" and json.loads(r[2])["update"]
    ]
    assert update_bodies
    update = update_bodies[-1]["update"][0]
    assert update["selectedPlanAction"] == "Delete"
    assert update["isDeleteAction"] is True
    assert update["id"] == 206646


def test_remove_missing_course_raises(client):
    plans, _ = client
    plans.add_course(58990, "202620", crn="22445")
    with pytest.raises(PlanNotFoundError):
        plans.remove_course(58990, "202620", crn="99999")


def test_delete_plan_uses_integer_sequence_number(client):
    plans, recorder = client
    plans.delete_plan(58990, "202620")
    deletes = [r for r in recorder.requests if r[1] == "/plan/delete"]
    assert deletes and deletes[0][2] == "delete=1"


def test_select_plan_verifies_session_binding(client):
    plans, recorder = client
    selected = plans.select_plan(58990, "202620")
    assert selected.id == 58990
    assert any(r[1] == "/plan/plan" for r in recorder.requests)


def test_non_planable_term_raises_plan_ahead_unavailable():
    html = (FIXTURES / "plan_select_not_planable.html").read_text()

    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path.replace("/StudentRegistrationSsb/ssb", "")
        if path == "/term/search":
            return httpx.Response(200, json={"fwdURL": "/StudentRegistrationSsb/ssb/plan/selectPlan"})
        if path == "/plan/selectPlan":
            return httpx.Response(200, text=html, headers={"content-type": "text/html;charset=UTF-8"})
        return httpx.Response(404)

    http = httpx.Client(transport=httpx.MockTransport(handler), base_url=BASE)
    plans = PlanClient(SSBClient(http, min_interval=0))
    with pytest.raises(PlanAheadUnavailable):
        plans.list_plans("202710")


def test_guest_page_still_raises_session_expired():
    html = '<html><head><meta name="fullName" content=""></head><body>guest shell</body></html>'

    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path.replace("/StudentRegistrationSsb/ssb", "")
        if path == "/term/search":
            return httpx.Response(200, json={"fwdURL": "/plan/selectPlan"})
        return httpx.Response(200, text=html, headers={"content-type": "text/html;charset=UTF-8"})

    http = httpx.Client(transport=httpx.MockTransport(handler), base_url=BASE)
    plans = PlanClient(SSBClient(http, min_interval=0))
    with pytest.raises(SessionExpiredError):
        plans.list_plans("202620")


def test_validate_sections_flags_hard_and_soft_issues():
    from eagle_flightplan.banner.plan import validate_sections
    from eagle_flightplan.models import Section

    def sec(crn, code, day, start, end, *, seats=20, open_=True, credits=4.0):
        from eagle_flightplan.models import Instructor
        from eagle_flightplan.schedule import Meeting

        subject, number = code.split()
        return Section(
            crn=crn, term="202620", subject=subject, number=number, section="01",
            subject_course=f"{subject}{number}", title=code, credits_low=credits,
            capacity=seats, enrollment=0, seats_available=seats, open=open_,
            faculty=[Instructor(name="Smith, James")],
            meetings=[Meeting(day=day, start=start, end=end)],
        )

    sections = [
        sec("1", "CYS 4245", "Mon", "10:00", "11:00"),
        sec("2", "CYS 4245", "Tue", "10:00", "11:00"),  # duplicate course
        sec("3", "CYS 4466", "Mon", "10:30", "11:30"),  # conflict with 1
        sec("4", "CYS 3185", "Wed", "10:00", "11:00", seats=0, open_=False),
    ]
    issues = validate_sections(sections, current_term="202610")
    kinds = {issue.kind for issue in issues}
    assert "duplicate" in kinds and "conflict" in kinds and "availability" in kinds
    assert any(issue.hard for issue in issues if issue.kind == "conflict")


def test_add_course_refuses_hard_blocker_without_http(monkeypatch):
    from eagle_flightplan.banner.plan import Plan, PlanCourse, PlanWriteBlocked
    from eagle_flightplan.models import Instructor, Section
    from eagle_flightplan.schedule import Meeting

    calls = {"n": 0}

    def handler(request):
        calls["n"] += 1
        return httpx.Response(500)

    http = httpx.Client(transport=httpx.MockTransport(handler), base_url=BASE)
    client = PlanClient(SSBClient(http, min_interval=0))

    existing_section = Section(
        crn="1", term="202620", subject="CYS", number="4245", section="01",
        subject_course="CYS4245", title="A", credits_low=4.0, capacity=20,
        seats_available=20, open=True, faculty=[Instructor(name="Smith, James")],
        meetings=[Meeting(day="Mon", start="10:00", end="11:00")],
    )
    candidate = Section(
        crn="2", term="202620", subject="CYS", number="4466", section="01",
        subject_course="CYS4466", title="B", credits_low=2.0, capacity=20,
        seats_available=20, open=True, faculty=[Instructor(name="Smith, James")],
        meetings=[Meeting(day="Mon", start="10:30", end="11:30")],
    )
    plan = Plan(
        id=999, name="Test", term="202620",
        courses=[PlanCourse(id=1, subject="CYS", number="4245", crn="1", credits=4.0)],
    )
    monkeypatch.setattr(client, "select_plan", lambda *args, **kwargs: plan)
    monkeypatch.setattr("eagle_flightplan.banner.search.load_snapshot", lambda term: _snapshot(existing_section, candidate))

    with pytest.raises(PlanWriteBlocked):
        client.add_course(999, "202620", crn="2")
    assert calls["n"] == 0


def _snapshot(*sections):
    from datetime import datetime, timezone

    from eagle_flightplan.banner.search import SnapshotMeta, TermSnapshot

    return TermSnapshot(
        meta=SnapshotMeta(term="202620", fetched_at=datetime.now(timezone.utc)),
        sections=list(sections),
    )
