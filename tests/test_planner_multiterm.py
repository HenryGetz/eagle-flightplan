"""Multi-term roadmap, intake, delivery and no-catalog honesty tests."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from eagle_flightplan import mcp_server
from eagle_flightplan.audit.models import (
    Audit,
    CourseRef,
    GroupKind,
    RequirementBlock,
    RequirementGroup,
)
from eagle_flightplan.banner.search import SnapshotMeta, TermSnapshot
from eagle_flightplan.models import Instructor, Section
from eagle_flightplan.planner import (
    Constraints,
    NoCatalogError,
    Planner,
    PrereqProvider,
    RatingInfo,
    RatingProvider,
    delivery_class,
)
from eagle_flightplan.schedule import Meeting, Weekday


def section(
    crn: str,
    subject: str,
    number: str,
    term: str,
    *,
    credits: float = 4.0,
    instructor: str = "Smith, James",
    days: tuple[Weekday, ...] = (Weekday.MON, Weekday.WED),
    method: str = "Face to Face",
    campus: str = "Main Campus",
    scheduled: bool = True,
) -> Section:
    meetings = (
        [Meeting(day=day, start="10:00", end="10:50", kind="Class") for day in days]
        if scheduled
        else []
    )
    return Section(
        crn=crn,
        term=term,
        subject=subject,
        number=number,
        section="01",
        subject_course=f"{subject}{number}",
        title=f"{subject} {number}",
        credits_low=credits,
        capacity=25,
        enrollment=0,
        seats_available=25,
        open=True,
        campus=campus,
        instructional_method=method,
        faculty=[Instructor(name=instructor)],
        meetings=meetings,
    )


def snapshot(term: str, sections: list[Section]) -> TermSnapshot:
    return TermSnapshot(
        meta=SnapshotMeta(term=term, fetched_at=datetime.now(UTC)),
        sections=sections,
    )


def audit_with(blocks: list[RequirementBlock]) -> Audit:
    return Audit(
        groups=[
            RequirementGroup(
                id="RA9",
                title="Major: Cybersecurity",
                kind=GroupKind.MAJOR,
                blocks=blocks,
            )
        ]
    )


def ratings(tmp_path) -> RatingProvider:
    return RatingProvider(
        path=tmp_path / "ratings.json",
        lookup=lambda name: RatingInfo(name=name),
    )


def prereqs(tmp_path, table: dict[str, str] | None = None) -> PrereqProvider:
    lookup = table or {}
    return PrereqProvider(
        directory=tmp_path,
        fetcher=lambda s: lookup.get(s.number, "No prerequisite information available."),
    )


def planner_for(
    tmp_path,
    audit: Audit,
    snapshots: dict[str, TermSnapshot],
    *,
    terms_order: list[str],
    prereq_table: dict[str, str] | None = None,
) -> Planner:
    return Planner(
        audit,
        snapshots,
        ratings=ratings(tmp_path),
        prereqs=prereqs(tmp_path, prereq_table),
        terms_order=terms_order,
    )


# --------------------------------------------------------------------------- #
# delivery
# --------------------------------------------------------------------------- #
def test_delivery_class_reads_method_then_campus():
    assert delivery_class(section("1", "CYS", "1", "202620", method="Online")) == "online"
    assert delivery_class(section("1", "CYS", "1", "202620", method="Virtual")) == "online"
    assert delivery_class(section("1", "CYS", "1", "202620", method="Blended")) == "hybrid"
    assert (
        delivery_class(section("1", "CYS", "1", "202620", method="Face to Face"))
        == "in_person"
    )
    assert (
        delivery_class(section("1", "CYS", "1", "202620", method="", campus="Virtual"))
        == "online"
    )
    assert delivery_class(section("1", "CYS", "1", "202620", method="", campus="")) == "unknown"


def test_online_preference_rejects_in_person_and_vice_versa(tmp_path):
    audit = audit_with(
        [
            RequirementBlock(
                label="Any CYS",
                pick_n_of=1,
                options=[CourseRef(subject="CYS", number="4245", status="option")],
            )
        ]
    )
    snapshots = {
        "202710": snapshot(
            "202710",
            [
                section("100", "CYS", "4245", "202710", method="Face to Face"),
                section("200", "CYS", "4245", "202710", method="Online"),
            ],
        )
    }
    planner = planner_for(tmp_path, audit, snapshots, terms_order=["202710"])

    online = planner.draft("202710", Constraints(delivery="online"))
    assert [row.crn for row in online.selections] == ["200"]

    in_person = planner.draft("202710", Constraints(delivery="in_person"))
    assert [row.crn for row in in_person.selections] == ["100"]

    any_delivery = planner.draft("202710", Constraints())
    assert any_delivery.selections


def test_online_preference_with_no_match_reports_delivery_reason(tmp_path):
    audit = audit_with(
        [
            RequirementBlock(
                label="Any CYS",
                pick_n_of=1,
                options=[CourseRef(subject="CYS", number="4245", status="option")],
            )
        ]
    )
    snapshots = {
        "202710": snapshot(
            "202710", [section("100", "CYS", "4245", "202710", method="Face to Face")]
        )
    }
    planner = planner_for(tmp_path, audit, snapshots, terms_order=["202710"])
    draft = planner.draft("202710", Constraints(delivery="online"))
    assert draft.selections == []
    assert "delivery" in draft.unplaced_requirements[0].reason


# --------------------------------------------------------------------------- #
# TBA honesty
# --------------------------------------------------------------------------- #
def test_all_tba_sections_mark_time_constraints_unenforced(tmp_path):
    audit = audit_with(
        [
            RequirementBlock(
                label="Any CYS",
                pick_n_of=1,
                options=[CourseRef(subject="CYS", number="4245", status="option")],
            )
        ]
    )
    snapshots = {
        "202710": snapshot(
            "202710", [section("100", "CYS", "4245", "202710", scheduled=False)]
        )
    }
    planner = planner_for(tmp_path, audit, snapshots, terms_order=["202710"])
    draft = planner.draft(
        "202710", Constraints(avoid_days=[Weekday.FRI], earliest="08:00", latest="11:00")
    )
    assert [row.crn for row in draft.selections] == ["100"]
    assert draft.all_selections_unscheduled is True
    assert draft.constraints_unenforced == ["avoid_days", "earliest", "latest"]
    assert any("could not be enforced" in warning for warning in draft.warnings)


def test_published_schedule_does_not_mark_constraints_unenforced(tmp_path):
    audit = audit_with(
        [
            RequirementBlock(
                label="Any CYS",
                pick_n_of=1,
                options=[CourseRef(subject="CYS", number="4245", status="option")],
            )
        ]
    )
    snapshots = {
        "202710": snapshot("202710", [section("100", "CYS", "4245", "202710")])
    }
    planner = planner_for(tmp_path, audit, snapshots, terms_order=["202710"])
    draft = planner.draft("202710", Constraints(avoid_days=[Weekday.FRI]))
    assert draft.constraints_unenforced == []
    assert draft.all_selections_unscheduled is False


# --------------------------------------------------------------------------- #
# assume_completed
# --------------------------------------------------------------------------- #
def test_standalone_spring_draft_does_not_duplicate_a_fall_planned_course(tmp_path):
    audit = audit_with(
        [
            RequirementBlock(
                label="Python Programming",
                pick_n_of=1,
                options=[CourseRef(subject="COS", number="2005", status="option")],
            )
        ]
    )
    snapshots = {"202720": snapshot("202720", [section("22201", "COS", "2005", "202720")])}
    planner = planner_for(tmp_path, audit, snapshots, terms_order=["202720"])

    plain = planner.draft("202720", Constraints())
    assert [row.course for row in plain.selections] == ["COS 2005"]

    aware = planner.draft("202720", Constraints(assume_completed=["COS 2005"]))
    assert aware.selections == []
    assert aware.assumed_completed == ["COS 2005"]
    assert "already completed" in aware.unplaced_requirements[0].reason.lower()


def test_assume_completed_unblocks_a_dependent_course(tmp_path):
    audit = audit_with(
        [
            RequirementBlock(
                label="Advanced",
                pick_n_of=1,
                options=[CourseRef(subject="CYS", number="4855", status="option")],
            )
        ]
    )
    snapshots = {
        "202720": snapshot("202720", [section("1", "CYS", "4855", "202720", credits=2.0)])
    }
    planner = planner_for(
        tmp_path,
        audit,
        snapshots,
        terms_order=["202720"],
        prereq_table={"4855": "Prerequisites:PQ ( Course or Test: COS 2005 )"},
    )
    blocked = planner.draft("202720", Constraints())
    assert blocked.selections == []

    unblocked = planner.draft("202720", Constraints(assume_completed=["COS 2005"]))
    assert [row.course for row in unblocked.selections] == ["CYS 4855"]


# --------------------------------------------------------------------------- #
# roadmap structure
# --------------------------------------------------------------------------- #
def test_roadmap_plans_summer_only_when_asked(tmp_path):
    audit = audit_with(
        [
            RequirementBlock(
                label="Any CYS",
                pick_n_of=1,
                options=[CourseRef(subject="CYS", number="4245", status="option")],
            )
        ]
    )
    snapshots = {
        "202620": snapshot("202620", [section("1", "BIB", "1000", "202620")]),
        "202630": snapshot("202630", [section("2", "CYS", "4245", "202630")]),
    }
    planner = planner_for(tmp_path, audit, snapshots, terms_order=["202620", "202630"])

    without = planner.roadmap(["202620", "202630"])
    assert [row.term for row in without.terms] == ["202620"]

    with_summer = planner.roadmap(["202620", "202630"], include_summer=True)
    assert [row.term for row in with_summer.terms] == ["202620", "202630"]
    assert [s.course for s in with_summer.terms[1].draft.selections] == ["CYS 4245"]


def test_roadmap_marks_unpublished_term_no_catalog(tmp_path):
    audit = audit_with(
        [
            RequirementBlock(
                label="Any CYS",
                pick_n_of=1,
                options=[CourseRef(subject="CYS", number="4245", status="option")],
            )
        ]
    )
    snapshots = {
        "202710": snapshot("202710", [section("1", "BIB", "1000", "202710")]),
        "202810": snapshot("202810", []),
    }
    planner = planner_for(tmp_path, audit, snapshots, terms_order=["202710", "202810"])
    roadmap = planner.roadmap(["202710", "202810"])
    spring, unpublished = roadmap.terms
    assert spring.status == "planned"
    assert unpublished.status == "no_catalog"
    assert unpublished.draft is None
    assert "not published" in unpublished.notes[0]
    assert roadmap.terms_without_catalog == ["202810"]


def test_roadmap_applies_pins_to_the_start_term_only(tmp_path):
    audit = audit_with(
        [
            RequirementBlock(
                label="Theology",
                pick_n_of=1,
                options=[CourseRef(subject="BIB", number="4360", status="option")],
            )
        ]
    )
    snapshots = {
        "202710": snapshot("202710", [section("1", "BIB", "4360", "202710")]),
        "202720": snapshot("202720", [section("2", "BIB", "4360", "202720")]),
    }
    planner = planner_for(tmp_path, audit, snapshots, terms_order=["202710", "202720"])
    roadmap = planner.roadmap(
        ["202710", "202720"], Constraints(required_courses=["BIB 4360"])
    )
    first, second = roadmap.terms
    assert [pin.outcome for pin in first.draft.pinned] == ["placed"]
    assert second.draft.pinned == []
    assert second.draft.selections == []


def test_draft_of_unpublished_term_raises_no_catalog(tmp_path):
    planner = planner_for(
        tmp_path, audit_with([]), {"202810": snapshot("202810", [])}, terms_order=["202810"]
    )
    with pytest.raises(NoCatalogError, match="no published catalog"):
        planner.draft("202810", Constraints())


# --------------------------------------------------------------------------- #
# MCP payloads
# --------------------------------------------------------------------------- #
def _install_planner(monkeypatch, planner, snapshots, audit, current_term="202610"):
    monkeypatch.setattr(
        mcp_server,
        "_planner",
        lambda terms, refresh=False: (planner, snapshots, audit, {}, current_term),
    )


def test_roadmap_payload_forwards_include_summer(monkeypatch, tmp_path):
    audit = audit_with(
        [
            RequirementBlock(
                label="Any CYS",
                pick_n_of=1,
                options=[CourseRef(subject="CYS", number="4245", status="option")],
            )
        ]
    )
    snapshots = {
        "202710": snapshot("202710", [section("1", "BIB", "1000", "202710")]),
        "202720": snapshot("202720", [section("2", "BIB", "1001", "202720")]),
        "202730": snapshot("202730", [section("3", "CYS", "4245", "202730")]),
    }
    planner = planner_for(
        tmp_path, audit, snapshots, terms_order=["202710", "202720", "202730"]
    )
    _install_planner(monkeypatch, planner, snapshots, audit)

    payload = mcp_server.roadmap_payload(
        "202710", terms=["202710", "202730"], include_summer=True
    )
    assert [row["term"] for row in payload["planned"]] == ["202710", "202730"]
    assert [s["course"] for s in payload["planned"][1]["draft"]["selections"]] == ["CYS 4245"]

    without = mcp_server.roadmap_payload(
        "202710", terms=["202710", "202730"], include_summer=False
    )
    assert [row["term"] for row in without["planned"]] == ["202710"]
    assert any("include_summer=false" in risk for risk in without["risks"])


def test_roadmap_payload_reports_assumptions_and_missing_preferences(monkeypatch, tmp_path):
    audit = audit_with(
        [
            RequirementBlock(
                label="Any CYS",
                pick_n_of=1,
                options=[CourseRef(subject="CYS", number="4245", status="option")],
            )
        ]
    )
    snapshots = {"202710": snapshot("202710", [section("1", "CYS", "4245", "202710")])}
    planner = planner_for(tmp_path, audit, snapshots, terms_order=["202710"])
    _install_planner(monkeypatch, planner, snapshots, audit)

    default = mcp_server.roadmap_payload("202710", terms="202710")
    by_field = {row["field"]: row for row in default["assumptions"]}
    assert by_field["max_credits"]["source"] == "default"
    assert by_field["max_credits"]["value"] == 16
    assert by_field["avoid_days"]["source"] == "default"
    assert by_field["terms"]["source"] == "requested"
    missing = {row["id"] for row in default["preferences_missing"]}
    assert {"credit_load", "blocked_days", "delivery", "summer", "graduation_target"} <= missing
    assert "rmp_priority" in missing

    answered = mcp_server.roadmap_payload(
        "202710",
        terms="202710",
        max_credits=12,
        min_credits=12,
        avoid_days=["Sat"],
        earliest="09:00",
        latest="15:00",
        delivery="online",
        include_summer=True,
        assume_completed=["COS 2005"],
        target_term="202710",
    )
    by_field = {row["field"]: row for row in answered["assumptions"]}
    assert by_field["max_credits"] == {
        "field": "max_credits",
        "value": 12.0,
        "source": "requested",
        "note": "16 is the server default; confirm the student's real load",
    }
    assert by_field["avoid_days"]["source"] == "requested"
    assert by_field["assume_completed"]["source"] == "requested"
    assert answered["constraints"]["delivery"] == "online"
    assert answered["constraints"]["assume_completed"] == ["COS 2005"]
    assert answered["graduation_target"]["target_term"] == "202710"
    assert answered["graduation_target"]["feasible"] in {True, False}


def test_roadmap_payload_target_after_horizon_is_not_demonstrated(monkeypatch, tmp_path):
    audit = audit_with(
        [
            RequirementBlock(
                label="Any CYS",
                min_credits=8,
                applied_credits=0,
                options=[CourseRef(subject="CYS", number="4245", status="option")],
            )
        ]
    )
    snapshots = {
        "202710": snapshot("202710", [section("1", "CYS", "4245", "202710")]),
        "202720": snapshot("202720", [section("2", "BIB", "1001", "202720")]),
    }
    planner = planner_for(tmp_path, audit, snapshots, terms_order=["202710", "202720"])
    _install_planner(monkeypatch, planner, snapshots, audit)

    payload = mcp_server.roadmap_payload(
        "202710", terms=["202710", "202720"], target_term="202820"
    )
    target = payload["graduation_target"]
    assert target["feasible"] is None
    assert "beyond the planned horizon" in target["notes"][-1]


def test_roadmap_payload_skips_unpublished_term(monkeypatch, tmp_path):
    audit = audit_with(
        [
            RequirementBlock(
                label="Any CYS",
                pick_n_of=1,
                options=[CourseRef(subject="CYS", number="4245", status="option")],
            )
        ]
    )
    snapshots = {
        "202710": snapshot("202710", [section("1", "CYS", "4245", "202710")]),
        "202810": snapshot("202810", []),
    }
    planner = planner_for(tmp_path, audit, snapshots, terms_order=["202710", "202810"])
    _install_planner(monkeypatch, planner, snapshots, audit)

    payload = mcp_server.roadmap_payload("202710", terms=["202710", "202810"])
    rows = {row["term"]: row for row in payload["planned"]}
    assert rows["202810"]["status"] == "no_catalog"
    assert rows["202810"]["draft"] is None
    assert payload["terms_without_catalog"] == ["202810"]
    assert any("no published catalog" in risk for risk in payload["risks"])
    assert not any("placed 0 credits" in risk and "202810" in risk for risk in payload["risks"])


def test_draft_payload_unpublished_term_is_no_catalog(monkeypatch, tmp_path):
    audit = audit_with(
        [
            RequirementBlock(
                label="Any CYS",
                pick_n_of=1,
                options=[CourseRef(subject="CYS", number="4245", status="option")],
            )
        ]
    )
    snapshots = {"202810": snapshot("202810", [])}
    planner = planner_for(tmp_path, audit, snapshots, terms_order=["202810"])
    _install_planner(monkeypatch, planner, snapshots, audit)

    result = mcp_server._safe(mcp_server.draft_payload, "202810")
    assert result["ok"] is False
    assert result["error_type"] == "NoCatalog"
    assert "no published catalog" in result["error"]
    assert "unpublished" in result["hint"].lower() or "zero sections" in result["hint"]


def test_draft_payload_exposes_delivery_and_unenforced_constraints(monkeypatch, tmp_path):
    audit = audit_with(
        [
            RequirementBlock(
                label="Any CYS",
                pick_n_of=1,
                options=[CourseRef(subject="CYS", number="4245", status="option")],
            )
        ]
    )
    snapshots = {
        "202710": snapshot(
            "202710",
            [
                section(
                    "100",
                    "CYS",
                    "4245",
                    "202710",
                    method="Online",
                    campus="Virtual",
                    scheduled=False,
                )
            ],
        )
    }
    planner = planner_for(tmp_path, audit, snapshots, terms_order=["202710"])
    _install_planner(monkeypatch, planner, snapshots, audit)

    payload = mcp_server.draft_payload("202710", avoid_days=["F"], delivery="online")
    body = payload["draft"]
    assert body["selections"][0]["delivery"] == "online"
    assert body["selections"][0]["instructional_method"] == "Online"
    assert body["selections"][0]["campus"] == "Virtual"
    assert body["constraints_unenforced"] == ["avoid_days"]
    assert body["all_selections_unscheduled"] is True
    assert body["tba_crns"] == ["100"]


# --------------------------------------------------------------------------- #
# intake
# --------------------------------------------------------------------------- #
def _fake_profile() -> dict:
    return {
        "majors": ["Computer Science", "Cybersecurity"],
        "minors": ["Ministry"],
        "programs": ["Computer Science-BS"],
        "catalog_year": "2024-2025 (Fall 2024)",
        "classification": "Junior",
        "credits_earned": 86.0,
        "credits_in_progress": 15.0,
        "anticipated_graduation": "Spring 2028",
        "percent_complete": 71,
        "academic_standing": "Good Standing",
        "holds": None,
        "holds_note": "null means unknown",
        "advisor_names": "Advisor One",
    }


def _fake_remaining() -> dict:
    return {
        "count": 8,
        "known_credits_remaining": 22.0,
        "unknown_credits_count": 3,
        "requirements": [],
    }


def _fake_window(term: str | None = None) -> dict:
    return {
        "ok": True,
        "current_term": "202610",
        "next_term": "202620",
        "timezone": "America/Chicago",
        "windows": [
            {
                "term": "202620",
                "term_label": "Spring 2027",
                "eligible": False,
                "start": "2026-11-01T08:30:00-05:00",
                "end": "2027-01-10T23:59:00-06:00",
                "countdown": "opens later",
                "parse_status": "parsed",
                "registration_standing": "Good Standing",
            }
        ],
    }


def test_intake_payload_returns_facts_questions_and_defaults(monkeypatch):
    monkeypatch.setattr(mcp_server, "profile_payload", _fake_profile)
    monkeypatch.setattr(mcp_server, "remaining_payload", _fake_remaining)
    monkeypatch.setattr(mcp_server, "registration_window_payload", _fake_window)

    payload = mcp_server.planning_intake_payload("202620")
    assert payload["ok"] is True
    assert payload["read_only"] is True
    assert payload["writes_performed"] == []
    assert payload["audit_facts"]["majors"] == ["Computer Science", "Cybersecurity"]
    assert payload["audit_facts"]["known_credits_remaining"] == 22.0
    assert payload["term_facts"]["next_term"] == "202620"
    assert payload["term_facts"]["eligible"]["202620"]["eligible"] is False

    ids = [row["id"] for row in payload["questions"]]
    for expected in (
        "credit_load",
        "blocked_days",
        "time_window",
        "delivery",
        "rmp_priority",
        "summer",
        "graduation_target",
        "work_hours",
        "program_confirmation",
        "retakes",
        "prior_credit",
        "holds",
        "avoid_instructors",
    ):
        assert expected in ids, expected
    question = {row["id"]: row for row in payload["questions"]}
    assert question["credit_load"]["default"] == {"min_credits": 0, "max_credits": 16}
    assert question["delivery"]["options"] == ["any", "online", "in_person"]
    assert "Spring 2028" in question["graduation_target"]["default"]
    assert all(row["prompt"] and row["why"] for row in payload["questions"])

    fields = {row["field"]: row for row in payload["defaults_used"]}
    assert fields["max_credits"]["value"] == 16
    assert fields["avoid_days"]["value"] == []
    assert "unw_semester_plan" in payload["next_step"]


def test_intake_payload_reports_section_errors_without_hiding_the_rest(monkeypatch):
    def boom():
        raise RuntimeError("degree works down")

    monkeypatch.setattr(mcp_server, "profile_payload", boom)
    monkeypatch.setattr(mcp_server, "remaining_payload", _fake_remaining)
    monkeypatch.setattr(mcp_server, "registration_window_payload", _fake_window)

    payload = mcp_server.planning_intake_payload(None)
    assert payload["ok"] is False
    assert "degree works down" in payload["source_errors"]["profile"]
    assert payload["term_facts"]["next_term"] == "202620"
    assert payload["question_count"] == len(payload["questions"])


def test_intake_tool_schema_and_readonly_description() -> None:
    import asyncio

    tools = {tool.name: tool for tool in asyncio.run(mcp_server.mcp.list_tools())}
    assert "unw_planning_intake" in tools
    properties = tools["unw_planning_intake"].input_schema["properties"]
    assert "term" in properties and "include_audit" in properties
    assert "READ-ONLY" in tools["unw_planning_intake"].description
    roadmap = tools["unw_roadmap"].input_schema["properties"]
    for key in (
        "min_credits",
        "avoid_days",
        "earliest",
        "latest",
        "avoid_instructors",
        "exclude_arrangement",
        "required_courses",
        "required_crns",
        "hours",
        "assume_completed",
        "delivery",
        "target_term",
        "include_summer",
    ):
        assert key in roadmap, key
