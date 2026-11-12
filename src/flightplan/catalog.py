"""Turn audit requirements into real sections, and learn when courses run.

Two capabilities:

``requirements_from_audit(audit)``
    Flattens the Degree Works worksheet into :class:`Requirement` objects with
    explicit course targets, attribute targets (``ATTRIBUTE = CCUL``), subject
    wildcards (``DAL``) and credit pools ("choose 8 credits from the following"),
    carrying any credits already applied / in progress.

``offering_history(course_or_attribute, terms, snapshots)``
    Answers "is this offered in Spring 2027?" across every cached term and infers
    a fall-only / spring-only / every-term / summer pattern. The planner roadmap
    is built on this matrix.
"""

from __future__ import annotations

import re
from enum import Enum
from typing import TYPE_CHECKING, Iterable, Sequence

from pydantic import BaseModel, Field

from .audit.models import Audit, CourseRef, GroupKind, RequirementGroup, RequirementStatus

if TYPE_CHECKING:  # avoids catalog -> banner.search -> banner.plan -> catalog
    from .banner.search import TermSnapshot
from .models import Section

SEASON = {"10": "Fall", "20": "Spring", "30": "Summer", "40": "Winter"}
TERM_ORDER = ("202520", "202530", "202610", "202620", "202630")

CREDITS_IN_NOTES = re.compile(r"choose\s+(?P<low>\d+(?:\.\d+)?)(?:-(?P<high>\d+(?:\.\d+)?))?\s+credits?", re.IGNORECASE)
ATTRIBUTE_IN_NOTES = re.compile(r"ATTRIBUTE\s*=\s*([A-Z0-9, ]+)", re.IGNORECASE)


def term_season(term: str) -> str:
    return SEASON.get(term[4:6], "Unknown")


def term_label(term: str) -> str:
    """UNW codes are academic-year prefixes: 202620 is Spring **2027**."""
    season = term_season(term)
    year = int(term[:4]) + (0 if season == "Fall" else 1)
    return f"{season} {year}"


class Priority(str, Enum):
    MAJOR_REQUIRED = "major_required"
    MAJOR_POOL = "major_pool"
    GEN_ED_REQUIRED = "gen_ed_required"
    GEN_ED_FILL = "gen_ed_fill"


class Availability(str, Enum):
    OPEN = "open"
    NOT_YET_OPEN = "not_yet_open"
    CLOSED = "closed"


#: Course-status values that earn credit toward requirements and prerequisites.
#: ``failed``/``withdrawn``/``incomplete``/``audit`` are deliberately excluded so
#: an attempt can be retaken and cannot silently satisfy a requirement.
CREDIT_STATUSES = frozenset({"completed", "in_progress"})


ARRANGEMENT_NUMBERS = {"4841", "4855", "4995"}


def availability_of(section: Section, current_term: str | None = None) -> Availability:
    """Distinguish 'full' from 'not yet opened' for future terms."""
    if (section.seats_available or 0) > 0:
        return Availability.OPEN
    if current_term and section.term > current_term:
        return Availability.NOT_YET_OPEN
    return Availability.CLOSED


class CourseKey(BaseModel):
    model_config = {"frozen": True}

    subject: str
    number: str

    @property
    def code(self) -> str:
        return f"{self.subject} {self.number}".strip()

    @classmethod
    def parse(cls, text: str) -> "CourseKey":
        match = re.match(r"\s*([A-Za-z]{2,5})\s*([0-9][0-9A-Za-z]*)\s*$", text)
        if not match:
            raise ValueError(f"Not a course code: {text!r}")
        return cls(subject=match.group(1).upper(), number=match.group(2).upper())


