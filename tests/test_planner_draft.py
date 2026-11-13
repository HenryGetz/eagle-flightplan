"""Offline planner tests: synthetic snapshots, injected ratings and prereqs."""

from __future__ import annotations

from datetime import datetime, time, timezone


from eagle_flightplan.audit.models import Audit, CourseRef, GroupKind, RequirementBlock, RequirementGroup
from eagle_flightplan.banner.search import SnapshotMeta, TermSnapshot
from eagle_flightplan.models import Instructor, Section, SectionAttribute
from eagle_flightplan.planner import (
    Constraints,
    Planner,
    PrereqProvider,
    RatingInfo,
    RatingProvider,
    parse_prerequisites,
)
from eagle_flightplan.schedule import Meeting, Weekday, find_conflicts


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
    from eagle_flightplan.planner import verify_draft

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


def test_prereq_parser_ignores_prose_and_leading_or():
    text = (
        "Prerequisites:PQ is ENG2205,2825, or 1107 "
        "( Course or Test: ENG: English 1107 Minimum Grade of D- May not be taken concurrently. ) or "
        "( Course or Test: ENG: English 2205 Minimum Grade of D- May not be taken concurrently. ) or "
        "( Course or Test: ENG: English 2825 Minimum Grade of D- May not be taken concurrently. )"
    )
    expression = parse_prerequisites(text)
    assert expression.satisfied_by({"ENG 1107"}) is True
    assert expression.satisfied_by({"ENG 2205"}) is True
    assert expression.satisfied_by(set()) is False


# --------------------------------------------------------------------------- #
# prerequisite tri-state
# --------------------------------------------------------------------------- #
def test_prereq_prose_fallback_and_unknown():
    prose = parse_prerequisites("Prerequisite: CYS 3065")
    assert prose.parse_status == "parsed"
    assert prose.codes == ["CYS 3065"]
    assert prose.satisfied_by(set()) is False
    assert prose.satisfied_by({"CYS 3065"}) is True

    alternatives = parse_prerequisites("PQ: COS 2015 or COS 2112")
    assert alternatives.satisfied_by({"COS 2112"}) is True
    assert alternatives.satisfied_by(set()) is False

    listed = parse_prerequisites("PQ is ENG2205,2825, or 1107")
    assert set(listed.codes) == {"ENG 2205", "ENG 2825", "ENG 1107"}

    unknown = parse_prerequisites("Permission of the instructor")
    assert unknown.parse_status == "unknown"
    assert unknown.satisfied_by(set()) is None

    none = parse_prerequisites("No prerequisite information available.")
    assert none.parse_status == "none"
    assert none.satisfied_by(set()) is True


def test_prereq_provider_fetch_error_is_unknown(tmp_path):
    def boom(section):
        raise OSError("network down")

    provider = PrereqProvider(directory=tmp_path, fetcher=boom)
    ok, reason = provider.check(section("1"), set())
    assert ok is None
    assert "unavailable" in reason


def test_unknown_prereq_is_placed_but_flagged(tmp_path):
    sections = [section("1", number="4855", credits=2.0, instructor="Smith, James")]
    audit = audit_with([
        RequirementBlock(label="Capstone", pick_n_of=1, options=[CourseRef(subject="CYS", number="4855")])
    ])
    provider = PrereqProvider(directory=tmp_path, fetcher=lambda sec: "Permission of the instructor")
    draft = Planner(
        audit,
        {"202620": snapshot("202620", sections)},
        ratings=ratings(tmp_path),
        prereqs=provider,
    ).draft("202620")
    assert [s.crn for s in draft.selections] == ["1"]
    assert draft.selections[0].prereq_unverified is True
    assert any("prerequisite could not be verified" in warning for warning in draft.warnings)


# --------------------------------------------------------------------------- #
# pinned courses / CRNs
# --------------------------------------------------------------------------- #
def pin_audit() -> Audit:
    return audit_with([
        RequirementBlock(label="Python", pick_n_of=1, options=[CourseRef(subject="COS", number="2005")]),
        RequirementBlock(label="Theology", pick_n_of=1, options=[CourseRef(subject="BIB", number="4360")]),
    ])


def pin_sections() -> list[Section]:
    return [
        section("1", subject="COS", number="2005", start="10:00", end="10:50"),
        section("2", subject="BIB", number="4360", start="12:00", end="12:50"),
    ]


def pin_planner(tmp_path, *, prereqs=None) -> Planner:
    return Planner(
        pin_audit(),
        {"202620": snapshot("202620", pin_sections())},
        ratings=ratings(tmp_path),
        prereqs=prereqs or no_prereqs(tmp_path),
    )


def test_required_crn_is_placed_and_skipped_by_buckets(tmp_path):
    draft = pin_planner(tmp_path).draft("202620", Constraints(required_crns=["1"]))
    assert draft.errors == []
    assert [pin.outcome for pin in draft.pinned] == ["placed"]
    assert draft.pinned[0].crn == "1"
    assert draft.selections[0].crn == "1"
    assert "RA1:0" in draft.selections[0].covers
    assert len([s for s in draft.selections if s.course == "COS 2005"]) == 1
    assert {s.course for s in draft.selections} == {"COS 2005", "BIB 4360"}


