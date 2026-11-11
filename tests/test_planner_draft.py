"""Offline planner tests: synthetic snapshots, injected ratings and prereqs."""

from __future__ import annotations

from datetime import datetime, time, timezone


from unw_planner.audit.models import Audit, CourseRef, GroupKind, RequirementBlock, RequirementGroup
from unw_planner.banner.search import SnapshotMeta, TermSnapshot
from unw_planner.models import Instructor, Section, SectionAttribute
from unw_planner.planner import (
    Constraints,
    Planner,
    PrereqProvider,
    RatingInfo,
    RatingProvider,
    parse_prerequisites,
)
from unw_planner.schedule import Meeting, Weekday, find_conflicts


def section(
    crn: str,
    subject: str = "CYS",
    number: str = "4245",
    *,
    title: str | None = None,
    section_no: str = "01",
    credits: float | None = 4.0,
    instructor: str | None = "Smith, James",
    days: tuple[Weekday, ...] = (Weekday.MON, Weekday.WED),
    start: str = "10:00",
    end: str = "10:50",
    attributes: tuple[str, ...] = (),
    open: bool = True,
    seats: int | None = 20,
    term: str = "202620",
) -> Section:
    meetings = [
        Meeting(day=day, start=start, end=end, location="RIL R1332", kind="Class")
        for day in days
    ] if days else []
    return Section(
        crn=crn,
        term=term,
        subject=subject,
        number=number,
        section=section_no,
        subject_course=f"{subject}{number}",
        title=title or f"{subject} {number}",
        credits_low=credits,
        capacity=seats,
        enrollment=0,
        seats_available=seats,
        open=open,
        faculty=[Instructor(name=instructor)] if instructor else [],
        attributes=[SectionAttribute(code=code, description=code) for code in attributes],
        meetings=meetings,
    )


def snapshot(term: str, sections: list[Section]) -> TermSnapshot:
    return TermSnapshot(
        meta=SnapshotMeta(term=term, fetched_at=datetime.now(timezone.utc)),
        sections=sections,
    )


def audit_with(blocks: list[RequirementBlock], kind: GroupKind = GroupKind.MAJOR) -> Audit:
    return Audit(
        student_name="Test Student",
        groups=[
            RequirementGroup(
                id="RA1",
                title="Major: Cybersecurity" if kind is GroupKind.MAJOR else "Core",
                kind=kind,
                blocks=blocks,
            )
        ],
    )


RATINGS = {
    "Smith, James": RatingInfo(name="Smith, James", rating=4.0, num_ratings=10, source="rmp"),
    "Terry, Justin": RatingInfo(name="Terry, Justin", rating=2.0, num_ratings=8, source="rmp"),
}


def ratings(tmp_path) -> RatingProvider:
    return RatingProvider(
        path=tmp_path / "ratings.json",
        lookup=lambda name: RATINGS.get(name, RatingInfo(name=name)),
    )


def no_prereqs(tmp_path) -> PrereqProvider:
    return PrereqProvider(directory=tmp_path, fetcher=lambda section: "No prerequisite information available.")


def test_rated_instructor_beats_unrated(tmp_path):
    sections = [
        section("1", instructor="Smith, James"),
        section("2", section_no="02", instructor="Nobody, New"),
    ]
    snap = snapshot("202620", sections)
    audit = audit_with([
        RequirementBlock(label="Course X", pick_n_of=1, options=[CourseRef(subject="CYS", number="4245")])
    ])
    planner = Planner(audit, {"202620": snap}, ratings=ratings(tmp_path), prereqs=no_prereqs(tmp_path))
    draft = planner.draft("202620")
    assert [s.crn for s in draft.selections] == ["1"]
    assert draft.selections[0].rating.rated is True


def test_low_n_rating_is_downweighted(tmp_path):
    sections = [
        section("1", instructor="One Rating, Prof"),   # 5.0 with n=1 -> effective 4.25
        section("2", section_no="02", instructor="Solid, Prof"),  # 4.3 with n=10
    ]
    lookup = {
        "One Rating, Prof": RatingInfo(name="One Rating, Prof", rating=5.0, num_ratings=1),
        "Solid, Prof": RatingInfo(name="Solid, Prof", rating=4.3, num_ratings=10),
    }
    provider = RatingProvider(path="/tmp/unw-test-ratings.json", lookup=lookup.__getitem__)
    audit = audit_with([
        RequirementBlock(label="Course X", pick_n_of=1, options=[CourseRef(subject="CYS", number="4245")])
    ])
    draft = Planner(
        audit,
        {"202620": snapshot("202620", sections)},
        ratings=provider,
        prereqs=no_prereqs(tmp_path),
    ).draft("202620")
    assert draft.selections[0].crn == "2"


