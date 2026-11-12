"""Parse Ellucian Degree Works audit JSON into :mod:`eagle_flightplan.audit.models`.

The audit app (``degreeaudit.oci.unw.edu``) is a React shell over a JSON API:

``GET /api/audit?studentId=<id>&school=UG&degree=BS&is-process-new=false
 &audit-type=AA&auditId=&include-inprogress=true&include-preregistered=true``

returns ``application/vnd.net.hedtech.degree+json`` (served as
``application/json``) with this shape::

    auditHeader            student identity, GPAs, applied-credit counters
    degreeInformation      catalog year, goals (MAJOR/MINOR/PROGRAM), GPA
    blockArray[]           one block per degree/major/minor/core requirement set
      header.qualifierArray  CLASSESCREDITS -> credits/applied/needed
      ruleArray[]          recursive tree of Subset/Group/Course/Complete nodes
        requirement        classesBegin/creditsBegin + courseArray (catalog options)
        classesAppliedToRule.classArray   courses actually applied here
    classInformation.classArray  every applied course, with title/grade/term

``nodeType`` codes seen in the wild: ``4700`` Subset (container), ``4600`` Group
(container), ``4200`` Course (leaf), ``4900`` Complete (waiver/complete note).
"""

from __future__ import annotations

import re
from typing import Any, Iterable

from .models import (
    Audit,
    AuditParseError,
    CourseRef,
    GroupKind,
    RequirementBlock,
    RequirementGroup,
    RequirementStatus,
)

BLOCK_KIND = {
    "DEGREE": GroupKind.DEGREE,
    "MAJOR": GroupKind.MAJOR,
    "MINOR": GroupKind.MINOR,
    "OTHER": GroupKind.GENED,
}

KIND_KEYWORDS: tuple[tuple[tuple[str, ...], GroupKind], ...] = (
    (("core curriculum", "general education", "gen ed"), GroupKind.GENED),
    (("major",), GroupKind.MAJOR),
    (("minor",), GroupKind.MINOR),
    (("degree",), GroupKind.DEGREE),
    (("elective",), GroupKind.ELECTIVE),
    (("residency",), GroupKind.RESIDENCY),
)

IN_PROGRESS_GRADES = {"IP", "PR", "INP", "IPG"}
# Grades that never earn credit toward a requirement and must not satisfy a
# prerequisite. The audit must keep the attempt for display and for retakes.
FAILED_GRADES = {"F", "WF", "FL", "NC", "U", "NP"}
WITHDRAWN_GRADES = {"W", "WP", "DR"}
INCOMPLETE_GRADES = {"I", "INC"}
AUDIT_GRADES = {"AU"}


def course_status(grade: str | None) -> str:
    """Map a Banner/Degree Works letter grade to a course-credit status.

    Only ``completed`` and ``in_progress`` earn credit. Failed, withdrawn,
    incomplete and audit attempts are retained with their own status so a
    student can see them and retake the course.
    """
    value = (grade or "").upper()
    if value in IN_PROGRESS_GRADES:
        return "in_progress"
    if value in FAILED_GRADES:
        return "failed"
    if value in WITHDRAWN_GRADES:
        return "withdrawn"
    if value in INCOMPLETE_GRADES:
        return "incomplete"
    if value in AUDIT_GRADES:
        return "audit"
    return "completed"


def _num(value: Any) -> float | None:
    if value is None or value == "":
        return None
    try:
        return float(str(value).replace(",", "").strip())
    except ValueError:
        return None


def _status(percent: Any, applied: Iterable[CourseRef]) -> RequirementStatus:
    value = _num(percent)
    if value is not None and value >= 100:
        return RequirementStatus.MET
    if any(course.in_progress for course in applied):
        return RequirementStatus.IN_PROGRESS
    return RequirementStatus.NOT_MET


def _kind_for(title: str, default: GroupKind) -> GroupKind:
    lowered = title.lower()
    for keywords, kind in KIND_KEYWORDS:
        if any(keyword in lowered for keyword in keywords):
            return kind
    return default


def _course_code(subject: str, number: str) -> str:
    return f"{subject} {number}".strip()


TITLE_CREDITS = re.compile(r"-\s*(?P<low>\d+(?:\.\d+)?)(?:-(?P<high>\d+(?:\.\d+)?))?\s+credits?\b", re.IGNORECASE)


def _credits_from_title(title: str) -> tuple[float | None, float | None]:
    """``"LEADERSHIP - 6-7 credits"`` -> (6.0, 7.0); ``"- 22 credits"`` -> (22, 22)."""
    match = TITLE_CREDITS.search(title)
    if not match:
        return None, None
    low = float(match.group("low"))
    high = float(match.group("high")) if match.group("high") else low
    return low, high