class Requirement(BaseModel):
    """One actionable worksheet block, ready to be matched against sections."""

    id: str
    parent_group: str
    kind: GroupKind
    label: str
    targets: list[CourseKey] = Field(default_factory=list)
    attributes: list[str] = Field(default_factory=list)
    subject_wildcards: list[str] = Field(default_factory=list)
    pick_n_of: int | None = None
    min_credits: float | None = None
    applied_credits: float = 0.0
    applied_count: int = 0
    major_specific: bool = False
    optional: bool = False
    priority: Priority = Priority.GEN_ED_FILL
    all_of: list[str] = Field(default_factory=list)
    subjects: list[str] = Field(default_factory=list)
    requires_arrangement: bool = False
    notes: str = ""

    @property
    def satisfied(self) -> bool:
        if self.pick_n_of is not None and self.applied_count >= self.pick_n_of:
            return True
        if self.min_credits is not None and self.applied_credits >= self.min_credits:
            return True
        return False

    @property
    def remaining_credits(self) -> float | None:
        if self.min_credits is None:
            return None
        return max(0.0, round(self.min_credits - self.applied_credits, 2))

    @property
    def target_codes(self) -> list[str]:
        return [target.code for target in self.targets]

    @property
    def search_label(self) -> str:
        bits = [target.code for target in self.targets]
        bits += [f"attr {code}" for code in self.attributes]
        bits += [f"{subject} *" for subject in self.subject_wildcards]
        return ", ".join(bits) or self.label


def _is_optional(group_title: str, label: str, kind: GroupKind, min_credits: float | None) -> bool:
    """True only for genuine elective/extra-credit pools.

    A block that names concrete courses or states a minimum (``pick one of`` or
    ``choose 4 credits``) is a requirement, even when its group title happens to
    contain a word like "leadership" or "scientific".
    """
    if kind is GroupKind.ELECTIVE:
        return True
    lowered = f"{group_title} {label}".lower()
    return "additional credits" in lowered or "elective" in lowered


def _priority(
    group_title: str,
    label: str,
    kind: GroupKind,
    min_credits: float | None,
    targets: list[CourseKey],
    attributes: list[str],
) -> Priority:
    optional = _is_optional(group_title, label, kind, min_credits)
    if kind in (GroupKind.MAJOR, GroupKind.MINOR):
        return Priority.MAJOR_POOL if optional else Priority.MAJOR_REQUIRED
    if optional:
        return Priority.GEN_ED_FILL
    # Everything else is a named general-education/degree requirement. No
    # keyword on the group title is allowed to demote it to filler.
    return Priority.GEN_ED_REQUIRED


def _parse_attribute_codes(ref: CourseRef) -> list[str]:
    codes: list[str] = []
    for match in ATTRIBUTE_IN_NOTES.finditer(ref.satisfies_notes):
        for code in match.group(1).split(","):
            code = code.strip().upper()
            if code and code not in {"AND", "OR"}:
                codes.append(code)
    return codes


def _needs_arrangement(label: str, targets: list[CourseKey]) -> bool:
    lowered = label.lower()
    if any(word in lowered for word in ("internship", "research", "capstone", "directed study", "senior project")):
        return True
    return any(target.number in ARRANGEMENT_NUMBERS for target in targets)


