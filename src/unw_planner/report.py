"""One-shot advising brief: remaining requirements, candidates, draft, risks.

`build_brief()` is deterministic (no timestamps, sorted rows) and name-free by
default so the markdown can be shared. Empty results always carry the reason.
"""

from __future__ import annotations

from typing import Any, Iterable

from pydantic import BaseModel, Field

from .audit import Audit, AuditClient
from .banner.search import TermSnapshot, load_snapshot
from .catalog import (
    Availability,
    Requirement,
    applied_codes,
    availability_of,
    requirements_from_audit,
    resolve,
    term_label,
)
from .models import Section
from .planner import (
    Constraints,
    Planner,
    PrereqProvider,
    RatingProvider,
    verify_draft,
)
from .schedule import find_conflicts

CURRENT_TERM = "202610"


class BriefOptions(BaseModel):
    term: str
    max_credits: float = 16
    include_roadmap: bool = False
    include_pii: bool = False


class CandidateRow(BaseModel):
    crn: str
    course: str
    section: str
    title: str
    instructor: str
    rating: float | None = None
    num_ratings: int = 0
    difficulty: float | None = None
    days: str = ""
    times: str = ""
    credits: float | None = None
    seats: int | None = None
    availability: str = ""
    attributes: list[str] = Field(default_factory=list)
    prereq_ok: bool | None = None
    prereq_note: str = ""
    reason_rejected: str = ""


class RequirementBlock(BaseModel):
    group: str
    requirement: str
    priority: str
    remaining_credits: float | None = None
    options: str = ""
    candidates: list[CandidateRow] = Field(default_factory=list)


class Brief(BaseModel):
    term: str
    term_label: str
    markdown: str
    data: dict[str, Any]


def _rmp(provider: RatingProvider, name: str):
    return provider.get(name or "Staff")


def _candidate_rows(
    requirement: Requirement,
    snapshot: TermSnapshot,
    passed: set[str],
    ratings: RatingProvider,
    prereqs: PrereqProvider,
) -> tuple[list[CandidateRow], str]:
    resolution = resolve(
        requirement,
        snapshot,
        exclude_codes=passed,
        open_only=False,
        current_term=CURRENT_TERM,
    )
    rows: list[CandidateRow] = []
    for section in resolution.candidates:
        name = section.faculty[0].name if section.faculty else "Staff"
        rating = ratings.get(name)
        ok, note = prereqs.check(section, passed)
        rows.append(
            CandidateRow(
                crn=section.crn,
                course=section.code,
                section=section.section,
                title=section.title,
                instructor=name,
                rating=rating.rating,
                num_ratings=rating.num_ratings,
                difficulty=rating.difficulty,
                days=section.day_label,
                times=section.time_label,
                credits=section.credits,
                seats=section.seats_available,
                availability=availability_of(section, CURRENT_TERM).value,
                attributes=sorted(section.attribute_codes),
                prereq_ok=ok,
                prereq_note=note,
            )
        )
    rows.sort(key=_row_sort)
    return rows, resolution.reason


def _row_sort(row: CandidateRow) -> tuple:
    rated = row.rating is not None and row.num_ratings > 0
    effective = (row.rating or 0.0) * (0.85 if 0 < row.num_ratings < 3 else 1.0)
    return (0 if rated else 1, -effective, -(row.credits or 0), row.crn)


def _rating_label(row: CandidateRow) -> str:
    if row.rating is None or row.num_ratings == 0:
        return "unrated"
    return f"{row.rating:.1f}/5 (n={row.num_ratings})"


def _candidate_table(rows: Iterable[CandidateRow]) -> list[str]:
    lines = [
        "| CRN | Course | Title | Instructor | RMP | Difficulty | Days | Time | Cr | Seats | Availability | Attributes | Prereq |",
        "| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |",
    ]
    for row in rows:
        difficulty = f"{row.difficulty:.1f}" if row.difficulty is not None else "-"
        if row.prereq_ok is True:
            prereq = "OK"
        elif row.prereq_ok is None:
            prereq = f"unknown ({row.prereq_note})" if row.prereq_note else "unknown"
        else:
            prereq = row.prereq_note or "blocked"
        lines.append(
            f"| {row.crn} | {row.course}-{row.section} | {row.title} | {row.instructor} | "
            f"{_rating_label(row)} | {difficulty} | {row.days} | {row.times} | "
            f"{row.credits if row.credits is not None else '-'} | {row.seats if row.seats is not None else '-'} | "
            f"{row.availability} | {', '.join(row.attributes) or '-'} | {prereq} |"
        )
    return lines


