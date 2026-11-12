"""Catalog tests: requirement extraction, resolution, offering patterns, labels."""

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
from flightplan.catalog import (
    CourseKey,
    OfferingPattern,
    Priority,
    applied_codes,
    offering_history_attribute,
    offering_history_course,
    render_matrix,
    requirements_from_audit,
    resolve,
    term_label,
)
from flightplan.models import Instructor, Section, SectionAttribute


def section(
    crn: str,
    subject: str,
    number: str,
    *,
    term: str,
    instructor: str = "Smith, James",
    attributes: tuple[str, ...] = (),
    open: bool = True,
    credits: float = 4.0,
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
        capacity=30 if open else 0,
        enrollment=0,
        seats_available=30 if open else 0,
        open=open,
        faculty=[Instructor(name=instructor)],
        attributes=[SectionAttribute(code=code, description=code) for code in attributes],
    )


def snapshot(term: str, sections: list[Section]) -> TermSnapshot:
    return TermSnapshot(meta=SnapshotMeta(term=term, fetched_at=datetime.now(timezone.utc)), sections=sections)


def test_term_labels_follow_academic_year_prefix():
    assert term_label("202610") == "Fall 2026"
    assert term_label("202620") == "Spring 2027"
    assert term_label("202630") == "Summer 2027"
    assert term_label("202710") == "Fall 2027"
    assert term_label("202720") == "Spring 2028"


def test_requirements_extract_courses_attributes_credits_and_applied_state():
    audit = Audit(
        groups=[
            RequirementGroup(
                id="RA1",
                title="TRAD: Core Curriculum",
                kind=GroupKind.GENED,
                blocks=[
                    RequirementBlock(
                        label="Theology",
                        pick_n_of=1,
                        options=[
                            CourseRef(subject="BIB", number="4360", status="option"),
                            CourseRef(subject="BIB", number="4365", status="option"),
                        ],
                    ),
                    RequirementBlock(
                        label="Bible Exposition - 4 credits",
                        notes="choose 4 credits from the following",
                        options=[
                            CourseRef(subject="BIB", number="3020", credits=2.0, status="in_progress", grade=None),
                            CourseRef(subject="", number="", satisfies_notes="ATTRIBUTE = OTEX", status="option"),
                        ],
                    ),
                    RequirementBlock(
                        label="Natural Science",
                        notes="choose 4 credits from the following",
                        options=[CourseRef(subject="", number="", satisfies_notes="ATTRIBUTE = NATS", status="option")],
                    ),
                    RequirementBlock(label="Met block", status="met", options=[]),
                ],
            )
        ]
    )
    requirements = requirements_from_audit(audit)
    by_label = {requirement.label: requirement for requirement in requirements}
    assert set(by_label) == {"Theology", "Bible Exposition - 4 credits", "Natural Science"}
    bible = by_label["Bible Exposition - 4 credits"]
    assert bible.min_credits == 4.0
    assert bible.applied_credits == 2.0
    assert bible.attributes == ["OTEX"]
    assert [target.code for target in bible.targets] == ["BIB 3020"]
    assert applied_codes(audit) == {"BIB 3020"}


def test_resolve_course_and_attribute_targets():
    term = "202620"
    sections = [
        section("1", "CYS", "4245", term=term, instructor="A"),
        section("2", "LIT", "2157", term=term, attributes=("CCUL",)),
        section("3", "PHI", "2016", term=term, attributes=("TPHI",)),
    ]
    snap = snapshot(term, sections)
    audit = Audit(
        groups=[
            RequirementGroup(
                id="RA2",
                title="Core",
                kind=GroupKind.GENED,
                blocks=[
                    RequirementBlock(
                        label="CCUL",
                        pick_n_of=1,
                        options=[CourseRef(subject="", number="", satisfies_notes="ATTRIBUTE = CCUL")],
                    )
                ],
            )
        ]
    )
    requirement = requirements_from_audit(audit)[0]
    resolution = resolve(requirement, snap)
    assert [candidate.crn for candidate in resolution.candidates] == ["2"]
    assert resolution.attribute_hits == {"CCUL": 1}