def requirements_from_audit(audit: Audit) -> list[Requirement]:
    """Every outstanding worksheet block that can be satisfied by course work."""
    requirements: list[Requirement] = []

    def visit(group: RequirementGroup) -> None:
        for index, block in enumerate(group.blocks):
            if block.status is RequirementStatus.MET:
                continue
            options = block.options
            targets = [
                CourseKey(subject=option.subject.upper(), number=option.number.upper())
                for option in options
                if option.subject and option.number
            ]
            attributes: list[str] = []
            wildcards: list[str] = []
            for option in options:
                attributes.extend(_parse_attribute_codes(option))
                if option.subject and not option.number:
                    wildcards.append(option.subject.upper())
            applied = [option for option in options if option.status in CREDIT_STATUSES]
            applied_credits = sum(option.credits or 0 for option in applied)
            credit_match = CREDITS_IN_NOTES.search(block.notes or "")
            min_credits = None
            if credit_match:
                min_credits = float(credit_match.group("high") or credit_match.group("low"))
            if not targets and not attributes and not wildcards:
                continue
            if block.pick_n_of is None and min_credits is None and applied:
                continue
            all_of: list[str] = []
            subjects: list[str] = []
            if "LABS" in attributes:
                # UNW encodes lab science as separate LABS + MINCREDIT qualifier.
                all_of = ["LABS"]
                attributes = [code for code in attributes if code != "LABS"]
            min_credit_note = re.search(
                r"MinCredits\s+[\d.]+\s+in\s+([A-Za-z0-9@, ]+)", block.notes or ""
            )
            if min_credit_note:
                subjects = sorted({
                    token for token in re.findall(r"\b([A-Z]{2,4})\b", min_credit_note.group(1))
                    if token not in {"MIN", "IN"}
                })
            requirements.append(
                Requirement(
                    id=f"{group.id}:{index}",
                    parent_group=group.title,
                    kind=group.kind,
                    label=block.label,
                    targets=targets,
                    attributes=sorted(set(attributes)),
                    all_of=all_of,
                    subjects=subjects,
                    subject_wildcards=sorted(set(wildcards)),
                    pick_n_of=block.pick_n_of,
                    min_credits=min_credits,
                    applied_credits=round(applied_credits, 2),
                    applied_count=len(applied),
                    major_specific=group.kind is GroupKind.MAJOR,
                    optional=_is_optional(group.title, block.label, group.kind, min_credits),
                    priority=_priority(group.title, block.label, group.kind, min_credits, targets, attributes),
                    requires_arrangement=_needs_arrangement(block.label, targets),
                    notes=block.notes,
                )
            )
        for sub in group.sub_groups:
            visit(sub)

    for group in audit.groups:
        visit(group)
    return requirements


def applied_codes(audit: Audit) -> set[str]:
    """Every course already completed or in progress anywhere on the worksheet."""
    codes: set[str] = set()
    for group in audit.walk():
        for block in group.blocks:
            for option in block.options:
                if option.status in CREDIT_STATUSES and option.code:
                    codes.add(option.code.upper().replace("  ", " "))
    return codes


class Resolution(BaseModel):
    """Candidate sections for one requirement in one term."""

    requirement: Requirement
    term: str
    candidates: list[Section] = Field(default_factory=list)
    attribute_hits: dict[str, int] = Field(default_factory=dict)
    reason: str = ""

    @property
    def found(self) -> bool:
        return bool(self.candidates)

    @property
    def open_candidates(self) -> list[Section]:
        return [section for section in self.candidates if section.open]


