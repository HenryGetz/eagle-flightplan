"""Typed degree-audit worksheet model.

Mirrors the hierarchy UNW's Degree Works-style worksheet exposes: audit ->
requirement groups (degree / major / minor / gen-ed / ...) -> requirement blocks
("pick 2 of the following") -> course options. Groups and blocks nest.
"""

from __future__ import annotations

from enum import Enum
from pydantic import BaseModel, ConfigDict, Field


class GroupKind(str, Enum):
    DEGREE = "degree"
    MAJOR = "major"
    MINOR = "minor"
    GENED = "gened"
    CORE = "core"
    ELECTIVE = "elective"
    CREDIT_TOTAL = "credit_total"
    GPA = "gpa"
    RESIDENCY = "residency"
    OTHER = "other"


class RequirementStatus(str, Enum):
    MET = "met"
    IN_PROGRESS = "in_progress"
    NOT_MET = "not_met"


class CourseRef(BaseModel):
    """One course line inside a requirement block."""

    model_config = ConfigDict(extra="allow")

    subject: str = ""
    number: str = ""
    title: str = ""
    credits: float | None = None
    grade: str | None = None
    status: str = ""
    term_taken: str | None = None
    satisfies_notes: str = ""

    # Provenance flags observed on real worksheets.
    in_progress: bool = False
    transfer: bool = False
    ap_credit: bool = False

    @property
    def code(self) -> str:
        return f"{self.subject} {self.number}".strip()


class RequirementBlock(BaseModel):
    """A 'choose N of the following' (or explicit course list) unit."""

    model_config = ConfigDict(extra="allow")

    label: str = ""
    pick_n_of: int | None = None
    status: RequirementStatus = RequirementStatus.NOT_MET
    options: list[CourseRef] = Field(default_factory=list)
    notes: str = ""


class RequirementGroup(BaseModel):
    """A degree/major/minor/gen-ed requirement section of the worksheet."""

    model_config = ConfigDict(extra="allow")

    id: str = ""
    title: str
    kind: GroupKind = GroupKind.OTHER
    required_credits: float | None = None
    earned_credits: float | None = None
    remaining_credits: float | None = None
    status: RequirementStatus = RequirementStatus.NOT_MET
    notes: str = ""
    blocks: list[RequirementBlock] = Field(default_factory=list)
    sub_groups: list["RequirementGroup"] = Field(default_factory=list)


class Audit(BaseModel):
    """Normalized degree audit for one student."""

    model_config = ConfigDict(extra="allow")

    student_name: str = ""
    student_id: str = ""
    catalog_year: str = ""
    majors: list[str] = Field(default_factory=list)
    minors: list[str] = Field(default_factory=list)
    credits_earned: float | None = None
    credits_in_progress: float | None = None
    gpa: float | None = None
    groups: list[RequirementGroup] = Field(default_factory=list)
    raw_ref: str = ""

    def walk(self) -> list[RequirementGroup]:
        """Every group and nested sub-group, depth-first."""
        out: list[RequirementGroup] = []

        def visit(group: RequirementGroup) -> None:
            out.append(group)
            for child in group.sub_groups:
                visit(child)

        for group in self.groups:
            visit(group)
        return out

    def unmet(self) -> list[RequirementGroup]:
        """Groups still outstanding (excluding pure GPA/residency bookkeeping)."""
        return [
            group
            for group in self.walk()
            if group.status != RequirementStatus.MET and group.kind not in (GroupKind.GPA, GroupKind.RESIDENCY)
        ]


class AuditParseError(ValueError):
    """The worksheet markup did not look like a recognisable audit."""

