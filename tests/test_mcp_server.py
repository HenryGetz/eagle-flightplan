"""Offline tests for the MCP server surface and its pure helpers."""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

import pytest

from unw_planner import mcp_server
from unw_planner.banner.plan import Plan, PlanError, PlanNotFoundError
from unw_planner.banner.search import SnapshotMeta, TermSnapshot
from unw_planner.banner.ssb import TermEligibility
from unw_planner.models import Instructor, Section, SectionAttribute
from unw_planner.planner import RatingInfo
from unw_planner.schedule import Meeting, Weekday

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
        faculty=[Instructor(name="Smith, James", email="jlsmith2@unwsp.edu", primary=True)],
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
            fetched_at=datetime.now(timezone.utc),
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
    assert len(tools) == 19
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
