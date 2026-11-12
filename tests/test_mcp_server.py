"""Offline tests for the MCP server surface and its pure helpers."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

import httpx
import pytest

from eagle_flightplan import mcp_server
from eagle_flightplan.banner.plan import (
    Plan,
    PlanClient,
    PlanCourse,
    PlanError,
    PlanNotFoundError,
    PlanWriteBlocked,
)
from eagle_flightplan.banner.search import SnapshotMeta, TermSnapshot
from eagle_flightplan.banner.ssb import SSBClient, TermEligibility
from eagle_flightplan.models import Instructor, Section, SectionAttribute
from eagle_flightplan.planner import RatingInfo
from eagle_flightplan.schedule import Meeting, Weekday

FIXTURES = Path(__file__).resolve().parent / "fixtures"

EXPECTED_TOOLS = {
    "unw_audit",
    "unw_banner_plan_add",
    "unw_banner_plan_create",
    "unw_banner_plan_delete",
    "unw_banner_plan_remove",
    "unw_banner_plans",
    "unw_candidates",
    "unw_conflicts",
    "unw_course",
    "unw_offering_matrix",
    "unw_plan_draft",
    "unw_profile",
    "unw_ratings",
    "unw_register_preview",
    "unw_registration_window",
    "unw_remaining",
    "unw_roadmap",
    "unw_search",
    "unw_session",
    "unw_terms",
}


def sample_section(
    *, term: str = "202620", seats: int | None = 0, open_section: bool = False
) -> Section:
    return Section(
        crn="12345",
        term=term,
        subject="CYS",
        number="2081",
        section="01",
        title="Data Communications",
        credits_low=4,
        credits_high=None,
        capacity=30,
        seats_available=seats,
        open=open_section,
        faculty=[Instructor(name="Smith, James", email="professor@example.edu", primary=True)],
        attributes=[SectionAttribute(code="CCUL", description="Cultural")],
        meetings=[Meeting(day=Weekday.TUE, start="1245", end="1425", location="Riley R1332")],
    )


# --------------------------------------------------------------------------- #
# normalisation
# --------------------------------------------------------------------------- #
def test_section_payload_normalisation() -> None:
    payload = mcp_server.section_payload(sample_section(seats=4, open_section=True))
    assert payload["crn"] == "12345"
    assert payload["code"] == "CYS 2081"
    assert payload["section_code"] == "CYS 2081-01"
    assert payload["credits"] == 4
    assert payload["variable_credits"] is False
    assert payload["days"] == "T"
    assert payload["times"] == "12:45-14:25"
    assert payload["availability"] == "open"
    assert payload["meetings"] == [
        {
            "day": "Tue",
            "start": "12:45",
            "end": "14:25",
            "location": "Riley R1332",
            "kind": "",
            "scheduled": True,
        }
    ]
    assert payload["attribute_codes"] == ["CCUL"]
    assert payload["instructors"] == ["Smith, James"]


def test_availability_distinguishes_closed_from_not_yet_open() -> None:
    future_zero_seats = sample_section(term="202620", seats=0)
    current_zero_seats = sample_section(term="202610", seats=0)
    assert (
        mcp_server.section_payload(future_zero_seats, current_term="202610")[
            "availability"
        ]
        == "not_yet_open"
    )
    assert (
        mcp_server.section_payload(current_zero_seats, current_term="202610")[
            "availability"
        ]
        == "closed"
    )


def test_tba_meeting_is_unscheduled() -> None:
    section = sample_section(seats=2, open_section=True)
    section.meetings = [Meeting(day=Weekday.MON, start=None, end=None)]
    payload = mcp_server.section_payload(section)
    assert payload["unscheduled"] is True
    assert payload["meetings"][0]["scheduled"] is False
    assert payload["times"] == "TBA"


# --------------------------------------------------------------------------- #
# plan name -> id resolution
# --------------------------------------------------------------------------- #
def plan(plan_id: int, name: str) -> Plan:
    return Plan(id=plan_id, name=name, term="202620", sequence_number=plan_id % 10)


def test_resolve_plan_by_exact_name_and_id() -> None:
    plans = [plan(59003, "Plan A - Max Rated"), plan(59005, "Plan B")]
    assert mcp_server._resolve_plan(plans, "Plan B").id == 59005
    assert mcp_server._resolve_plan(plans, 59003).id == 59003
    assert mcp_server._resolve_plan(plans, "59005").id == 59005
    assert mcp_server._resolve_plan(plans, "plan b").id == 59005


def test_resolve_plan_ambiguous_lists_candidates() -> None:
    plans = [plan(1, "Same"), plan(2, "Same"), plan(3, "Other")]
    with pytest.raises(PlanError) as excinfo:
        mcp_server._resolve_plan(plans, "Same")
    message = str(excinfo.value)
    assert "ambiguous" in message.lower()
    assert "1" in message and "2" in message


def test_resolve_plan_missing_lists_available() -> None:
    plans = [plan(59003, "Plan A - Max Rated")]
    with pytest.raises(PlanNotFoundError) as excinfo:
        mcp_server._resolve_plan(plans, "Nope")
    message = str(excinfo.value)
    assert "No plan named 'Nope'" in message
    assert "59003" in message and "Plan A - Max Rated" in message


# --------------------------------------------------------------------------- #
# ratings
# --------------------------------------------------------------------------- #
def test_unrated_professor_is_null_not_zero() -> None:
    unrated = mcp_server.rating_payload(RatingInfo(name="Nobody, Nemo"))
    assert unrated["rated"] is False
    assert unrated["rating"] is None
    assert unrated["difficulty"] is None
    assert unrated["num_ratings"] == 0

    # RMP's "has a rating but zero ratings" sentinel must also stay null.
    inconsistent = mcp_server.rating_payload(
        RatingInfo(name="Ghost", rating=4.5, difficulty=2.0, num_ratings=0)
    )
    assert inconsistent["rated"] is False
    assert inconsistent["rating"] is None


def test_names_keep_commas_for_single_lookups() -> None:
    assert mcp_server._norm_names("Zderad, Jonathan") == ["Zderad, Jonathan"]
    assert mcp_server._norm_names(["Zderad, Jonathan", "Megan Brown"]) == [
        "Zderad, Jonathan",
        "Megan Brown",
    ]
    assert mcp_server._norm_names("") == []


def test_rating_payload_flags_weak_evidence() -> None:
    weak = mcp_server.rating_payload(
        RatingInfo(name="Thin, Evidence", rating=4.0, difficulty=2.0, num_ratings=2)
    )
    assert weak["rated"] is True
    assert weak["rating"] == 4.0
    assert weak["weak_evidence"] is True


# --------------------------------------------------------------------------- #
# profile
# --------------------------------------------------------------------------- #
def test_profile_builder_uses_audit_fields() -> None:
    payload = json.loads((FIXTURES / "audit_WEB31.json").read_text(encoding="utf-8"))
    profile = mcp_server.build_profile(payload)
    assert profile["ok"] is True
    assert profile["name"] == "REDACTED STUDENT"
    assert profile["id"] == "00000000"
    assert profile["classification"] == "Junior"
    assert profile["majors"] == ["Cybersecurity", "Computer Science"]
    assert "Bible" in profile["minors"]
    assert profile["programs"] == ["Computer Science-BS", "Cybersecurity-BS"]
    assert profile["credits_earned"] == 86
    assert profile["credits_in_progress"] == 15
    assert profile["gpa"] == 4.0
    assert profile["percent_complete"] == 76
    assert profile["anticipated_graduation"] == "Spring 2028"
    assert profile["academic_standing"].startswith("Good Standing")
    assert profile["holds"] is None
    names = [advisor["name"] for advisor in profile["advisors"]]
    assert names.count("Zderad, Jonathan A") == 1
    assert "Megan Brown" in names


# --------------------------------------------------------------------------- #
# registration preview never writes
# --------------------------------------------------------------------------- #
class _OfflineSSB:
    def __init__(self, *args, **kwargs) -> None:
        pass

    def set_term(self, term: str, mode: str = "search") -> TermEligibility:
        assert mode == "registration"
        return TermEligibility(
            {
                "regAllowed": False,
                "studentEligValid": False,
                "studentEligFailures": ["Term not eligible for registration"],
                "fwdURL": "/StudentRegistrationSsb/ssb/classRegistration/classRegistration",
            }
        )

    def __getattr__(self, name: str):
        raise AssertionError(f"register preview must not call SSBClient.{name}")


class _OfflineAudit:
    def __init__(self, *args, **kwargs) -> None:
        pass

    def audit(self):
        raise RuntimeError("offline test")


def test_register_preview_refuses_and_reports_banner_text(monkeypatch) -> None:
    snapshot = TermSnapshot(
        meta=SnapshotMeta(
            term="202620",
            fetched_at=datetime.now(UTC),
            source="cache",
            total_reported=1,
        ),
        sections=[sample_section(seats=3, open_section=True)],
    )
    monkeypatch.setattr(mcp_server.auth, "ensure", lambda *args, **kwargs: None)
    monkeypatch.setattr(mcp_server, "SSBClient", _OfflineSSB)
    monkeypatch.setattr(mcp_server, "AuditClient", _OfflineAudit)
    monkeypatch.setattr(mcp_server, "_snapshot", lambda term, refresh=False: snapshot)

    payload = mcp_server.registration_preview_payload("202620", ["12345"])

    assert payload["ok"] is True
    assert payload["dry_run"] is True
    assert payload["registration_attempted"] is False
    assert payload["writes_performed"] == []
    assert payload["can_register"] is False
    assert payload["banner_message"] == "Term not eligible for registration"
    assert payload["eligibility"]["student_elig_valid"] is False
    assert payload["missing_crns"] == []
    assert payload["conflict_count"] == 0
    assert payload["prerequisites"]["checked"] is False


# --------------------------------------------------------------------------- #
# term sequencing
# --------------------------------------------------------------------------- #
def test_term_sequence_includes_start_term() -> None:
    assert mcp_server._term_sequence("202620", 4, False) == [
        "202620",
        "202710",
        "202720",
        "202810",
    ]
    assert mcp_server._term_sequence("202620", 4, True) == [
        "202620",
        "202630",
        "202710",
        "202720",
    ]
    assert mcp_server._term_sequence("202610", 3, False) == [
        "202610",
        "202620",
        "202710",
    ]


def test_next_summer() -> None:
    assert mcp_server._next_summer("202610") == "202630"
    assert mcp_server._next_summer("202620") == "202630"
    assert mcp_server._next_summer("202630") == "202730"


# --------------------------------------------------------------------------- #
# MCP surface
# --------------------------------------------------------------------------- #
async def test_tool_count_names_and_descriptions() -> None:
    tools = await mcp_server.mcp.list_tools()
    names = {tool.name for tool in tools}
    assert names == EXPECTED_TOOLS
    assert len(tools) == 20
    for tool in tools:
        assert tool.description and tool.description.strip(), tool.name
        assert tool.input_schema.get("type") == "object"


async def test_cached_tools_expose_refresh() -> None:
    tools = {tool.name: tool for tool in await mcp_server.mcp.list_tools()}
    for name in (
        "unw_search",
        "unw_terms",
        "unw_course",
        "unw_candidates",
        "unw_conflicts",
        "unw_plan_draft",
        "unw_roadmap",
        "unw_offering_matrix",
    ):
        assert "refresh" in tools[name].input_schema["properties"], name


async def test_plans_docstring_states_term_scoping() -> None:
    tools = {tool.name: tool for tool in await mcp_server.mcp.list_tools()}
    description = tools["unw_banner_plans"].description
    assert "TERM-SCOPED" in description
    assert "THIS term" in description
    assert "not" in description.replace("Note", "note")


async def test_register_preview_docstring_states_dry_run() -> None:
    tools = {tool.name: tool for tool in await mcp_server.mcp.list_tools()}
    assert "DRY RUN" in tools["unw_register_preview"].description


def test_server_instructions_are_concrete() -> None:
    lines = [line for line in mcp_server.INSTRUCTIONS.splitlines() if line.strip()]
    assert 30 <= len(lines) <= 60
    for required in ("unw_session", "unw_offering_matrix", "unw_register_preview"):
        assert required in mcp_server.INSTRUCTIONS


# --------------------------------------------------------------------------- #
# enforced confirmation for Banner writes
# --------------------------------------------------------------------------- #
def plan_fixture(
    plan_id: int = 59021,
    name: str = "Advisor Plan - Spring 2027",
    courses: list[PlanCourse] | None = None,
) -> Plan:
    return Plan(
        id=plan_id,
        name=name,
        term="202620",
        sequence_number=5,
        courses=courses or [],
    )


def plan_course_row(
    course_id: int = 206646,
    *,
    crn: str = "22445",
    subject: str = "CYS",
    number: str = "2081",
    credits: float = 4.0,
) -> PlanCourse:
    return PlanCourse(
        id=course_id,
        subject=subject,
        number=number,
        title="Data Communications",
        crn=crn,
        term="202620",
        credits=credits,
        status="Planned",
        section="01",
    )


class RefusingPlanClient:
    """Lists plans but fails loudly if a write method is touched."""

    def __init__(self, plans: list[Plan]) -> None:
        self.plans = plans
        self.calls: list[str] = []

    def list_plans(self, term: str) -> list[Plan]:
        self.calls.append("list_plans")
        return list(self.plans)

    def __getattr__(self, name: str):
        def _write(*args, **kwargs):
            self.calls.append(name)
            raise AssertionError(f"write method {name} must not be called on refusal")

        return _write


def add_target_snapshot() -> TermSnapshot:
    section = Section(
        crn="22449",
        term="202620",
        subject="CYS",
        number="4855",
        section="01",
        title="Senior Capstone",
        credits_low=2.0,
        seats_available=5,
        open=True,
        meetings=[],
    )
    return TermSnapshot(
        meta=SnapshotMeta(
            term="202620",
            fetched_at=datetime.now(UTC),
            source="cache",
            total_reported=1,
        ),
        sections=[section],
    )


def test_create_refuses_without_confirm_and_does_not_write(monkeypatch) -> None:
    recorder = RefusingPlanClient([plan_fixture()])
    monkeypatch.setattr(mcp_server, "PlanClient", lambda: recorder)

    payload = mcp_server.plan_create_guarded("202620", "Plan D - Draft", False)

    assert payload["ok"] is False
    assert payload["refused"] is True
    assert payload["confirmation_required"] is True
    assert payload["writes_performed"] == []
    assert "would create" in payload["intended_effect"]
    assert "Plan D - Draft" in payload["intended_effect"]
    assert payload["current_state"]["plan_count"] == 1
    assert payload["required_confirmation"] == {"confirm": True}
    assert recorder.calls == ["list_plans"]


def test_add_refusal_names_exact_section_and_plan(monkeypatch) -> None:
    recorder = RefusingPlanClient([plan_fixture(courses=[plan_course_row()])])
    monkeypatch.setattr(mcp_server, "PlanClient", lambda: recorder)
    monkeypatch.setattr(mcp_server, "_snapshot", lambda term, refresh=False: add_target_snapshot())

    payload = mcp_server.plan_add_guarded(
        "202620", "Advisor Plan - Spring 2027", False, crn="22449"
    )

    assert payload["refused"] is True
    assert payload["writes_performed"] == []
    assert "CYS 4855 CRN 22449" in payload["intended_effect"]
    assert "Advisor Plan - Spring 2027" in payload["intended_effect"]
    assert payload["current_state"]["course_count"] == 1
    assert recorder.calls == ["list_plans"]


def test_remove_refusal_names_row_and_credits(monkeypatch) -> None:
    row = plan_course_row(206700, crn="22449", subject="CYS", number="4855", credits=2.0)
    recorder = RefusingPlanClient([plan_fixture(courses=[row])])
    monkeypatch.setattr(mcp_server, "PlanClient", lambda: recorder)

    payload = mcp_server.plan_remove_guarded(
        "202620", "59021", False, crn="22449"
    )

    assert payload["refused"] is True
    assert payload["writes_performed"] == []
    assert "would remove CYS 4855" in payload["intended_effect"]
    assert "2 -> 0" in payload["intended_effect"]
    assert recorder.calls == ["list_plans"]


def test_delete_refuses_without_confirm(monkeypatch) -> None:
    recorder = RefusingPlanClient([plan_fixture(courses=[plan_course_row()])])
    monkeypatch.setattr(mcp_server, "PlanClient", lambda: recorder)

    payload = mcp_server.plan_delete_guarded(
        "202620", "59021", False, None
    )

    assert payload["refused"] is True
    assert payload["writes_performed"] == []
    assert "would DELETE" in payload["intended_effect"]
    assert payload["required_confirmation"]["confirm_name"] == "Advisor Plan - Spring 2027"
    assert recorder.calls == ["list_plans"]


def test_delete_refuses_wrong_confirm_name(monkeypatch) -> None:
    recorder = RefusingPlanClient([plan_fixture(courses=[plan_course_row()])])
    monkeypatch.setattr(mcp_server, "PlanClient", lambda: recorder)

    payload = mcp_server.plan_delete_guarded(
        "202620", "59021", True, "Advisor Plan - Spring 2027 "
    )

    assert payload["refused"] is True
    assert payload["writes_performed"] == []
    assert "does not match the exact plan name" in payload["error"]
    assert "Advisor Plan - Spring 2027" in payload["error"]
    assert recorder.calls == ["list_plans"]


class PlanHTTPRecorder:
    """Minimal Plan Ahead server for one add, with real read-back."""

    def __init__(self) -> None:
        self.courses = [
            {
                "id": 206646,
                "subject": "CYS",
                "courseNumber": "2081",
                "courseTitle": "Data Communications",
                "courseReferenceNumber": "22445",
                "section": "01",
                "term": "202620",
                "creditHours": 4.0,
                "planStatus": "Planned",
            }
        ]
        self.requests: list[tuple[str, str]] = []

    @staticmethod
    def header(courses: list[dict]) -> dict:
        return {
            "id": 59021,
            "description": "Advisor Plan - Spring 2027",
            "term": "202620",
            "termDescription": "Spring 2027",
            "sequenceNumber": 5,
            "preferredIndicator": False,
            "planCourses": courses,
            "totalPlannedCreditHours": 4.0,
        }

    def handler(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path.replace("/StudentRegistrationSsb/ssb", "")
        self.requests.append((request.method, path))
        if path == "/term/search":
            return httpx.Response(200, json={"fwdURL": "/ssb/plan/selectPlan"})
        if path == "/plan/selectPlan":
            bootstrap = json.dumps([self.header(self.courses)])
            text = (
                "<script>window.bootstraps = { plans: "
                + bootstrap
                + ", selectedPlan: {} };</script>"
            )
            return httpx.Response(200, text=text, headers={"content-type": "text/html"})
        if path == "/plan/plan":
            return httpx.Response(
                200,
                text="<script>window.bootstraps={selectedPlan:{}}</script>",
                headers={"content-type": "text/html"},
            )
        if path == "/plan/addPlanItem":
            return httpx.Response(
                200,
                json={
                    "success": True,
                    "model": {
                        "subject": "CYS",
                        "courseNumber": "4855",
                        "courseTitle": "Senior Capstone",
                        "courseReferenceNumber": "22449",
                        "section": "01",
                        "term": "202620",
                        "creditHours": 2.0,
                        "tuid": -1,
                        "planStatus": "Pending",
                    },
                },
            )
        if path == "/plan/submitPlan/batch":
            body = json.loads(request.content.decode())
            if body["create"]:
                course = {
                    "id": 206700,
                    "subject": "CYS",
                    "courseNumber": "4855",
                    "courseTitle": "Senior Capstone",
                    "courseReferenceNumber": "22449",
                    "section": "01",
                    "term": "202620",
                    "creditHours": 2.0,
                    "planStatus": "Planned",
                    }
                self.courses = [*self.courses, course]
            return httpx.Response(
                200, json={"success": True, "data": {"planHeader": self.header(self.courses)}}
            )
        return httpx.Response(404, json={"success": False})


def test_add_success_returns_read_back_diff(monkeypatch) -> None:
    recorder = PlanHTTPRecorder()
    transport = httpx.MockTransport(recorder.handler)
    http = httpx.Client(
        transport=transport,
        base_url="https://registration-ss.oci.unw.edu/StudentRegistrationSsb/ssb",
    )
    real_plan_client = PlanClient
    monkeypatch.setattr(
        mcp_server,
        "PlanClient",
        lambda: real_plan_client(SSBClient(http, min_interval=0)),
    )
    monkeypatch.setattr(mcp_server, "_snapshot", lambda term, refresh=False: add_target_snapshot())
    # Keep this test hermetic: the prerequisite gate is covered separately.
    monkeypatch.setattr(mcp_server, "_passed_codes_or_none", lambda: None)

    payload = mcp_server.plan_add_guarded(
        "202620", "59021", True, crn="22449"
    )

    assert payload["ok"] is True
    assert payload["prereq_checked"] is False
    assert payload["writes_performed"] == ["add_course"]
    diff = payload["diff"]
    assert diff["action"] == "add_course"
    assert diff["rows_added"][0]["crn"] == "22449"
    assert diff["rows_added"][0]["code"] == "CYS 4855"
    assert diff["rows_removed"] == []
    assert diff["course_count_before"] == 1
    assert diff["course_count_after"] == 2
    assert diff["credits_before"] == 4.0
    assert diff["credits_after"] == 6.0
    assert diff["credits_delta"] == 2.0
    assert len(payload["plan"]["courses"]) == 2
    assert any(path == "/plan/addPlanItem" for _, path in recorder.requests)


async def test_write_tools_require_confirmation_arguments() -> None:
    tools = {tool.name: tool for tool in await mcp_server.mcp.list_tools()}
    for name in (
        "unw_banner_plan_create",
        "unw_banner_plan_add",
        "unw_banner_plan_remove",
        "unw_banner_plan_delete",
    ):
        required = tools[name].input_schema["required"]
        assert "confirm" in required, name
        assert "confirm" in (tools[name].description or "").lower()
    delete_required = tools["unw_banner_plan_delete"].input_schema["required"]
    assert "confirm_name" in delete_required


# --------------------------------------------------------------------------- #
# result caps, ratings freshness, registration windows, pins
# --------------------------------------------------------------------------- #
def many_sections(count: int = 205) -> TermSnapshot:
    return TermSnapshot(
        meta=SnapshotMeta(term="202620", fetched_at=datetime.now(UTC), source="cache"),
        sections=[
            Section(
                crn=str(index),
                term="202620",
                subject="CYS",
                number=str(1000 + index),
                section="01",
                title=f"Course {index}",
                credits_low=3.0,
                seats_available=5,
                open=True,
            )
            for index in range(count)
        ],
    )


def test_search_limit_is_capped_and_reports_paging(monkeypatch) -> None:
    monkeypatch.setattr(mcp_server, "_snapshot", lambda term, refresh=False: many_sections())
    payload = mcp_server.search_payload("202620", limit=1000)
    assert payload["requested_limit"] == 1000
    assert payload["limit"] == mcp_server.SEARCH_LIMIT_CAP == 200
    assert payload["limit_capped"] is True
    assert payload["count"] == 200
    assert payload["total_matched"] == 205
    assert payload["next_offset"] == 200

    last = mcp_server.search_payload("202620", limit=1000, offset=200)
    assert last["count"] == 5
    assert last["next_offset"] is None


def test_section_payload_reports_credit_range() -> None:
    variable = Section(
        crn="1",
        term="202620",
        subject="COS",
        number="4841",
        section="01",
        title="Internship",
        credits_low=1.0,
        credits_high=6.0,
        seats_available=5,
        open=True,
    )
    payload = mcp_server.section_payload(variable)
    assert payload["credit_range"] == [1.0, 6.0]
    assert payload["planning_credits"] == 1.0
    assert payload["credits"] == 6.0


def test_ratings_refresh_is_forwarded(monkeypatch) -> None:
    calls: list[tuple[str, bool]] = []

    class StubProvider:
        def get(self, name: str, refresh: bool = False):
            calls.append((name, refresh))
            return RatingInfo(name=name, note="no RMP record")

    monkeypatch.setattr(mcp_server, "RatingProvider", StubProvider)
    payload = mcp_server.ratings_payload(["Smith, James"], refresh=True)
    assert calls == [("Smith, James", True)]
    assert payload["refresh"] is True
    assert payload["ratings"][0]["rating"] is None


def test_registration_window_is_read_only_and_parses_banner_text(monkeypatch) -> None:
    from eagle_flightplan.registration import parse_failures

    class TermsClient:
        def get_terms(self):
            from eagle_flightplan.banner.models import Term

            return [
                Term(code="202610", description="Fall 2026"),
                Term(code="202620", description="Spring 2027"),
            ]

    fake = TermsClient()
    monkeypatch.setattr(mcp_server, "SSBClient", lambda client: fake)
    monkeypatch.setattr(mcp_server.auth, "ensure", lambda: object())
    monkeypatch.setattr(
        "eagle_flightplan.registration.window",
        lambda term, ssb=None: parse_failures(
            term,
            ["You can register from 04/20/2026 08:30 AM to 10/28/2026 11:59 PM"],
            False,
        ),
    )
    payload = mcp_server.registration_window_payload(None)
    assert payload["ok"] is True
    assert payload["read_only"] is True
    assert payload["writes_performed"] == []
    assert payload["timezone"] == "America/Chicago"
    assert payload["count"] == 2
    assert [row["term"] for row in payload["windows"]] == ["202610", "202620"]
    assert payload["windows"][0]["parse_status"] == "parsed"
    assert payload["windows"][0]["start"].startswith("2026-04-20T08:30")
    assert "countdown" in payload["windows"][0]


def test_current_term_prefers_audit_active_term(monkeypatch) -> None:
    raw = {"degreeInformation": {"degreeDataArray": [{"activeTerm": "202710"}]}}
    assert mcp_server._active_term(raw) == "202710"
    assert mcp_server._active_term({}) == mcp_server.CURRENT_TERM


def test_plan_add_refuses_missing_prereq_before_any_post(monkeypatch) -> None:
    recorder = PlanHTTPRecorder()
    transport = httpx.MockTransport(recorder.handler)
    http = httpx.Client(
        transport=transport,
        base_url="https://registration-ss.oci.unw.edu/StudentRegistrationSsb/ssb",
    )
    real_plan_client = PlanClient
    monkeypatch.setattr(
        mcp_server,
        "PlanClient",
        lambda: real_plan_client(SSBClient(http, min_interval=0)),
    )
    monkeypatch.setattr(mcp_server, "_snapshot", lambda term, refresh=False: add_target_snapshot())
    monkeypatch.setattr(mcp_server, "_passed_codes_or_none", lambda: set())
    monkeypatch.setattr(
        "eagle_flightplan.banner.search.load_snapshot", lambda term: add_target_snapshot()
    )

    class BlockingPrereq:
        def __init__(self, *args, **kwargs) -> None:
            pass

        def check(self, section, passed):
            return False, "needs COS 2099"

    monkeypatch.setattr("eagle_flightplan.planner.PrereqProvider", BlockingPrereq)

    with pytest.raises(PlanWriteBlocked):
        mcp_server.plan_add_guarded("202620", "59021", True, crn="22449")
    assert not any(path == "/plan/addPlanItem" for _, path in recorder.requests)


def test_summer_pass_carries_spring_prereq(monkeypatch) -> None:
    from eagle_flightplan.audit.models import (
        Audit,
        CourseRef,
        GroupKind,
        RequirementBlock,
        RequirementGroup,
    )
    from eagle_flightplan.planner import Planner, PrereqProvider, RatingInfo, RatingProvider

    audit = Audit(
        student_name="Test Student",
        groups=[
            RequirementGroup(
                id="RA1",
                title="Major",
                kind=GroupKind.MAJOR,
                blocks=[
                    RequirementBlock(
                        label="Intro",
                        pick_n_of=1,
                        options=[CourseRef(subject="COS", number="1000")],
                    ),
                    RequirementBlock(
                        label="Advanced",
                        pick_n_of=1,
                        options=[CourseRef(subject="COS", number="2000")],
                    ),
                ],
            )
        ],
    )

    def sec(crn, number, term):
        return Section(
            crn=crn,
            term=term,
            subject="COS",
            number=number,
            section="01",
            title=f"COS {number}",
            credits_low=4.0,
            seats_available=10,
            open=True,
        )

    snapshots = {
        "202620": TermSnapshot(
            meta=SnapshotMeta(term="202620", fetched_at=datetime.now(UTC), source="cache"),
            sections=[sec("1", "1000", "202620")],
        ),
        "202630": TermSnapshot(
            meta=SnapshotMeta(term="202630", fetched_at=datetime.now(UTC), source="cache"),
            sections=[sec("2", "2000", "202630")],
        ),
    }

    def prereq_fetcher(section):
        if section.number == "2000":
            return "Course or Test: COS 1000"
        return "No prerequisite information available."

    planner = Planner(
        audit,
        snapshots,
        ratings=RatingProvider(
            path="/tmp/eagle-flightplan-mcp-summer-ratings.json",
            lookup=lambda name: RatingInfo(name=name),
        ),
        prereqs=PrereqProvider(
            directory="/tmp/eagle-flightplan-mcp-summer-prereq", fetcher=prereq_fetcher
        ),
        current_term="202620",
    )
    monkeypatch.setattr(
        mcp_server,
        "_planner",
        lambda terms, refresh=False: (planner, snapshots, audit, {}, "202620"),
    )
    payload = mcp_server.draft_payload("202620", include_summer=True)
    assert [s["course"] for s in payload["draft"]["selections"]] == ["COS 1000"]
    assert [s["course"] for s in payload["summer"]["draft"]["selections"]] == ["COS 2000"]
    assert payload["draft"]["grid_legend"]["COS 1000"] == ["1"]


async def test_new_draft_and_rating_arguments_are_in_the_schema() -> None:
    tools = {tool.name: tool for tool in await mcp_server.mcp.list_tools()}
    properties = tools["unw_plan_draft"].input_schema["properties"]
    for key in (
        "min_credits",
        "required_crns",
        "required_courses",
        "exclude_crns",
        "exclude_courses",
        "avoid_instructors",
        "exclude_arrangement",
        "hours",
    ):
        assert key in properties, key
    assert properties["hours"]["anyOf"][0]["type"] == "object"
    assert "refresh" in tools["unw_ratings"].input_schema["properties"]
    assert "READ-ONLY" in tools["unw_registration_window"].description


def test_draft_payload_reports_failed_pin_as_ok_false(monkeypatch) -> None:
    from eagle_flightplan.audit.models import (
        Audit,
        CourseRef,
        GroupKind,
        RequirementBlock,
        RequirementGroup,
    )
    from eagle_flightplan.planner import Planner, PrereqProvider, RatingInfo, RatingProvider

    audit = Audit(
        student_name="Test Student",
        groups=[
            RequirementGroup(
                id="RA1",
                title="Major",
                kind=GroupKind.MAJOR,
                blocks=[
                    RequirementBlock(
                        label="Python",
                        pick_n_of=1,
                        options=[CourseRef(subject="COS", number="2005")],
                    )
                ],
            )
        ],
    )
    snapshot = TermSnapshot(
        meta=SnapshotMeta(term="202620", fetched_at=datetime.now(UTC), source="cache"),
        sections=[
            Section(
                crn="1",
                term="202620",
                subject="COS",
                number="2005",
                section="01",
                title="Python",
                credits_low=4.0,
                seats_available=10,
                open=True,
            )
        ],
    )
    planner = Planner(
        audit,
        {"202620": snapshot},
        ratings=RatingProvider(
            path="/tmp/eagle-flightplan-mcp-pin-ratings.json",
            lookup=lambda name: RatingInfo(name=name),
        ),
        prereqs=PrereqProvider(
            directory="/tmp/eagle-flightplan-mcp-pin-prereq",
            fetcher=lambda s: "No prerequisite information available.",
        ),
        current_term="202620",
    )
    monkeypatch.setattr(
        mcp_server,
        "_planner",
        lambda terms, refresh=False: (planner, {"202620": snapshot}, audit, {}, "202620"),
    )
    payload = mcp_server.draft_payload("202620", required_crns=["999"])
    assert payload["ok"] is False
    assert "pinned" in payload["error"]
    assert payload["draft"]["pinned"][0]["outcome"] == "unavailable"


def _audit_with_course(code: str, label: str = "Python"):
    from eagle_flightplan.audit.models import (
        Audit,
        CourseRef,
        GroupKind,
        RequirementBlock,
        RequirementGroup,
    )

    subject, number = code.split()
    return Audit(
        groups=[
            RequirementGroup(
                id="RA1",
                title="Major",
                kind=GroupKind.MAJOR,
                blocks=[
                    RequirementBlock(
                        label=label,
                        pick_n_of=1,
                        options=[CourseRef(subject=subject, number=number)],
                    )
                ],
            )
        ]
    )


def test_candidates_compact_and_limit_keep_default_shape(monkeypatch) -> None:
    snapshot = TermSnapshot(
        meta=SnapshotMeta(term="202620", fetched_at=datetime.now(UTC), source="cache"),
        sections=[
            Section(
                crn=str(index),
                term="202620",
                subject="CYS",
                number="1000",
                section=f"0{index}",
                title="Python",
                credits_low=3.0,
                seats_available=5,
                open=True,
            )
            for index in range(1, 5)
        ],
    )
    audit = _audit_with_course("CYS 1000")

    class FakeAuditClient:
        def audit(self):
            return audit

    monkeypatch.setattr(mcp_server, "AuditClient", FakeAuditClient)
    monkeypatch.setattr(mcp_server, "_snapshot", lambda term, refresh=False: snapshot)

    compact = mcp_server.candidates_payload("202620", compact=True, limit=2)
    row = compact["requirements"][0]
    assert row["count"] == 4 and row["returned"] == 2 and row["truncated"] is True
    assert "meetings" not in row["candidates"][0]
    assert "faculty" not in row["candidates"][0]

    full = mcp_server.candidates_payload("202620")
    assert full["requirements"][0]["returned"] == 4
    assert "meetings" in full["requirements"][0]["candidates"][0]


def test_remaining_marks_shared_title_and_options(monkeypatch) -> None:
    from eagle_flightplan.audit.models import (
        Audit,
        CourseRef,
        GroupKind,
        RequirementBlock,
        RequirementGroup,
    )

    def block():
        return RequirementBlock(
            label="Python Programming",
            pick_n_of=1,
            options=[CourseRef(subject="COS", number="2005")],
        )

    audit = Audit(
        groups=[
            RequirementGroup(
                id="A", title="Major: Cybersecurity", kind=GroupKind.MAJOR, blocks=[block()]
            ),
            RequirementGroup(
                id="B", title="Major: Computer Science", kind=GroupKind.MAJOR, blocks=[block()]
            ),
        ]
    )

    class FakeAuditClient:
        def audit(self):
            return audit

    monkeypatch.setattr(mcp_server, "AuditClient", FakeAuditClient)
    payload = mcp_server.remaining_payload()
    by_id = {row["id"]: row for row in payload["requirements"]}
    assert len(by_id) == 2
    assert all(len(row["shared_title_with"]) == 1 for row in by_id.values())


def test_conflicts_payload_has_grid_legend(monkeypatch) -> None:
    a = Section(
        crn="1", term="202620", subject="CYS", number="1000", section="01",
        title="A", credits_low=3.0, seats_available=5, open=True,
        meetings=[Meeting(day=Weekday.MON, start="10:00", end="11:00")],
    )
    b = Section(
        crn="2", term="202620", subject="BIB", number="2000", section="01",
        title="B", credits_low=3.0, seats_available=5, open=True,
        meetings=[Meeting(day=Weekday.MON, start="10:30", end="11:30")],
    )
    snapshot = TermSnapshot(
        meta=SnapshotMeta(term="202620", fetched_at=datetime.now(UTC), source="cache"),
        sections=[a, b],
    )
    monkeypatch.setattr(mcp_server, "_snapshot", lambda term, refresh=False: snapshot)
    payload = mcp_server.conflicts_payload("202620", ["1", "2"])
    assert payload["grid_legend"]["BIB 2000/CYS 1000"] == ["1", "2"]


def test_section_payload_semantics_notes() -> None:
    payload = mcp_server.section_payload(sample_section(seats=4, open_section=True))
    assert "credits_semantics" in payload
    assert "part_of_term_note" in payload


def test_session_reports_degraded_without_ordering_a_stop(monkeypatch, tmp_path) -> None:
    state = tmp_path / "state.json"
    state.write_text("{}")
    monkeypatch.setattr(mcp_server, "STATE_PATH", state)
    monkeypatch.setattr(mcp_server.auth, "state_age_seconds", lambda: 10.0)
    monkeypatch.setattr(mcp_server.auth, "load_state", lambda: {"cookies": []})
    monkeypatch.setattr(mcp_server.auth, "session_valid", lambda *args, **kwargs: False)
    payload = mcp_server.session_status_payload()
    assert payload["authenticated"] is False
    assert payload["degraded"] is True
    assert "try unw_profile/unw_terms" in payload["read_hint"]
    assert "before any Banner plan write" in payload["write_hint"]
    assert "try unw_profile" in payload["message"]