class _AuditParser:
    def __init__(self, payload: dict[str, Any]) -> None:
        self.payload = payload
        self.class_titles: dict[str, str] = {}
        self.class_records: dict[str, dict[str, Any]] = {}
        for course in (payload.get("classInformation") or {}).get("classArray") or []:
            code = _course_code(course.get("discipline") or "", course.get("number") or "")
            if course.get("courseTitle"):
                self.class_titles[code] = course["courseTitle"]
            if code:
                self.class_records.setdefault(code, course)

    # ------------------------------------------------------------------ header
    def parse(self) -> Audit:
        header = self.payload.get("auditHeader") or {}
        degree_info = self.payload.get("degreeInformation") or {}
        degree_rows = degree_info.get("degreeDataArray") or []
        degree_row = degree_rows[0] if degree_rows else {}

        goals = degree_info.get("goalArray") or []
        majors = [g.get("valueLiteral") or g.get("value") or "" for g in goals if g.get("code") == "MAJOR"]
        minors = [g.get("valueLiteral") or g.get("value") or "" for g in goals if g.get("code") == "MINOR"]

        earned = sum(
            _num(header.get(key)) or 0
            for key in ("residentApplied", "transferApplied", "examAppliedCredits")
        )
        in_progress = sum(
            _num(header.get(key)) or 0
            for key in ("residentAppliedInProgress", "transferAppliedInProgress", "examAppliedInProgress")
        )

        groups = [self._parse_block(block) for block in self.payload.get("blockArray") or []]
        if not groups:
            raise AuditParseError("Audit JSON contains no blockArray entries.")

        return Audit(
            student_name=header.get("studentName") or "",
            student_id=header.get("studentId") or degree_row.get("studentId") or "",
            catalog_year=degree_row.get("catalogYearLit")
            or degree_row.get("catalogYear")
            or (self.payload.get("blockArray") or [{}])[0].get("catalogYearLit", ""),
            majors=majors,
            minors=minors,
            credits_earned=earned,
            credits_in_progress=in_progress,
            gpa=_num(header.get("studentSystemGpa")) or _num(degree_row.get("studentSystemCumulativeGpa")),
            groups=groups,
            raw_ref=header.get("auditId") or "",
        )

    # ------------------------------------------------------------------- blocks
    def _parse_block(self, block: dict[str, Any]) -> RequirementGroup:
        kind = BLOCK_KIND.get(block.get("requirementType") or "", GroupKind.OTHER)
        required, earned, remaining = self._block_credits(block)
        rules = block.get("ruleArray") or []
        status = _status(block.get("percentComplete"), self._applied_everywhere(rules))
        group = RequirementGroup(
            id=block.get("requirementId") or "",
            title=block.get("title") or "",
            kind=kind,
            required_credits=required,
            earned_credits=earned,
            remaining_credits=remaining,
            status=status,
            notes=block.get("header", {}).get("notes") or "",
            blocks=[],
            sub_groups=[],
        )
        for rule in rules:
            self._attach_rule(group, rule, kind, prefix=group.id)
        return group

    def _block_credits(self, block: dict[str, Any]) -> tuple[float | None, float | None, float | None]:
        qualifiers = (block.get("header") or {}).get("qualifierArray") or []
        for qualifier in qualifiers:
            if qualifier.get("name") == "CLASSESCREDITS":
                required = _num(qualifier.get("credits"))
                applied = _num(qualifier.get("applied"))
                needed = _num(qualifier.get("needed"))
                if needed is None and required is not None and applied is not None:
                    needed = max(0.0, required - applied)
                return required, applied, needed
        return None, _num(block.get("creditsApplied")), None

    def _applied_everywhere(self, rules: Iterable[dict[str, Any]]) -> list[CourseRef]:
        collected: list[CourseRef] = []

        def visit(rule: dict[str, Any]) -> None:
            collected.extend(self._applied(rule))
            for child in rule.get("ruleArray") or []:
                visit(child)

        for rule in rules:
            visit(rule)
        return collected

    # -------------------------------------------------------------------- rules
    def _attach_rule(
        self,
        group: RequirementGroup,
        rule: dict[str, Any],
        parent_kind: GroupKind,
        *,
        prefix: str,
    ) -> None:
        children = rule.get("ruleArray") or []
        rule_id = f"{prefix}:{rule.get('ruleId') or rule.get('nodeId') or len(group.blocks) + 1}"
        if children:
            title = rule.get("label") or ""
            if rule.get("ruleType") == "Complete":
                group.blocks.append(
                    RequirementBlock(
                        label=title,
                        status=_status(rule.get("percentComplete"), []),
                        notes="Complete/waiver node",
                    )
                )
                return
            applied = self._applied_everywhere([rule])
            low, high = _credits_from_title(title)
            required = high
            earned = sum(course.credits or 0 for course in applied)
            notes = f"{rule.get('ruleType', '')} node".strip()
            if low is not None and high is not None and high != low:
                notes = f"{notes}; requires {low:g}-{high:g} credits".strip("; ")
            sub = RequirementGroup(
                id=rule_id,
                title=title,
                kind=_kind_for(title, parent_kind),
                required_credits=required,
                earned_credits=round(earned, 2) if applied else None,
                remaining_credits=max(0.0, round(high - earned, 2)) if high is not None else None,
                status=_status(rule.get("percentComplete"), applied),
                notes=notes,
            )
            for child in children:
                self._attach_rule(sub, child, sub.kind, prefix=sub.id)
            group.sub_groups.append(sub)
            return

        group.blocks.append(self._parse_leaf_rule(rule))

    def _parse_leaf_rule(self, rule: dict[str, Any]) -> RequirementBlock:
        requirement = rule.get("requirement") or {}
        applied = self._applied(rule)
        options = list(applied)
        seen = {course.code for course in applied if course.code}
        for option in requirement.get("courseArray") or []:
            ref = self._option_ref(option)
            if ref.code and ref.code in seen:
                continue
            if not ref.code and not ref.satisfies_notes:
                continue
            seen.add(ref.code)
            options.append(ref)

        pick = None
        if requirement.get("classesBegin"):
            try:
                pick = int(requirement["classesBegin"])
            except (TypeError, ValueError):
                pick = None

        notes = []
        if requirement.get("creditsBegin"):
            credits = requirement["creditsBegin"]
            high = requirement.get("creditsEnd")
            notes.append(
                f"choose {credits} credits from the following"
                if not high or high == credits
                else f"choose {credits}-{high} credits from the following"
            )
        for qualifier in requirement.get("qualifierArray") or []:
            notes.append(self._qualifier_note(qualifier))
        if requirement.get("except"):
            notes.append(f"except: {requirement['except']}")

        return RequirementBlock(
            label=rule.get("label") or "",
            pick_n_of=pick,
            status=_status(rule.get("percentComplete"), applied),
            options=options,
            notes="; ".join(note for note in notes if note),
        )

    def _qualifier_note(self, qualifier: dict[str, Any]) -> str:
        text = qualifier.get("text")
        if text:
            subjects = ", ".join(qualifier.get("subTextList") or [])
            return f"qualifier: {text}{subjects}".strip()
        parts = [str(qualifier.get(key)) for key in ("attribute", "operator", "value", "grade", "credits") if qualifier.get(key)]
        return f"qualifier: {' '.join(parts)}" if parts else ""

    def _option_ref(self, option: dict[str, Any]) -> CourseRef:
        subject = (option.get("discipline") or "").strip()
        number = (option.get("number") or "").strip()
        if subject == "@":
            subject = ""
        if number == "@":
            number = ""
        notes = []
        for condition in option.get("withArray") or []:
            code = condition.get("code") or ""
            values = ", ".join(str(value) for value in condition.get("valueList") or [])
            if code and values:
                notes.append(f"{code} {condition.get('operator', '=')} {values}")
        code = _course_code(subject, number)
        return CourseRef(
            subject=subject,
            number=number,
            title=self.class_titles.get(code, ""),
            status="option",
            satisfies_notes="; ".join(notes),
        )

    def _applied(self, rule: dict[str, Any]) -> list[CourseRef]:
        entries = (rule.get("classesAppliedToRule") or {}).get("classArray") or []
        refs: list[CourseRef] = []
        for entry in entries:
            subject = entry.get("discipline") or ""
            number = entry.get("number") or ""
            grade = (entry.get("letterGrade") or "").upper()
            in_progress = grade in IN_PROGRESS_GRADES
            status = course_status(grade)
            record = self.class_records.get(_course_code(subject, number), {})
            transfer_type = (record.get("transferType") or entry.get("transferType") or "").upper()
            transfer_marker = (record.get("transfer") or entry.get("transfer") or "").strip().upper()
            is_transfer = (
                transfer_type in {"TR", "AP"}
                or transfer_marker == "T"
                or grade in {"AP", "TA", "TR"}
            )
            refs.append(
                CourseRef(
                    subject=subject,
                    number=number,
                    title=self.class_titles.get(_course_code(subject, number), ""),
                    credits=_num(entry.get("credits")),
                    grade=None if in_progress else grade,
                    status=status,
                    term_taken=entry.get("term"),
                    in_progress=in_progress,
                    transfer=is_transfer,
                    ap_credit=transfer_type == "AP" or grade == "AP",
                )
            )
        return refs


def parse_audit(payload: dict[str, Any]) -> Audit:
    """Parse a Degree Works audit JSON payload."""
    if not isinstance(payload, dict) or "blockArray" not in payload:
        raise AuditParseError(
            "Payload is not a Degree Works audit: expected keys 'auditHeader' and 'blockArray'."
        )
    return _AuditParser(payload).parse()


def looks_like_html(text: str) -> bool:
    return bool(re.search(r"<\s*(html|body|div|section)\b", text[:2000], re.IGNORECASE))