def test_conflict_elimination_picks_higher_rated(tmp_path):
    sections = [
        section("1", title="Morning A", instructor="Terry, Justin", start="10:00", end="11:00"),
        section("2", section_no="02", title="Morning B", instructor="Smith, James", start="10:30", end="11:30"),
    ]
    audit = audit_with([
        RequirementBlock(label="Course X", pick_n_of=1, options=[CourseRef(subject="CYS", number="4245")])
    ])
    draft = Planner(
        audit,
        {"202620": snapshot("202620", sections)},
        ratings=ratings(tmp_path),
        prereqs=no_prereqs(tmp_path),
    ).draft("202620")
    assert [s.crn for s in draft.selections] == ["2"]
    assert find_conflicts([s.to_schedule_section() for s in [sections[1]]]) == []


def test_max_credits_trimming(tmp_path):
    sections = [
        section("1", number="4245", credits=4.0, instructor="Smith, James"),
        section("2", number="4466", credits=4.0, instructor="Terry, Justin"),
    ]
    audit = audit_with([
        RequirementBlock(label="A", pick_n_of=1, options=[CourseRef(subject="CYS", number="4245")]),
        RequirementBlock(label="B", pick_n_of=1, options=[CourseRef(subject="CYS", number="4466")]),
    ])
    draft = Planner(
        audit,
        {"202620": snapshot("202620", sections)},
        ratings=ratings(tmp_path),
        prereqs=no_prereqs(tmp_path),
    ).draft("202620", Constraints(max_credits=4))
    assert draft.total_credits == 4
    assert len(draft.selections) == 1
    assert any("budget" in item.reason for item in draft.unplaced_requirements)


def test_prereq_rejection_leaves_requirement_unplaced(tmp_path):
    sections = [section("1", number="4855", credits=2.0, instructor="Smith, James")]
    audit = audit_with([
        RequirementBlock(label="Capstone", pick_n_of=1, options=[CourseRef(subject="CYS", number="4855")])
    ])
    provider = PrereqProvider(
        directory=tmp_path,
        fetcher=lambda section: (
            "Prerequisites:PQ for CYS4855 ( Course or Test: MGT: Management 2271 "
            "Minimum Grade of C- May not be taken concurrently. ) or "
            "( Course or Test: CYS: Cyber Security 3065 Minimum Grade of C- "
            "May not be taken concurrently. )"
        ),
    )
    draft = Planner(
        audit,
        {"202620": snapshot("202620", sections)},
        ratings=ratings(tmp_path),
        prereqs=provider,
    ).draft("202620")
    assert draft.selections == []
    assert "MGT 2271" in draft.unplaced_requirements[0].reason


def test_prereq_met_allows_placement(tmp_path):
    sections = [section("1", number="4855", credits=2.0, instructor="Smith, James")]
    audit = audit_with([
        RequirementBlock(label="Capstone", pick_n_of=1, options=[CourseRef(subject="CYS", number="4855")])
    ])
    planner = Planner(
        audit,
        {"202620": snapshot("202620", sections)},
        ratings=ratings(tmp_path),
        prereqs=no_prereqs(tmp_path),
    )
    planner.passed.add("CYS 3065")
    draft = planner.draft("202620")
    assert [s.crn for s in draft.selections] == ["1"]


def test_already_satisfied_requirement_is_never_suggested(tmp_path):
    sections = [section("1", instructor="Smith, James")]
    met = RequirementBlock(
        label="Information Literacy",
        status="met",
        options=[CourseRef(subject="CYS", number="1000")],
    )
    fresh = RequirementBlock(
        label="Python Programming",
        min_credits=None,
        pick_n_of=1,
        options=[CourseRef(subject="CYS", number="4245")],
    )
    audit = audit_with([met, fresh])
    draft = Planner(
        audit,
        {"202620": snapshot("202620", sections)},
        ratings=ratings(tmp_path),
        prereqs=no_prereqs(tmp_path),
    ).draft("202620")
    assert [s.requirement for s in draft.selections] == ["Python Programming"]


