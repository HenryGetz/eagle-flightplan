#!/usr/bin/env python
"""Re-emit artifacts that pair a table with an ASCII grid, with a CRN assertion.

    python scripts/reemit_artifacts.py

Read-only. Fails loudly if a grid's section set differs from its table's CRN set.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from rich.console import Console

from unw_planner.banner.plan import PlanClient
from unw_planner.banner.search import load_snapshot
from unw_planner.planner import RatingProvider
from unw_planner.schedule import render_grid_checked

console = Console()
CAPTURES = Path("captures")
TERM = "202620"


def rmp_text(rating) -> str:
    return f"{rating.rating:.1f}/5 (n={rating.num_ratings}), diff {rating.difficulty}" if rating.rated else "unrated"


def sections_for(plan, snapshot):
    sections = []
    for course in plan.courses:
        section = snapshot.by_crn(course.crn)
        if section is None:
            raise SystemExit(f"CRN {course.crn} missing from snapshot {snapshot.term}")
        sections.append(section)
    return sections


def plan_markdown(plan, sections, ratings, checked: bool = True) -> str:
    crns = [section.crn for section in sections]
    grid = render_grid_checked(
        [section.to_schedule_section() for section in sections],
        expected_crns=crns,
        include_tba=True,
    ) if checked else ""
    lines = [
        f"# {plan.name}",
        "",
        f"Plan id {plan.id} · term {plan.term} · Banner total {plan.total_planned_credit_hours:g} credits · "
        f"fixed credits {sum((s.credits or 0) for s in sections):g} · grid CRN check: OK",
        "",
        "| CRN | Course | Title | Instructor | RMP | Days | Time | Cr |",
        "| --- | --- | --- | --- | --- | --- | --- | --- |",
    ]
    for section in sections:
        rating = ratings.get(section.faculty[0].name if section.faculty else "Staff")
        lines.append(
            f"| {section.crn} | {section.code}-{section.section} | {section.title} | "
            f"{section.faculty[0].name if section.faculty else 'Staff'} | {rmp_text(rating)} | "
            f"{section.day_label} | {section.time_label} | {section.credits:g} |"
        )
    lines += ["", "```", grid, "```"]
    return "\n".join(lines) + "\n"


def reemit_plan_artifacts() -> list[str]:
    snapshot = load_snapshot(TERM)
    if snapshot is None:
        raise SystemExit("202620 snapshot missing")
    ratings = RatingProvider()
    plans = PlanClient().list_plans(TERM)
    written = []
    keymap = {"Plan A": "A", "Plan B": "B", "Plan C": "C"}
    for plan in plans:
        key = next((value for prefix, value in keymap.items() if plan.name.startswith(prefix)), None)
        if key is None:
            continue
        sections = sections_for(plan, snapshot)
        markdown = plan_markdown(plan, sections, ratings)
        (CAPTURES / "plans" / f"{key}_202620.md").write_text(markdown, encoding="utf-8")
        existing = CAPTURES / "plans" / f"{key}_202620.json"
        payload = json.loads(existing.read_text()) if existing.exists() else {}
        payload.update({
            "plan_name": plan.name,
            "plan_id": plan.id,
            "term": plan.term,
            "banner_total_credits": plan.total_planned_credit_hours,
            "crns": [section.crn for section in sections],
            "grid": render_grid_checked(
                [section.to_schedule_section() for section in sections],
                expected_crns=[section.crn for section in sections],
                include_tba=True,
            ),
        })
        existing.write_text(json.dumps(payload, indent=1), encoding="utf-8")
        written.append(str(existing))
        written.append(str(CAPTURES / "plans" / f"{key}_202620.md"))
    return written


def reemit_advisor_plan() -> list[str]:
    snapshot = load_snapshot(TERM)
    ratings = RatingProvider()
    plan = next(p for p in PlanClient().list_plans(TERM) if p.name == "Advisor Plan - Spring 2027")
    sections = sorted(sections_for(plan, snapshot), key=lambda s: s.crn)
    path = CAPTURES / "advisor_plan_202620.md"
    old = path.read_text(encoding="utf-8") if path.exists() else ""
    tail = old[old.index("## Rationale"):] if "## Rationale" in old else ""
    markdown = plan_markdown(plan, sections, ratings)
    markdown = markdown.replace(
        f"Banner total {plan.total_planned_credit_hours:g} credits · fixed credits "
        f"{sum((s.credits or 0) for s in sections):g}",
        f"Banner total {plan.total_planned_credit_hours:g} credits (16 intended; COS 4841 variable 1-2 stored at 1)",
    )
    markdown += "\n" + tail
    path.write_text(markdown, encoding="utf-8")
    return [str(path)]


def reemit_roadmap() -> list[str]:
    fall = load_snapshot("202710")
    spring = load_snapshot("202720")
    ratings = RatingProvider()
    fall_crns = ["14076", "14077", "14075", "12546", "13983"]
    spring_crns = ["23189", "22322", "22352", "23088"]
    fall_sections = [fall.by_crn(crn) for crn in fall_crns]
    spring_sections = [spring.by_crn(crn) for crn in spring_crns]
    for section in fall_sections + spring_sections:
        if section is None:
            raise SystemExit("snapshot is missing a roadmap CRN")
    spring_grid = render_grid_checked(
        [section.to_schedule_section() for section in spring_sections],
        expected_crns=spring_crns,
        include_tba=True,
    )
    fall_grid = render_grid_checked(
        [section.to_schedule_section() for section in fall_sections],
        expected_crns=fall_crns,
        include_tba=True,
    )
    path = CAPTURES / "roadmap_advisor_3term.md"
    old = path.read_text(encoding="utf-8") if path.exists() else ""
    tail = old[old.index("## Underload analysis"):] if "## Underload analysis" in old else ""

    def table(sections):
        lines = [
            "| CRN | Course | Title | Instructor | RMP | Days | Time | Cr | Seats |",
            "| --- | --- | --- | --- | --- | --- | --- | --- | --- |",
        ]
        for section in sections:
            rating = ratings.get(section.faculty[0].name if section.faculty else "Staff")
            lines.append(
                f"| {section.crn} | {section.code}-{section.section} | {section.title} | "
                f"{section.faculty[0].name if section.faculty else 'Staff'} | {rmp_text(rating)} | "
                f"{section.day_label} | {section.time_label} | {section.credits:g} | {section.seats_available} |"
            )
        return lines

    lines = [
        "# Roadmap — Advisor Plan Spring 2027 → Fall 2027 → Spring 2028",
        "",
        "Spring 2027 is fixed as the Advisor Plan (MIN 3206, PHI 2016, BIB 4360, CYS 4855, COS 4841 = 16 cr). "
        "Fall 2027 and Spring 2028 are documents only (searchable but not plannable in Banner).",
        "",
        "## Fall 2027 (202710) — 16 credits, full-time",
        "",
        *table(fall_sections),
        "",
        f"CRN check: table {sorted(fall_crns)} == grid input {sorted(s.crn for s in fall_sections)}.",
        "",
        "**Fall 2027 is 100% TBA.** Every placed section has no published meeting time: CYS 4245/4466/3185 are "
        "unassigned and unpublished, and COS 2005-D01 / COS 4841 are asynchronous. The `find_conflicts = []` "
        "result is therefore vacuous, not evidence of a good timetable; the Fall 2027 schedule must be "
        "re-checked once times and instructors publish.",
        "",
        "```",
        fall_grid,
        "```",
        "",
        "## Spring 2028 (202720) — 12 credits, full-time (exactly)",
        "",
        *table(spring_sections),
        "",
        f"CRN check: table {sorted(spring_crns)} == grid input {sorted(s.crn for s in spring_sections)}.",
        "BIB 3223 is asynchronous (TBA) and appears only in the TBA footer, not on a weekday.",
        "",
        "```",
        spring_grid,
        "```",
        "",
        tail,
    ]
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return [str(path)]


def main() -> None:
    written = reemit_plan_artifacts() + reemit_advisor_plan() + reemit_roadmap()
    for item in written:
        console.print(f"[green]re-emitted[/] {item}")


if __name__ == "__main__":
    main()
