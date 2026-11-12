#!/usr/bin/env python
"""Advising artifacts: full candidate table + 4-term roadmap (read-only).

    python scripts/advise.py --candidates
    python scripts/advise.py --roadmap

No Banner writes. Uses cached term snapshots, ratings and prerequisite text.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

import typer
from rich.console import Console

from eagle_flightplan.audit import AuditClient
from eagle_flightplan.banner.search import TermSnapshot, load_snapshot
from eagle_flightplan.catalog import (
    Availability,
    Requirement,
    applied_codes,
    availability_of,
    requirements_from_audit,
    resolve,
    term_label,
)
from eagle_flightplan.planner import (
    Constraints,
    DraftPlan,
    Planner,
    PrereqProvider,
    RatingProvider,
    Selection,
)
from eagle_flightplan.schedule import render_grid

app = typer.Typer(add_completion=False)
console = Console()
CAPTURES = Path("captures")
TERM = "202620"
CURRENT_TERM = "202610"
ROADMAP_TERMS = ["202620", "202710", "202720"]


def availability_text(section, current_term: str = CURRENT_TERM) -> str:
    state = availability_of(section, current_term)
    if state is Availability.OPEN:
        return "OPEN"
    if state is Availability.NOT_YET_OPEN:
        return "NOT_YET_OPEN"
    return "CLOSED"


def rating_for(name: str, provider: RatingProvider):
    return provider.get(name) if name else provider.get("Staff")


def candidate_rows(requirement: Requirement, snapshot: TermSnapshot, passed: set[str], ratings: RatingProvider, prereqs: PrereqProvider, current_term: str):
    resolution = resolve(requirement, snapshot, exclude_codes=passed, open_only=False, current_term=current_term)
    rows = []
    for section in resolution.candidates:
        name = section.faculty[0].name if section.faculty else "Staff"
        rating = ratings.get(name)
        ok, why = prereqs.check(section, passed)
        rows.append({
            "section": section,
            "rating": rating,
            "prereq_ok": ok,
            "prereq_note": why,
            "availability": availability_text(section, current_term),
        })
    rows.sort(key=lambda row: (
        0 if row["rating"].rated else 1,
        -(row["rating"].effective_rating),
        -(row["section"].credits or 0),
        row["section"].crn,
    ))
    return resolution, rows


def candidates_markdown(audit, snapshot, ratings, prereqs) -> str:
    passed = applied_codes(audit)
    requirements = [r for r in requirements_from_audit(audit) if not r.satisfied]
    lines = [
        f"# Spring 2027 ({TERM}) candidate sections",
        "",
        "Every section matching every remaining requirement, ranked by Rate My Professors rating "
        "(unrated last). Availability: OPEN = seats > 0; NOT_YET_OPEN = 0 seats in a future term; "
        "CLOSED = 0 seats in a registerable term. Eligibility = Banner prerequisite text vs completed/in-progress courses.",
        "",
    ]
    for requirement in requirements:
        resolution, rows = candidate_rows(requirement, snapshot, passed, ratings, prereqs, CURRENT_TERM)
        eligible = [row for row in rows if row["prereq_ok"]]
        best = eligible[0] if eligible else None
        best_any = rows[0] if rows else None
        lines += [f"## {requirement.label} — {requirement.parent_group} ({requirement.priority.value})", ""]
        target = requirement.search_label
        credits_note = f"; needs {requirement.remaining_credits:g} cr" if requirement.remaining_credits else ""
        lines += [f"Target: {target}{credits_note}. Candidates: {len(rows)}.", ""]
        lines += [
            "| CRN | Course | Title | Instructor | RMP | Difficulty | Days | Time | Cr | Seats | Availability | Attributes | Eligibility |",
            "| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |",
        ]
        for row in rows:
            section = row["section"]
            rating = row["rating"]
            rmp = f"{rating.rating:.1f} (n={rating.num_ratings})" if rating.rated else "unrated"
            difficulty = f"{rating.difficulty:.1f}" if rating.difficulty is not None else "-"
            eligibility = "OK" if row["prereq_ok"] else (f"BLOCKED: {row['prereq_note']}" if row["prereq_note"] else "BLOCKED")
            lines.append(
                f"| {section.crn} | {section.code}-{section.section} | {section.title} | {section.faculty[0].name if section.faculty else 'Staff'} | "
                f"{rmp} | {difficulty} | {section.day_label} | {section.time_label} | "
                f"{section.credits if section.credits is not None else '-'} | {section.seats_available if section.seats_available is not None else '-'} | "
                f"{row['availability']} | {', '.join(sorted(section.attribute_codes)) or '-'} | {eligibility} |"
            )
        if best:
            r = best["rating"]
            rating_text = f"{r.rating:.1f}/5 (n={r.num_ratings})" if r.rated else "unrated"
            note = f"Best-rated eligible: {best['section'].code}-{best['section'].section} (CRN {best['section'].crn}), {rating_text}, {best['section'].credits:g} cr."
        else:
            note = "No eligible candidate in Spring 2027."
        if best_any and (not best or best_any["section"].crn != best["section"].crn):
            r = best_any["rating"]
            rating_text = f"{r.rating:.1f}/5 (n={r.num_ratings})" if r.rated else "unrated"
            note += (
                f" Highest-rated overall is {best_any['section'].code}-{best_any['section'].section} "
                f"(CRN {best_any['section'].crn}), {rating_text}, but it is "
                + ("prereq-blocked." if not best_any["prereq_ok"] else "not eligible for another reason.")
            )
        lines += ["", note, ""]
    return "\n".join(lines) + "\n"


def plan_a_crns() -> list[str]:
    path = CAPTURES / "banner_plans_202620.json"
    if path.exists():
        plans = json.loads(path.read_text())
        for plan in plans:
            if plan["name"].startswith("Plan A"):
                return [course["crn"] for course in plan["courses"]]
    return ["22201", "20226", "20737", "23109", "23189"]


def selection_for(section, requirement: Requirement, planner: Planner, constraints: Constraints) -> Selection:
    name, rating = planner._best_instructor(section, constraints)
    state = availability_of(section, CURRENT_TERM)
    return Selection(
        requirement_id=requirement.id if requirement else "plan-a",
        requirement=requirement.label if requirement else "Plan A (as written in Banner)",
        group=requirement.parent_group if requirement else "",
        crn=section.crn,
        course=section.code,
        section=section.section,
        title=section.title,
        instructor=name,
        rating=rating,
        times=section.time_label,
        days=section.day_label,
        credits=section.credits,
        seats=section.seats_available,
        open=section.open,
        availability=state.value,
        verify_at_registration=state is not Availability.OPEN,
        meets=[f"{m.day.value} {m.start.strftime('%H:%M')}-{m.end.strftime('%H:%M')} {m.location}".strip() for m in section.meetings if m.scheduled],
        covers=[requirement.id] if requirement else [],
        rationale="as written in Banner Plan A",
    )


def requirement_for(section, requirements: list[Requirement]) -> Requirement | None:
    for requirement in requirements:
        if section.code in {target.code for target in requirement.targets}:
            return requirement
        if requirement.attributes and set(requirement.attributes) & section.attribute_codes:
            return requirement
    return None


def roadmap_markdown(audit, snapshots, planner, constraints) -> str:
    requirements = [r for r in planner.requirements]
    progress = {r.id: r.applied_credits for r in requirements}
    counts = {r.id: r.applied_count for r in requirements}
    passed = set(applied_codes(audit))
    lines = ["# Roadmap — Spring 2027 → Fall 2027 → Spring 2028 (no summer)", ""]
    lines += [f"Spring 2027 is Plan A as written in Banner (plan id 59003). Terms: {', '.join(term_label(t) for t in ROADMAP_TERMS)}.", ""]

    # Spring 2027 = Plan A
    spring_snapshot = snapshots[TERM]
    spring_sections = [spring_snapshot.by_crn(crn) for crn in plan_a_crns()]
    spring_sections = [s for s in spring_sections if s is not None]
    spring_selections = []
    for section in spring_sections:
        requirement = requirement_for(section, requirements)
        spring_selections.append(selection_for(section, requirement, planner, constraints))
        passed.add(section.code)
        for req in requirements:
            if (requirement and req.id == requirement.id) or (
                req.attributes and set(req.attributes) & section.attribute_codes
            ) or section.code in {t.code for t in req.targets}:
                progress[req.id] = progress.get(req.id, 0) + (section.credits or 0)
                counts[req.id] = counts.get(req.id, 0) + 1
    remaining = [r for r in requirements if not Planner._satisfied(r, progress, counts)]

    def emit(term, draft):
        nonlocal lines
        lines.append(f"## {term_label(term)} ({term})")
        lines.append("")
        lines.append("| CRN | Course | Title | Instructor | RMP | Days | Time | Cr | Availability |")
        lines.append("| --- | --- | --- | --- | --- | --- | --- | --- | --- |")
        for selection in draft.selections:
            rmp = f"{selection.rating.rating:.1f} (n={selection.rating.num_ratings})" if selection.rating.rated else "unrated"
            lines.append(
                f"| {selection.crn} | {selection.course}-{selection.section} | {selection.title} | {selection.instructor} | {rmp} | {selection.days} | {selection.times} | "
                f"{selection.credits if selection.credits is not None else '-'} | {selection.availability} |"
            )
        lines += ["", f"Credits: {draft.total_credits:g}", ""]
        if draft.warnings:
            lines += [f"* {warning}" for warning in draft.warnings] + [""]

    spring_draft = DraftPlan(term=TERM, label=term_label(TERM), selections=spring_selections,
                             total_credits=round(sum(s.credits or 0 for s in spring_selections), 2),
                             grid=render_grid([s.to_schedule_section() for s in spring_sections], include_tba=True))
    emit(TERM, spring_draft)
    lines += ["Still open after Spring 2027:"] + [f"* {r.label} ({r.parent_group})" for r in remaining] + [""]

    for term in ROADMAP_TERMS[1:]:
        snapshot = snapshots.get(term)
        if snapshot is None:
            lines += [f"## {term_label(term)} ({term})", "", "_Catalog not published._", ""]
            continue
        adjusted = [
            r.model_copy(update={"applied_credits": progress.get(r.id, r.applied_credits),
                                 "applied_count": counts.get(r.id, r.applied_count)})
            for r in remaining
        ]
        draft = planner.draft(term, constraints, requirements=adjusted, passed=passed)
        for selection in draft.selections:
            section = snapshot.by_crn(selection.crn)
            passed.add(selection.course)
            for req in remaining:
                match_code = selection.course in {t.code for t in req.targets}
                match_attr = bool(section and req.attributes and set(req.attributes) & section.attribute_codes)
                if match_code or match_attr:
                    progress[req.id] = progress.get(req.id, 0) + (selection.credits or 0)
                    counts[req.id] = counts.get(req.id, 0) + 1
        remaining = [r for r in remaining if not Planner._satisfied(r, progress, counts)]
        emit(term, draft)
        lines += ["Still open after this term:"] + [f"* {r.label} ({r.parent_group})" for r in remaining] + [""]

    lines += ["## Graduation risk", ""]
    single = []
    unassigned = []
    zero_seat = []
    for term in ROADMAP_TERMS[1:]:
        snapshot = snapshots.get(term)
        if snapshot is None:
            continue
        for code in ["CYS 4245", "CYS 4466", "CYS 3185", "BIB 4825", "BIB 6312"]:
            sections = snapshot.by_course(*code.split())
            if sections:
                single.append(f"{code} in {term_label(term)}: {len(sections)} section(s)")
                for section in sections:
                    if not section.faculty or all("unassigned" in person.name.lower() for person in section.faculty):
                        unassigned.append(f"{code} {section.crn} in {term_label(term)}: faculty unassigned")
                    if (section.seats_available or 0) == 0:
                        zero_seat.append(f"{code} {section.crn} in {term_label(term)}: 0 seats / not yet open")
    lines += ["### Single-section courses", ""] + [f"* {item}" for item in single] + [""]
    lines += ["### Unassigned faculty", ""] + [f"* {item}" for item in unassigned] + [""]
    lines += ["### Zero-seat / not-yet-open sections", ""] + [f"* {item}" for item in zero_seat] + [""]
    lines += [
        "### Consequence if one is cancelled or moved",
        "* CYS 4245, CYS 4466 and CYS 3185 are Fall-only in every observed term. Losing the Fall 2027 section pushes the course to Fall 2028 (catalog not published), which delays graduation by a year unless an override/independent study is approved.",
        "* CYS 3185 is currently 0 seats / unassigned in the Fall 2027 draft catalog. If it does not open, Server Administration remains the single unmet Cybersecurity requirement.",
        "* If CYS 4245 or CYS 4466 moves to Spring, the Spring 2028 load must absorb it; the current plan has room there.",
    ]
    return "\n".join(lines) + "\n"


@app.command()
def main(
    candidates: bool = typer.Option(False, "--candidates"),
    roadmap: bool = typer.Option(False, "--roadmap"),
) -> None:
    CAPTURES.mkdir(parents=True, exist_ok=True)
    audit = AuditClient().audit()
    snapshots = {term: load_snapshot(term) for term in ROADMAP_TERMS}
    snapshots = {term: snapshot for term, snapshot in snapshots.items() if snapshot is not None}
    ratings = RatingProvider()
    prereqs = PrereqProvider()
    planner = Planner(audit, snapshots, ratings=ratings, prereqs=prereqs, current_term=CURRENT_TERM)

    if candidates:
        if TERM not in snapshots:
            raise typer.Exit("Spring 2027 snapshot missing")
        markdown = candidates_markdown(audit, snapshots[TERM], ratings, prereqs)
        (CAPTURES / "candidates_202620.md").write_text(markdown, encoding="utf-8")
        console.print("[green]wrote[/] captures/candidates_202620.md")

    if roadmap:
        constraints = Constraints(max_credits=16)
        markdown = roadmap_markdown(audit, snapshots, planner, constraints)
        (CAPTURES / "roadmap_4term.md").write_text(markdown, encoding="utf-8")
        console.print("[green]wrote[/] captures/roadmap_4term.md")


if __name__ == "__main__":
    app()