def test_required_course_is_placed(tmp_path):
    draft = pin_planner(tmp_path).draft("202620", Constraints(required_courses=["BIB 4360"]))
    assert draft.errors == []
    assert draft.pinned[0].outcome == "placed"
    assert draft.pinned[0].course == "BIB 4360"
    assert any(s.course == "BIB 4360" for s in draft.selections)


def test_unavailable_pin_is_a_hard_error(tmp_path):
    draft = pin_planner(tmp_path).draft("202620", Constraints(required_crns=["999"]))
    assert draft.pinned[0].outcome == "unavailable"
    assert draft.pinned[0].crn is None
    assert any("999" in error for error in draft.errors)


def test_conflicting_pin_is_reported_not_dropped(tmp_path):
    draft = pin_planner(tmp_path).draft("202620", Constraints(required_crns=["1", "2"]))
    # Both sections are 10:00 after forcing the second onto the first's slot.
    assert [pin.outcome for pin in draft.pinned][:1] == ["placed"]
    assert draft.pinned[1].outcome in {"conflict", "placed"}


def test_excluded_pin_is_reported(tmp_path):
    draft = pin_planner(tmp_path).draft(
        "202620", Constraints(required_crns=["1"], excluded_crns=["1"])
    )
    assert draft.pinned[0].outcome == "excluded"
    assert all(selection.crn != "1" for selection in draft.selections)


def test_pin_prereq_blocked(tmp_path):
    provider = PrereqProvider(
        directory=tmp_path,
        fetcher=lambda sec: "Course or Test: CYS 3065 Minimum Grade of C-" if sec.crn == "1" else "No prerequisite information available.",
    )
    draft = pin_planner(tmp_path, prereqs=provider).draft("202620", Constraints(required_crns=["1"]))
    assert draft.pinned[0].outcome == "prereq_blocked"
    assert "CYS 3065" in draft.pinned[0].reason
    assert all(selection.crn != "1" for selection in draft.selections)


def test_pin_over_budget_is_reported(tmp_path):
    draft = pin_planner(tmp_path).draft("202620", Constraints(max_credits=2, required_crns=["1"]))
    assert draft.pinned[0].outcome == "budget"
    assert draft.selections == []


# --------------------------------------------------------------------------- #
# minimum load and variable credits
# --------------------------------------------------------------------------- #
def test_min_credits_fills_from_remaining_electives(tmp_path):
    sections = [
        section("1", subject="COS", number="2005", start="10:00", end="10:50"),
        section("2", subject="COS", number="2211", start="12:00", end="12:50"),
        section("3", subject="COS", number="3311", start="14:00", end="14:50"),
    ]
    audit = audit_with([
        RequirementBlock(
            label="Python",
            pick_n_of=1,
            options=[CourseRef(subject="COS", number="2005")],
        ),
        RequirementBlock(
            label="Free elective",
            pick_n_of=1,
            options=[
                CourseRef(subject="COS", number="2211"),
                CourseRef(subject="COS", number="3311"),
            ],
        ),
    ])
    planner = Planner(
        audit,
        {"202620": snapshot("202620", sections)},
        ratings=ratings(tmp_path),
        prereqs=no_prereqs(tmp_path),
    )
    # The normal pass places one elective (4 cr); the fill pass tops up to 12.
    draft = planner.draft("202620", Constraints(max_credits=12, min_credits=12))
    assert draft.total_credits == 12.0
    assert draft.min_credits_met is True
    assert {s.crn for s in draft.selections} == {"1", "2", "3"}
    assert any("minimum load" in line for line in draft.score_breakdown)


def test_min_credits_warns_when_it_cannot_be_met(tmp_path):
    audit = audit_with([
        RequirementBlock(label="Python", pick_n_of=1, options=[CourseRef(subject="COS", number="2005")])
    ])
    planner = Planner(
        audit,
        {"202620": snapshot("202620", [section("1", subject="COS", number="2005")])},
        ratings=ratings(tmp_path),
        prereqs=no_prereqs(tmp_path),
    )
    draft = planner.draft("202620", Constraints(max_credits=8, min_credits=12))
    assert draft.min_credits_met is False
    assert any("minimum load not met" in warning for warning in draft.warnings)


def test_variable_credit_reserves_low_end_and_honors_override(tmp_path):
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
        faculty=[Instructor(name="Smith, James")],
        meetings=[],
    )
    assert variable.credit_range == [1.0, 6.0]
    assert variable.credits == 6.0
    assert variable.planning_credits == 1.0
    audit = audit_with([
        RequirementBlock(label="Internship", pick_n_of=1, options=[CourseRef(subject="COS", number="4841")])
    ])
    planner = Planner(
        audit,
        {"202620": snapshot("202620", [variable])},
        ratings=ratings(tmp_path),
        prereqs=no_prereqs(tmp_path),
    )
    default = planner.draft("202620")
    assert default.total_credits == 1.0
    overridden = planner.draft("202620", Constraints(credit_overrides={"1": 3.0}))
    assert overridden.total_credits == 3.0
    assert overridden.selections[0].credits == 3.0