def _rejection_reason(
    row: CandidateRow,
    selected: list[Section],
    picked_credits: float,
    max_credits: float,
    selected_section: Section | None,
) -> str:
    if row.availability == Availability.CLOSED.value:
        return "closed / no seats in a registerable term"
    if row.availability == Availability.NOT_YET_OPEN.value:
        return "not yet open (future term)"
    if row.prereq_ok is False:
        return f"prerequisite: {row.prereq_note or 'not met'}"
    candidate = next((section for section in selected if section.crn == row.crn), None)
    if candidate is None:
        return "not selected by the planner"
    clash = find_conflicts(
        [section.to_schedule_section() for section in selected if section.crn != row.crn]
        + [candidate.to_schedule_section()]
    )
    if clash:
        return f"time conflict: {clash[0].summary}"
    if picked_credits - (selected_section.credits if selected_section else 0) + (candidate.credits or 0) > max_credits:
        return "over the credit cap"
    return "not selected by the planner"


def build_brief(
    term: str,
    *,
    max_credits: float = 16,
    include_roadmap: bool = False,
    include_pii: bool = True,
    audit: Audit | None = None,
    snapshot: TermSnapshot | None = None,
    ratings: RatingProvider | None = None,
    prereqs: PrereqProvider | None = None,
) -> Brief:
    audit = audit or AuditClient().audit()
    snapshot = snapshot or load_snapshot(term)
    if snapshot is None:
        raise RuntimeError(f"No cached catalog snapshot for {term}; fetch it first.")
    ratings = ratings or RatingProvider()
    prereqs = prereqs or PrereqProvider()
    passed = applied_codes(audit)
    requirements = requirements_from_audit(audit)
    outstanding = [requirement for requirement in requirements if not requirement.satisfied]
    planner = Planner(
        audit,
        {term: snapshot},
        ratings=ratings,
        prereqs=prereqs,
        current_term=CURRENT_TERM,
    )
    constraints = Constraints(max_credits=max_credits)
    draft = planner.draft(term, constraints)
    selected_sections = [snapshot.by_crn(selection.crn) for selection in draft.selections]
    selected_sections = [section for section in selected_sections if section is not None]
    conflicts = find_conflicts([section.to_schedule_section() for section in selected_sections])
    problems = verify_draft(draft, snapshot)

    blocks: list[RequirementBlock] = []
    for requirement in outstanding:
        rows, empty_reason = _candidate_rows(requirement, snapshot, passed, ratings, prereqs)
        chosen = next((selection for selection in draft.selections if selection.requirement_id == requirement.id), None)
        chosen_section = snapshot.by_crn(chosen.crn) if chosen else None
        if chosen is not None:
            for row in rows:
                if row.crn == chosen.crn:
                    continue
                if row.rating is not None and row.rating > (chosen.rating.rating or 0.0):
                    row.reason_rejected = _rejection_reason(
                        row, selected_sections, draft.total_credits, max_credits, chosen_section
                    )
        blocks.append(
            RequirementBlock(
                group=requirement.parent_group,
                requirement=requirement.label,
                priority=requirement.priority.value,
                remaining_credits=requirement.remaining_credits,
                options=requirement.search_label,
                candidates=rows,
            )
        )
        if not rows:
            blocks[-1].options = f"{requirement.search_label} — no candidate sections ({empty_reason})"

    merged: dict[tuple, RequirementBlock] = {}
    for block in blocks:
        signature = (
            block.requirement,
            block.options,
            tuple((row.crn for row in block.candidates)),
        )
        if signature in merged:
            current = merged[signature].group
            if block.group not in current.split(" / "):
                merged[signature].group = f"{current} / {block.group}"
            elif "(×2)" not in current:
                merged[signature].group = f"{current} (×2)"
            elif "(×2)" in current:
                merged[signature].group = current.replace("(×2)", "(×3)")
            continue
        merged[signature] = block
    blocks = list(merged.values())

    lines = [
        "<!-- classification: internal advising working document -->",
        "# UNW Advising Brief",
        "",
        f"**Term:** {term_label(term)} (`{term}`) · **Credits planned:** {draft.total_credits:g}/{max_credits:g}",
        "",
        "## 1. Student and program",
        "",
        f"* Programs: majors {', '.join(audit.majors) or '—'}; minors {', '.join(audit.minors) or '—'}",
        f"* Catalog: {audit.catalog_year}",
        f"* Credits: {audit.credits_earned:g} earned + {audit.credits_in_progress:g} in progress"
        if audit.credits_earned is not None
        else "* Credits: unavailable",
        f"* GPA: {audit.gpa if audit.gpa is not None else 'unavailable'}",
    ]
    if include_roadmap:
        lines.append("* Projected graduation credits: see section 7")
    lines += ["", "## 2. Remaining requirements", "",
              "| Group | Requirement | Remaining credits | Priority | Options |",
              "| --- | --- | --- | --- | --- |"]
    for block in blocks:
        remaining = f"{block.remaining_credits:g}" if block.remaining_credits is not None else "-"
        lines.append(f"| {block.group} | {block.requirement} | {remaining} | {block.priority} | {block.options} |")

    lines += ["", "## 3. Candidates per requirement", ""]
    for block in blocks:
        lines += [f"### {block.requirement} — {block.group} ({block.priority})", ""]
        if not block.candidates:
            lines += [f"_No offered section: {block.options}_", ""]
            continue
        lines += _candidate_table(block.candidates) + [""]

    lines += ["## 4. Recommended draft", ""]
    lines += ["| CRN | Course | Title | Instructor | RMP | Days | Time | Cr | Rationale |",
              "| --- | --- | --- | --- | --- | --- | --- | --- | --- |"]
    for selection in draft.selections:
        lines.append(
            f"| {selection.crn} | {selection.course}-{selection.section} | {selection.title} | "
            f"{selection.instructor} | {selection.rating.label} | {selection.days} | {selection.times} | "
            f"{selection.credits if selection.credits is not None else '-'} | {selection.rationale} |"
        )
    lines += ["", f"Total credits: **{draft.total_credits:g}**. Conflict check: "
                  + ("**no overlaps**" if not conflicts else "; ".join(conflict.summary for conflict in conflicts))
                  + ("" if not problems else f". Sanity problems: {'; '.join(problems)}"), "",
              "```", draft.grid, "```", ""]

    lines += ["## 5. Why not the others", ""]
    rejected = [row for block in blocks for row in block.candidates if row.reason_rejected]
    if not rejected:
        lines.append("_No higher-rated candidate was passed over._")
    else:
        lines += ["| Requirement | CRN | Course | RMP | Why it lost |", "| --- | --- | --- | --- | --- |"]
        for block in blocks:
            for row in block.candidates:
                if row.reason_rejected:
                    lines.append(
                        f"| {block.requirement} | {row.crn} | {row.course}-{row.section} | "
                        f"{_rating_label(row)} | {row.reason_rejected} |"
                    )
    lines.append("")

    lines += ["## 6. Risks", ""]
    risks: list[str] = []
    for selection in draft.selections:
        section = snapshot.by_crn(selection.crn)
        if section is None:
            continue
        sections = snapshot.by_course(section.subject, section.number)
        if len(sections) == 1:
            risks.append(f"{section.code} has a single section this term (CRN {section.crn}).")
        if not section.faculty or all("unassigned" in person.name.lower() for person in section.faculty):
            risks.append(f"{section.code} CRN {section.crn} has unassigned faculty.")
        if availability_of(section, CURRENT_TERM) is Availability.NOT_YET_OPEN:
            risks.append(f"{section.code} CRN {section.crn} is not yet open (0 seats) — verify at registration.")
        if section.credits_low is not None and section.credits_high is not None and section.credits_low != section.credits_high:
            risks.append(
                f"{section.code} CRN {section.crn} is variable credit ({section.credits_low:g}-{section.credits_high:g}); "
                "credit hours must be set."
            )
    for block in blocks:
        if not block.candidates:
            risks.append(f"{block.requirement}: no offered section can satisfy it this term.")
    if not risks:
        risks.append("No structural risks detected in the selected set.")
    lines += [f"* {risk}" for risk in risks]

    if include_roadmap:
        terms = [term, "202710", "202720"]
        planner_all = Planner(
            audit,
            {candidate: snapshot for candidate in terms if candidate == term}
            | {candidate: load_snapshot(candidate) for candidate in terms if candidate != term and load_snapshot(candidate)},
            ratings=ratings,
            prereqs=prereqs,
            current_term=CURRENT_TERM,
        )
        roadmap = planner_all.roadmap(terms, constraints, include_summer=False)
        lines += ["", "## 7. Roadmap", "", "| Term | Credits | Placements |", "| --- | --- | --- |"]
        for entry in roadmap.terms:
            placements = ", ".join(
                f"{selection.course} ({selection.crn})" for selection in (entry.draft.selections if entry.draft else [])
            ) or "—"
            lines.append(f"| {entry.label} | {entry.draft.total_credits if entry.draft else 0:g} | {placements} |")
        total = (audit.credits_earned or 0) + (audit.credits_in_progress or 0) + sum(
            entry.draft.total_credits for entry in roadmap.terms if entry.draft
        )
        lines += ["", f"Projected graduation credits: **{total:g}** (minimum 125)."]
        if roadmap.final_remaining:
            lines += ["", "Still open after the roadmap:"] + [f"* {item}" for item in roadmap.final_remaining]
        lines.append("")

    markdown = "\n".join(lines) + "\n"
    data = {
        "term": term,
        "student_name": audit.student_name if include_pii else None,
        "term_label": term_label(term),
        "max_credits": max_credits,
        "requirements": [block.model_dump() for block in blocks],
        "draft": draft.model_dump(mode="json"),
        "conflicts": [conflict.model_dump(mode="json") for conflict in conflicts],
        "sanity_problems": problems,
        "risks": risks,
    }
    return Brief(term=term, term_label=term_label(term), markdown=markdown, data=data)