def resolve(
    requirement: Requirement,
    snapshot: TermSnapshot,
    *,
    exclude_codes: Iterable[str] = (),
    open_only: bool = True,
    current_term: str | None = None,
) -> Resolution:
    """Map one requirement onto sections offered in ``snapshot``'s term."""
    excluded = {code.upper().replace("  ", " ") for code in exclude_codes}
    candidates: dict[str, Section] = {}
    hits: dict[str, int] = {}

    for target in requirement.targets:
        for section in snapshot.by_course(target.subject, target.number):
            candidates[section.crn] = section
    for attribute in requirement.attributes:
        matched = snapshot.by_attribute(attribute)
        hits[attribute] = len(matched)
        for section in matched:
            candidates[section.crn] = section
    for required in requirement.all_of:
        candidates = {
            crn: section
            for crn, section in candidates.items()
            if required.upper() in section.attribute_codes
        }
    if requirement.subjects:
        allowed = {subject.upper() for subject in requirement.subjects}
        candidates = {
            crn: section for crn, section in candidates.items() if section.subject.upper() in allowed
        }
    for subject in requirement.subject_wildcards:
        for section in snapshot.by_subject(subject):
            candidates[section.crn] = section

    excluded_hits = [section for section in candidates.values() if section.code.upper() in excluded]
    filtered = []
    for section in candidates.values():
        if section.code.upper() in excluded:
            continue
        state = availability_of(section, current_term)
        if open_only and state is Availability.CLOSED:
            continue
        filtered.append(section)
    filtered.sort(key=lambda section: (section.subject, section.number, section.section, section.crn))

    reason = ""
    if not filtered:
        if candidates:
            matched = sorted({section.code for section in candidates.values()})
            if excluded_hits and not [
                section for section in candidates.values() if section.code.upper() not in excluded
            ]:
                reason = (
                    f"{', '.join(sorted({section.code for section in excluded_hits}))} is already "
                    "completed or in progress"
                )
            else:
                reason = (
                    f"{len(candidates)} section(s) exist for {', '.join(matched)} but none are open "
                    f"in {snapshot.term} (closed/unpublished or zero seats)"
                )
        elif hits:
            zero = [code for code, count in hits.items() if count == 0]
            if zero:
                reason = (
                    f"attribute target(s) {', '.join(zero)} have zero sections in {snapshot.term}; "
                    f"the requirement cannot be filled this term"
                )
            else:
                reason = f"{len(candidates)} section(s) match but none are open in {snapshot.term}"
        elif requirement.targets or requirement.subject_wildcards:
            reason = f"none of {requirement.search_label} is offered in {snapshot.term}"
        else:
            reason = "requirement has no resolvable targets"
    return Resolution(
        requirement=requirement,
        term=snapshot.term,
        candidates=filtered,
        attribute_hits=hits,
        reason=reason,
    )


# --------------------------------------------------------------------------- #
# offering history
# --------------------------------------------------------------------------- #
class TermOffering(BaseModel):
    term: str
    offered: bool = False
    sections: int = 0
    open_sections: int = 0
    instructors: list[str] = Field(default_factory=list)
    part_of_terms: list[str] = Field(default_factory=list)

    @property
    def label(self) -> str:
        return term_label(self.term)


class OfferingPattern(str, Enum):
    EVERY_TERM = "every-term"
    FALL_ONLY = "fall-only"
    SPRING_ONLY = "spring-only"
    SUMMER_ONLY = "summer-only"
    FALL_AND_SUMMER = "fall+summer"
    SPRING_AND_SUMMER = "spring+summer"
    IRREGULAR = "irregular"
    UNKNOWN = "unknown"


class OfferingHistory(BaseModel):
    key: str
    course: CourseKey | None = None
    attribute: str | None = None
    terms: list[TermOffering] = Field(default_factory=list)
    pattern: OfferingPattern = OfferingPattern.UNKNOWN

    @property
    def offered_terms(self) -> list[str]:
        return [entry.term for entry in self.terms if entry.offered]

    def offered_in(self, term: str) -> bool:
        return any(entry.term == term and entry.offered for entry in self.terms)


def _pattern(terms: Sequence[TermOffering]) -> OfferingPattern:
    offered = [entry.term for entry in terms if entry.offered]
    if not offered:
        return OfferingPattern.UNKNOWN
    springs = [term for term in offered if term_season(term) in {"Spring", "Winter"}]
    falls = [term for term in offered if term_season(term) == "Fall"]
    summers = [term for term in offered if term_season(term) == "Summer"]
    if springs and falls and summers:
        return OfferingPattern.EVERY_TERM
    if springs and falls:
        return OfferingPattern.EVERY_TERM
    if falls and summers:
        return OfferingPattern.FALL_AND_SUMMER
    if springs and summers:
        return OfferingPattern.SPRING_AND_SUMMER
    if falls:
        return OfferingPattern.FALL_ONLY
    if springs:
        return OfferingPattern.SPRING_ONLY
    if summers:
        return OfferingPattern.SUMMER_ONLY
    return OfferingPattern.UNKNOWN