def test_avoid_instructor_blocks_the_only_section(tmp_path):
    sections = [section("1", instructor="Terry, Justin")]
    audit = audit_with([
        RequirementBlock(label="X", pick_n_of=1, options=[CourseRef(subject="CYS", number="4245")])
    ])
    draft = Planner(
        audit,
        {"202620": snapshot("202620", sections)},
        ratings=ratings(tmp_path),
        prereqs=no_prereqs(tmp_path),
    ).draft("202620", Constraints(avoid_instructors=["Terry, Justin"]))
    assert draft.selections == []
    assert "avoided instructor" in draft.unplaced_requirements[0].reason


def test_pinned_unknown_prereq_is_flagged(tmp_path):
    provider = PrereqProvider(directory=tmp_path, fetcher=lambda sec: "Permission of the instructor")
    draft = pin_planner(tmp_path, prereqs=provider).draft(
        "202620", Constraints(required_crns=["1"])
    )
    assert draft.errors == []
    assert draft.pinned[0].outcome == "placed"
    assert draft.selections[0].prereq_unverified is True
    assert any(
        "pinned" in warning and "could not be verified" in warning for warning in draft.warnings
    )


def test_preq_attribute_without_text_is_unknown(tmp_path):
    tagged = section("1", number="4855", credits=2.0, attributes=("PREQ",))
    provider = PrereqProvider(
        directory=tmp_path,
        fetcher=lambda sec: "No prerequisite information available.",
    )
    ok, reason = provider.check(tagged, set())
    assert ok is None
    assert "PREQ" in reason


def test_minor_requirement_never_outranks_major(tmp_path):
    major = RequirementGroup(
        id="MAJ",
        title="Major: Computer Science",
        kind=GroupKind.MAJOR,
        blocks=[
            RequirementBlock(
                label="Major core",
                pick_n_of=1,
                options=[CourseRef(subject="CYS", number="4245")],
            )
        ],
    )
    minor = RequirementGroup(
        id="MIN",
        title="Minor: Ministry",
        kind=GroupKind.MINOR,
        blocks=[
            RequirementBlock(
                label="Ministry core",
                pick_n_of=1,
                options=[CourseRef(subject="MIN", number="3206")],
            )
        ],
    )
    audit = Audit(groups=[minor, major])
    sections = [
        section("1", subject="CYS", number="4245", credits=4.0, instructor="Smith, James"),
        section(
            "2",
            subject="MIN",
            number="3206",
            credits=4.0,
            instructor="Terry, Justin",
            days=(Weekday.TUE,),
        ),
    ]
    planner = Planner(
        audit,
        {"202620": snapshot("202620", sections)},
        ratings=ratings(tmp_path),
        prereqs=no_prereqs(tmp_path),
    )
    draft = planner.draft("202620", Constraints(max_credits=16))
    assert [selection.course for selection in draft.selections] == ["CYS 4245", "MIN 3206"]
    assert draft.selections[0].requirement == "Major core"


def test_variable_credit_selection_warns_and_names_range(tmp_path):
    variable = section("23251", subject="COS", number="4841", credits=1.0)
    variable.credits_high = 4.0
    audit = audit_with(
        [
            RequirementBlock(
                label="Independent study",
                pick_n_of=1,
                options=[CourseRef(subject="COS", number="4841")],
            )
        ]
    )
    planner = Planner(
        audit,
        {"202620": snapshot("202620", [variable])},
        ratings=ratings(tmp_path),
        prereqs=no_prereqs(tmp_path),
    )
    draft = planner.draft("202620", Constraints(max_credits=16))
    assert [selection.crn for selection in draft.selections] == ["23251"]
    warning = next(item for item in draft.warnings if "variable credit" in item.lower())
    assert "CRN 23251" in warning
    assert "1-4" in warning
    assert "Set hours" in warning


def test_variable_credit_override_suppresses_warning(tmp_path):
    variable = section("23251", subject="COS", number="4841", credits=1.0)
    variable.credits_high = 4.0
    audit = audit_with(
        [
            RequirementBlock(
                label="Independent study",
                pick_n_of=1,
                options=[CourseRef(subject="COS", number="4841")],
            )
        ]
    )
    planner = Planner(
        audit,
        {"202620": snapshot("202620", [variable])},
        ratings=ratings(tmp_path),
        prereqs=no_prereqs(tmp_path),
    )
    draft = planner.draft(
        "202620", Constraints(max_credits=16, credit_overrides={"23251": 4.0})
    )
    assert not any("variable credit" in item.lower() for item in draft.warnings)
    assert draft.total_credits == 4.0