def test_attribute_target_and_zero_hit_reason(tmp_path):
    sections = [
        section("1", subject="LIT", number="2157", credits=2.0, attributes=("CCUL",), instructor="Fox, Rebecca"),
    ]
    audit = audit_with([
        RequirementBlock(label="CCUL", pick_n_of=1, options=[CourseRef(subject="", number="", satisfies_notes="ATTRIBUTE = CCUL")]),
        RequirementBlock(label="TPHI", pick_n_of=1, options=[CourseRef(subject="", number="", satisfies_notes="ATTRIBUTE = TPHI")]),
    ], kind=GroupKind.GENED)
    lookup = {"Fox, Rebecca": RatingInfo(name="Fox, Rebecca", rating=4.5, num_ratings=8)}
    draft = Planner(
        audit,
        {"202620": snapshot("202620", sections)},
        ratings=RatingProvider(path="/tmp/unw-test-ratings.json", lookup=lookup.__getitem__),
        prereqs=no_prereqs(tmp_path),
    ).draft("202620")
    assert [s.course for s in draft.selections] == ["LIT 2157"]
    reasons = {item.requirement: item.reason for item in draft.unplaced_requirements}
    assert "TPHI" in reasons["TPHI"]
    assert "zero sections" in reasons["TPHI"]


def test_prereq_parser_boolean_semantics():
    expression = parse_prerequisites(
        "Prerequisites:PQ for X ( Course or Test: COS: Computer Science 2015 Minimum Grade of D- ) "
        "or ( Course or Test: COS 2112 Minimum Grade of D- )"
    )
    assert expression.satisfied_by({"COS 2015"}) is True
    assert expression.satisfied_by({"COS 2112"}) is True
    assert expression.satisfied_by(set()) is False
    both = parse_prerequisites(
        "Prerequisites:PQ ( Course or Test: LDR 1011 ) and ( Course or Test: LDR 2112 )"
    )
    assert both.satisfied_by({"LDR 1011"}) is False
    assert both.satisfied_by({"LDR 1011", "LDR 2112"}) is True


def test_avoid_days_and_window_constraints(tmp_path):
    sections = [
        section("1", instructor="Smith, James", days=(Weekday.MON,)),
        section("2", section_no="02", instructor="Smith, James", days=(Weekday.SAT,)),
    ]
    audit = audit_with([
        RequirementBlock(label="Any", pick_n_of=1, options=[CourseRef(subject="CYS", number="4245")])
    ])
    draft = Planner(
        audit,
        {"202620": snapshot("202620", sections)},
        ratings=ratings(tmp_path),
        prereqs=no_prereqs(tmp_path),
    ).draft("202620", Constraints(avoid_days=[Weekday.SAT]))
    assert [s.crn for s in draft.selections] == ["1"]

    narrow = Planner(
        audit,
        {"202620": snapshot("202620", sections)},
        ratings=ratings(tmp_path),
        prereqs=no_prereqs(tmp_path),
    ).draft("202620", Constraints(earliest=time(11, 0)))
    assert narrow.selections == []



def test_verify_draft_flags_conflicts_and_missing_crns(tmp_path):
    from unw_planner.planner import verify_draft

    sections = [
        section("1", title="A", start="10:00", end="11:00"),
        section("2", section_no="02", title="B", start="10:30", end="11:30"),
    ]
    audit = audit_with([
        RequirementBlock(label="A", pick_n_of=1, options=[CourseRef(subject="CYS", number="4245")]),
        RequirementBlock(label="B", pick_n_of=1, options=[CourseRef(subject="CYS", number="4245")]),
    ])
    planner = Planner(
        audit,
        {"202620": snapshot("202620", sections)},
        ratings=ratings(tmp_path),
        prereqs=no_prereqs(tmp_path),
    )
    draft = planner.draft("202620")
    assert verify_draft(draft, planner.snapshots["202620"]) == []

    draft.selections[0].crn = "does-not-exist"
    problems = verify_draft(draft, planner.snapshots["202620"])
    assert any("not in snapshot" in problem for problem in problems)


def test_arrangement_is_an_annotation_not_a_filter(tmp_path):
    sections = [section("1", subject="CYS", number="4855", credits=2.0, instructor="Zderad, Jonathan")]
    audit = audit_with([
        RequirementBlock(label="Capstone", pick_n_of=1, options=[CourseRef(subject="CYS", number="4855")])
    ])
    lookup = {"Zderad, Jonathan": RatingInfo(name="Zderad, Jonathan", rating=5.0, num_ratings=7)}
    planner = Planner(
        audit,
        {"202620": snapshot("202620", sections)},
        ratings=RatingProvider(path=tmp_path / "ratings.json", lookup=lookup.__getitem__),
        prereqs=no_prereqs(tmp_path),
    )
    draft = planner.draft("202620")
    assert [s.crn for s in draft.selections] == ["1"]
    assert draft.selections[0].requires_arrangement is True
    assert any("faculty sponsor" in warning for warning in draft.warnings)

    excluded = planner.draft("202620", Constraints(exclude_arrangement=True))
    assert excluded.selections == []
    assert "excluded by constraint" in excluded.unplaced_requirements[0].reason