def offering_history_course(
    course: CourseKey, terms: Sequence[str], snapshots: dict[str, TermSnapshot]
) -> OfferingHistory:
    entries: list[TermOffering] = []
    for term in terms:
        snapshot = snapshots.get(term)
        if snapshot is None:
            continue
        sections = snapshot.by_course(course.subject, course.number)
        entries.append(
            TermOffering(
                term=term,
                offered=bool(sections),
                sections=len(sections),
                open_sections=sum(1 for section in sections if section.open),
                instructors=sorted(
                    {person.name for section in sections for person in section.faculty if person.name}
                ),
                part_of_terms=sorted({section.part_of_term for section in sections if section.part_of_term}),
            )
        )
    return OfferingHistory(
        key=course.code,
        course=course,
        terms=entries,
        pattern=_pattern(entries),
    )


def offering_history_attribute(
    attribute: str, terms: Sequence[str], snapshots: dict[str, TermSnapshot]
) -> OfferingHistory:
    entries: list[TermOffering] = []
    for term in terms:
        snapshot = snapshots.get(term)
        if snapshot is None:
            continue
        sections = snapshot.by_attribute(attribute)
        entries.append(
            TermOffering(
                term=term,
                offered=bool(sections),
                sections=len(sections),
                open_sections=sum(1 for section in sections if section.open),
                instructors=sorted(
                    {person.name for section in sections for person in section.faculty if person.name}
                ),
                part_of_terms=sorted({section.part_of_term for section in sections if section.part_of_term}),
            )
        )
    return OfferingHistory(
        key=f"ATTRIBUTE {attribute}",
        attribute=attribute.upper(),
        terms=entries,
        pattern=_pattern(entries),
    )


def offering_matrix(
    requirements: Sequence[Requirement],
    terms: Sequence[str],
    snapshots: dict[str, TermSnapshot],
) -> list[OfferingHistory]:
    """One row per distinct target/attribute across every requirement."""
    rows: dict[str, OfferingHistory] = {}
    for requirement in requirements:
        for target in requirement.targets:
            rows.setdefault(
                target.code,
                offering_history_course(target, terms, snapshots),
            )
        for attribute in requirement.attributes:
            key = f"ATTRIBUTE {attribute}"
            rows.setdefault(
                key,
                offering_history_attribute(attribute, terms, snapshots),
            )
        for subject in requirement.subject_wildcards:
            key = f"{subject} *"
            if key in rows:
                continue
            entries = []
            for term in terms:
                snapshot = snapshots.get(term)
                if snapshot is None:
                    continue
                sections = snapshot.by_subject(subject)
                entries.append(
                    TermOffering(
                        term=term,
                        offered=bool(sections),
                        sections=len(sections),
                        open_sections=sum(1 for section in sections if section.open),
                        instructors=sorted(
                            {
                                person.name
                                for section in sections
                                for person in section.faculty
                                if person.name
                            }
                        ),
                        part_of_terms=sorted(
                            {section.part_of_term for section in sections if section.part_of_term}
                        ),
                    )
                )
            rows[key] = OfferingHistory(key=key, terms=entries, pattern=_pattern(entries))
    return [rows[key] for key in sorted(rows)]


def render_matrix(histories: Sequence[OfferingHistory], terms: Sequence[str]) -> str:
    """Markdown table: one row per course/attribute, one column per term."""
    header = "| target | " + " | ".join(term_label(term) for term in terms) + " | pattern |"
    divider = "| --- | " + " | ".join("---" for _ in terms) + " | --- |"
    lines = [header, divider]
    for history in histories:
        cells = []
        for term in terms:
            entry = next((item for item in history.terms if item.term == term), None)
            if entry is None:
                cells.append("?")
            elif entry.offered:
                instructors = ", ".join(entry.instructors[:2]) or "-"
                cells.append(f"Y {entry.sections}× ({instructors})")
            else:
                cells.append("N")
        lines.append(f"| {history.key} | " + " | ".join(cells) + f" | {history.pattern.value} |")
    return "\n".join(lines)