def test_resolve_zero_hits_and_closed_sections_are_explained():
    term = "202620"
    snap = snapshot(term, [section("1", "CYS", "3185", term=term, open=False)])
    audit = Audit(
        groups=[
            RequirementGroup(
                id="RA3",
                title="Major",
                kind=GroupKind.MAJOR,
                blocks=[
                    RequirementBlock(
                        label="Server Administration",
                        pick_n_of=1,
                        options=[CourseRef(subject="CYS", number="3185", status="option")],
                    )
                ],
            )
        ]
    )
    requirement = requirements_from_audit(audit)[0]
    closed = resolve(requirement, snap)
    assert closed.candidates == []
    assert "none are open" in closed.reason

    attribute_audit = Audit(
        groups=[
            RequirementGroup(
                id="RA4",
                title="Core",
                kind=GroupKind.GENED,
                blocks=[
                    RequirementBlock(
                        label="TPHI",
                        pick_n_of=1,
                        options=[CourseRef(subject="", number="", satisfies_notes="ATTRIBUTE = TPHI")],
                    )
                ],
            )
        ]
    )
    missing = resolve(requirements_from_audit(attribute_audit)[0], snap)
    assert missing.candidates == []
    assert "zero sections" in missing.reason


def test_resolution_excludes_already_applied_courses():
    term = "202620"
    snap = snapshot(term, [section("1", "CYS", "4245", term=term)])
    audit = Audit(
        groups=[
            RequirementGroup(
                id="RA5",
                title="Major",
                kind=GroupKind.MAJOR,
                blocks=[
                    RequirementBlock(
                        label="Current Practices",
                        pick_n_of=1,
                        options=[CourseRef(subject="CYS", number="4245", status="option")],
                    )
                ],
            )
        ]
    )
    resolution = resolve(requirements_from_audit(audit)[0], snap, exclude_codes={"CYS 4245"})
    assert resolution.candidates == []
    assert "already completed or in progress" in resolution.reason


def test_offering_history_and_patterns():
    terms = ["202520", "202610", "202620", "202710"]
    snaps = {
        "202520": snapshot("202520", [section("1", "BIB", "4825", term="202520")]),
        "202610": snapshot("202610", [section("2", "BIB", "4825", term="202610")]),
        "202620": snapshot("202620", []),
        "202710": snapshot("202710", [section("3", "BIB", "4825", term="202710")]),
    }
    history = offering_history_course(CourseKey(subject="BIB", number="4825"), terms, snaps)
    assert history.offered_terms == ["202520", "202610", "202710"]
    assert history.pattern is OfferingPattern.EVERY_TERM
    assert history.offered_in("202620") is False

    fall_only = offering_history_course(
        CourseKey(subject="CYS", number="4245"),
        ["202620", "202710"],
        {"202620": snapshot("202620", []), "202710": snapshot("202710", [section("4", "CYS", "4245", term="202710")])},
    )
    assert fall_only.pattern is OfferingPattern.FALL_ONLY

    spring_only = offering_history_course(
        CourseKey(subject="BIB", number="3805"),
        ["202620", "202710"],
        {"202620": snapshot("202620", [section("5", "BIB", "3805", term="202620")]), "202710": snapshot("202710", [])},
    )
    assert spring_only.pattern is OfferingPattern.SPRING_ONLY


def test_attribute_history_and_matrix_rendering():
    terms = ["202620"]
    snaps = {"202620": snapshot("202620", [section("1", "LIT", "2157", term="202620", attributes=("CCUL",))])}
    history = offering_history_attribute("CCUL", terms, snaps)
    assert history.terms[0].sections == 1
    table = render_matrix([history], terms)
    assert "ATTRIBUTE CCUL" in table
    assert "Spring 2027" in table


def test_required_leadership_block_is_not_demoted_to_fill():
    audit = Audit(
        groups=[
            RequirementGroup(
                id="RA1",
                title="LEADERSHIP & PROFESSIONALISM - 6-7 credits",
                kind=GroupKind.GENED,
                blocks=[
                    RequirementBlock(
                        label="Leading with Courage and Calling-2 credits",
                        pick_n_of=1,
                        options=[
                            CourseRef(subject="LDR", number="1011", status="option"),
                            CourseRef(subject="LDR", number="2112", status="option"),
                        ],
                    )
                ],
            )
        ]
    )
    requirement = requirements_from_audit(audit)[0]
    assert requirement.optional is False
    assert requirement.priority is Priority.GEN_ED_REQUIRED


def test_additional_credits_pool_stays_optional():
    audit = Audit(
        groups=[
            RequirementGroup(
                id="RA2",
                title="Major: Computer Science",
                kind=GroupKind.MAJOR,
                blocks=[
                    RequirementBlock(
                        label="Additional credits for double major with Cybersecurity",
                        notes="choose 8 credits from the following",
                        options=[CourseRef(subject="DAL", number="", status="option")],
                    )
                ],
            )
        ]
    )
    requirement = requirements_from_audit(audit)[0]
    assert requirement.optional is True
    assert requirement.priority is Priority.MAJOR_POOL
