"""Roadmap tests: rolling requirements to the term where they are offered."""

from __future__ import annotations

from datetime import datetime, timezone

from flightplan.audit.models import (
    Audit,
    CourseRef,
    GroupKind,
    RequirementBlock,
    RequirementGroup,
)
from flightplan.banner.search import SnapshotMeta, TermSnapshot
from flightplan.models import Instructor, Section, SectionAttribute
from flightplan.planner import Constraints, Planner, PrereqProvider, RatingInfo, RatingProvider
from flightplan.schedule import Meeting, Weekday


def section(
    crn: str,
    subject: str,
    number: str,
    term: str,
    *,
    credits: float = 4.0,
    instructor: str = "Smith, James",
    attributes: tuple[str, ...] = (),
    days: tuple[Weekday, ...] = (Weekday.MON, Weekday.WED),
) -> Section:
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
        faculty=[Instructor(name=instructor)],
        attributes=[SectionAttribute(code=code, description=code) for code in attributes],
        meetings=[Meeting(day=day, start="10:00", end="10:50", kind="Class") for day in days],
    )


def snapshot(term: str, sections: list[Section]) -> TermSnapshot:
    return TermSnapshot(
        meta=SnapshotMeta(term=term, fetched_at=datetime.now(timezone.utc)),
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
    table = {
        "Smith, James": RatingInfo(name="Smith, James", rating=4.0, num_ratings=10),
        "Terry, Justin": RatingInfo(name="Terry, Justin", rating=3.0, num_ratings=5),
    }
    return RatingProvider(path=tmp_path / "ratings.json", lookup=lambda name: table.get(name, RatingInfo(name=name)))


def prereqs(tmp_path) -> PrereqProvider:
    return PrereqProvider(
        directory=tmp_path, fetcher=lambda section: "No prerequisite information available."
    )


def test_fall_only_course_rolls_to_fall_term(tmp_path):
    audit = audit_with([
        RequirementBlock(
            label="Current Practices",
            pick_n_of=1,
            options=[CourseRef(subject="CYS", number="4245", status="option")],
        )
    ])
    snapshots = {
        "202620": snapshot("202620", []),
        "202710": snapshot("202710", [section("9001", "CYS", "4245", "202710")]),
    }
    planner = Planner(
        audit, snapshots, ratings=ratings(tmp_path), prereqs=prereqs(tmp_path),
        terms_order=["202620", "202710"],
    )
    roadmap = planner.roadmap(["202620", "202710"])
    spring, fall = roadmap.terms
    assert spring.draft is not None and spring.draft.selections == []
    assert any("is offered in" in item.reason for item in spring.draft.unplaced_requirements)
    assert [s.crn for s in fall.draft.selections] == ["9001"]
    assert roadmap.final_remaining == []


def test_requirement_consumed_once_across_terms(tmp_path):
    audit = audit_with([
        RequirementBlock(
            label="Theology",
            pick_n_of=1,
            options=[CourseRef(subject="BIB", number="4360", status="option")],
        )
    ])
    snapshots = {
        "202620": snapshot("202620", [section("1", "BIB", "4360", "202620")]),
        "202710": snapshot("202710", [section("2", "BIB", "4360", "202710")]),
    }
    planner = Planner(
        audit, snapshots, ratings=ratings(tmp_path), prereqs=prereqs(tmp_path),
        terms_order=["202620", "202710"],
    )
    roadmap = planner.roadmap(["202620", "202710"])
    assert [s.crn for s in roadmap.terms[0].draft.selections] == ["1"]
    assert roadmap.terms[1].draft.selections == []
    assert roadmap.terms[1].remaining == []


def test_roadmap_reports_missing_catalog_term(tmp_path):
    audit = audit_with([
        RequirementBlock(
            label="Theology",
            pick_n_of=1,
            options=[CourseRef(subject="BIB", number="4360", status="option")],
        )
    ])
    planner = Planner(
        audit,
        {"202620": snapshot("202620", [])},
        ratings=ratings(tmp_path),
        prereqs=prereqs(tmp_path),
        terms_order=["202620", "202810"],
    )
    roadmap = planner.roadmap(["202620", "202810"])
    assert roadmap.terms[1].status == "no_catalog"
    assert roadmap.terms[1].notes
    assert "Theology" in roadmap.final_remaining[0]


def test_roadmap_respects_prereq_ordering(tmp_path):
    audit = audit_with([
        RequirementBlock(
            label="Advanced",
            pick_n_of=1,
            options=[CourseRef(subject="CYS", number="4855", status="option")],
        ),
        RequirementBlock(
            label="Foundation",
            pick_n_of=1,
            options=[CourseRef(subject="CYS", number="3065", status="option")],
        ),
    ])
    lookup = PrereqProvider(
        directory=tmp_path,
        fetcher=lambda s: (
            "Prerequisites:PQ ( Course or Test: CYS 3065 )"
            if s.number == "4855"
            else "No prerequisite information available."
        ),
    )
    snapshots = {
        "202620": snapshot("202620", [section("100", "CYS", "3065", "202620", credits=4.0)]),
        "202710": snapshot(
            "202710",
            [
                section("200", "CYS", "4855", "202710", credits=2.0),
                section("201", "CYS", "3065", "202710", credits=4.0),
            ],
        ),
    }
    planner = Planner(
        audit, snapshots, ratings=ratings(tmp_path), prereqs=lookup,
        terms_order=["202620", "202710"],
    )
    roadmap = planner.roadmap(["202620", "202710"])
    spring_codes = [s.course for s in roadmap.terms[0].draft.selections]
    fall_codes = [s.course for s in roadmap.terms[1].draft.selections]
    assert "CYS 4855" not in spring_codes
    assert "CYS 4855" in fall_codes
    assert roadmap.final_remaining == []


def test_constraints_are_carried_into_each_term(tmp_path):
    audit = audit_with([
        RequirementBlock(
            label="Any",
            pick_n_of=1,
            options=[CourseRef(subject="BIB", number="4360", status="option")],
        )
    ])
    snapshots = {
        "202620": snapshot("202620", [section("1", "BIB", "4360", "202620", days=(Weekday.SAT,))]),
        "202710": snapshot("202710", [section("2", "BIB", "4360", "202710", days=(Weekday.MON,))]),
    }
    planner = Planner(
        audit, snapshots, ratings=ratings(tmp_path), prereqs=prereqs(tmp_path),
        terms_order=["202620", "202710"],
    )
    roadmap = planner.roadmap(["202620", "202710"], Constraints(avoid_days=[Weekday.SAT]))
    assert roadmap.terms[0].draft.selections == []
    assert [s.crn for s in roadmap.terms[1].draft.selections] == ["2"]


def test_wildcard_pool_is_not_replanned_every_term(tmp_path):
    audit = audit_with([
        RequirementBlock(
            label="DAL *",
            pick_n_of=1,
            status="not_met",
            options=[CourseRef(subject="DAL", number="", status="option")],
        )
    ])
    snapshots = {
        "202620": snapshot("202620", [section("1", "DAL", "1010", "202620")]),
        "202710": snapshot("202710", [section("2", "DAL", "1010", "202710")]),
    }
    planner = Planner(
        audit,
        snapshots,
        ratings=ratings(tmp_path),
        prereqs=prereqs(tmp_path),
        terms_order=["202620", "202710"],
    )
    roadmap = planner.roadmap(["202620", "202710"])
    assert [s.course for s in roadmap.terms[0].draft.selections] == ["DAL 1010"]
    assert roadmap.terms[1].draft.selections == []
    assert roadmap.final_remaining == []
