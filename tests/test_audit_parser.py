import json
from datetime import UTC
from pathlib import Path

import pytest

from eagle_flightplan.audit.models import AuditParseError, GroupKind, RequirementStatus
from eagle_flightplan.audit.parser import parse_audit

FIXTURE = Path(__file__).parent / "fixtures" / "audit_WEB31.json"


@pytest.fixture(scope="module")
def audit():
    return parse_audit(json.loads(FIXTURE.read_text()))


def test_identity_and_totals(audit):
    assert audit.student_name == "REDACTED STUDENT"
    assert audit.student_id == "00000000"
    assert audit.catalog_year == "2024-2025 (Fall 2024)"
    assert audit.majors == ["Cybersecurity", "Computer Science"]
    assert audit.minors == ["Bible"]
    assert audit.credits_earned == 86.0
    assert audit.credits_in_progress == 15.0
    assert audit.gpa == 4.0
    assert audit.raw_ref == "A000D3oX"


def test_group_inventory(audit):
    assert len(audit.groups) == 5
    kinds = {group.kind for group in audit.groups}
    assert GroupKind.DEGREE in kinds
    assert GroupKind.MAJOR in kinds
    assert GroupKind.MINOR in kinds
    degree = audit.groups[0]
    assert degree.title == "Degree: Bachelor of Science"
    assert degree.required_credits == 125.0
    assert degree.remaining_credits == 24.0
    assert degree.status is RequirementStatus.NOT_MET


def test_cs_major_group_has_remaining_credits(audit):
    cs = [group for group in audit.groups if group.title == "Major: Computer Science"]
    assert len(cs) == 1
    group = cs[0]
    assert group.kind is GroupKind.MAJOR
    assert group.remaining_credits is not None and group.remaining_credits > 0
    assert group.earned_credits == 48.0


def test_unmet_group_parses_with_options(audit):
    unmet = [group for group in audit.walk() if group.status is not RequirementStatus.MET]
    assert unmet
    python_blocks = [
        block
        for group in unmet
        for block in group.blocks
        if "Python Programming" in block.label
    ]
    assert python_blocks, "expected a Python Programming requirement"
    block = python_blocks[0]
    assert block.status is RequirementStatus.NOT_MET
    assert [option.code for option in block.options] == ["COS 2005", "COS 2211", "COS 2212"]


def test_in_progress_courses_are_flagged(audit):
    stats = [
        block
        for group in audit.walk()
        for block in group.blocks
        if "Statistics" in block.label
    ]
    assert stats
    applied = [option for option in stats[0].options if option.status != "option"]
    assert applied and applied[0].in_progress is True
    assert applied[0].grade is None


def test_transfer_courses_marked(audit):
    transfers = [
        option
        for group in audit.walk()
        for block in group.blocks
        for option in block.options
        if option.transfer
    ]
    assert transfers, "AP/transfer credits should be flagged"


def test_attribute_wildcard_options_are_documented():
    payload = json.loads(FIXTURE.read_text())
    audit = parse_audit(payload)
    wildcards = [
        option
        for group in audit.walk()
        for block in group.blocks
        for option in block.options
        if option.satisfies_notes.startswith("ATTRIBUTE")
    ]
    assert wildcards
    assert wildcards[0].subject == ""


def test_rejects_non_audit_payload():
    with pytest.raises(AuditParseError):
        parse_audit({"foo": "bar"})


def _single_course_audit(grade: str) -> dict:
    return {
        "auditHeader": {"studentName": "Test Student"},
        "degreeInformation": {},
        "classInformation": {"classArray": []},
        "blockArray": [
            {
                "requirementId": "RA1",
                "title": "Major",
                "requirementType": "MAJOR",
                "percentComplete": "0",
                "ruleArray": [
                    {
                        "ruleId": "1",
                        "label": "Course X",
                        "percentComplete": "0",
                        "requirement": {
                            "classesBegin": "1",
                            "courseArray": [{"discipline": "CYS", "number": "3065"}],
                        },
                        "classesAppliedToRule": {
                            "classArray": [
                                {
                                    "discipline": "CYS",
                                    "number": "3065",
                                    "letterGrade": grade,
                                    "credits": 4,
                                }
                            ]
                        },
                    }
                ],
            }
        ],
    }


@pytest.mark.parametrize(
    ("grade", "status"),
    [
        ("F", "failed"),
        ("WF", "failed"),
        ("NC", "failed"),
        ("W", "withdrawn"),
        ("I", "incomplete"),
        ("AU", "audit"),
    ],
)
def test_non_credit_grades_never_earn_credit(grade, status):
    from eagle_flightplan.catalog import applied_codes, requirements_from_audit

    audit = parse_audit(_single_course_audit(grade))
    option = audit.groups[0].blocks[0].options[0]
    assert option.status == status
    assert option.in_progress is False
    assert applied_codes(audit) == set()
    requirement = requirements_from_audit(audit)[0]
    assert requirement.applied_count == 0
    assert requirement.applied_credits == 0.0
    assert requirement.satisfied is False


def test_failed_course_can_be_retaken():
    from datetime import datetime

    from eagle_flightplan.banner.search import SnapshotMeta, TermSnapshot
    from eagle_flightplan.catalog import applied_codes, requirements_from_audit, resolve
    from eagle_flightplan.models import Instructor, Section

    audit = parse_audit(_single_course_audit("F"))
    requirement = requirements_from_audit(audit)[0]
    snapshot = TermSnapshot(
        meta=SnapshotMeta(term="202620", fetched_at=datetime.now(UTC)),
        sections=[
            Section(
                crn="9001",
                term="202620",
                subject="CYS",
                number="3065",
                section="01",
                title="Course X",
                credits_low=4.0,
                seats_available=10,
                open=True,
                faculty=[Instructor(name="Smith, James")],
            )
        ],
    )
    resolution = resolve(requirement, snapshot, exclude_codes=applied_codes(audit))
    assert [section.crn for section in resolution.candidates] == ["9001"]
