#!/usr/bin/env python
"""Draft a real term plan (and the multi-term roadmap) for the signed-in student.

    python scripts/draft.py --term 202620
    python scripts/draft.py --roadmap
    python scripts/draft.py --matrix

Read-only: no Banner plan writes. Snapshots are cached under `.state/terms/`.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

import typer
from rich.console import Console
from rich.table import Table

from eagle_flightplan.audit import AuditClient
from eagle_flightplan.banner.search import TermIndex, TermSnapshot, fetch_term, load_snapshot
from eagle_flightplan.catalog import offering_matrix, render_matrix, requirements_from_audit, term_label
from eagle_flightplan.banner.plan import PlanClient
from eagle_flightplan.planner import Constraints, DraftPlan, Planner, Roadmap, verify_draft

app = typer.Typer(add_completion=False)
console = Console()
CAPTURES = Path("captures")
HISTORY_TERMS = ["202520", "202530", "202610", "202620", "202630", "202710", "202720"]
ROADMAP_TERMS = ["202620", "202630", "202710", "202720"]


def index_summary() -> str:
    index = TermIndex()
    try:
        return ", ".join(f"{term}={index.section_count(term)}" for term in index.terms())
    finally:
        index.close()


def load_snapshots(terms: list[str], refresh: bool = False) -> dict[str, TermSnapshot]:
    snapshots: dict[str, TermSnapshot] = {}
    for term in terms:
        snapshot = None if refresh else load_snapshot(term)
        if snapshot is None:
            console.print(f"[yellow]fetching {term}…[/]")
            snapshot = fetch_term(term)
        snapshots[term] = snapshot
    return snapshots


def draft_table(draft: DraftPlan) -> Table:
    table = Table(title=f"{draft.label} ({draft.term}) — {draft.total_credits:g} credits")
    for column in ("CRN", "Course", "Title", "Instructor", "RMP", "Days", "Time", "Cr", "Requirement"):
        table.add_column(column)
    for selection in draft.selections:
        table.add_row(
            selection.crn,
            f"{selection.course}-{selection.section}",
            selection.title[:34],
            selection.instructor,
            selection.rating.label,
            selection.days,
            selection.times,
            f"{selection.credits:g}" if selection.credits is not None else "-",
            selection.requirement[:30],
        )
    return table


PLAN_SPECS = [
    ("A", "Plan A - Max Rated 16cr", "A_max_rated", 16, Constraints(max_credits=16)),
    ("B", "Plan B - Major Forward 18cr", "B_major_forward", 18, Constraints(max_credits=18)),
    ("C", "Plan C - Light 13cr", "C_light", 13, Constraints(max_credits=13, include_optional=False)),
]


def average_rating(draft: DraftPlan) -> tuple[float, int]:
    rated = [selection.rating for selection in draft.selections if selection.rating.rated]
    if not rated:
        return 0.0, 0
    return sum(r.rating or 0 for r in rated) / len(rated), len(rated)


def write_plan_artifacts(plans: list[tuple[str, str, str, DraftPlan]]) -> None:
    directory = CAPTURES / "plans"
    directory.mkdir(parents=True, exist_ok=True)
    for key, name, slug, draft in plans:
        average, count = average_rating(draft)
        payload = draft.model_dump(mode="json")
        payload["plan_name"] = name
        payload["average_rmp"] = round(average, 2)
        payload["rated_picks"] = count
        (directory / f"{key}_202620.json").write_text(json.dumps(payload, indent=1), encoding="utf-8")
        markdown = draft_markdown(draft)
        markdown = markdown.replace("# Draft plan", f"# {name} — draft plan", 1)
        markdown += f"\nAverage RMP of rated picks: **{average:.2f}/5** ({count} rated)\n"
        (directory / f"{key}_202620.md").write_text(markdown, encoding="utf-8")
        console.print(f"[green]wrote[/] captures/plans/{key}_202620.{{md,json}}  ({draft.total_credits:g} cr, avg RMP {average:.2f})")


def publish(plans: list[tuple[str, str, str, DraftPlan]]) -> None:
    client = PlanClient()
    existing = {plan.name: plan for plan in client.list_plans("202620")}
    for key, name, slug, draft in plans:
        if name in existing:
            console.print(f"[yellow]deleting existing plan[/] {existing[name].id} {name!r}")
            client.delete_plan(existing[name].id, "202620")
        plan = client.create_plan(name, "202620")
        for selection in draft.selections:
            client.add_course(plan.id, "202620", crn=selection.crn)
        read_back = client.get_plan(plan.id, "202620")
        written = sorted(course.crn or "" for course in read_back.courses)
        expected = sorted(selection.crn for selection in draft.selections)
        if written != expected:
            raise RuntimeError(f"{name}: Banner rows {written} != draft {expected}")
        console.print(f"[green]published[/] {name} (id {plan.id}, {len(read_back.courses)} courses verified)")


def draft_markdown(draft: DraftPlan) -> str:
    lines = [
        f"# Draft plan — {draft.label} ({draft.term})",
        "",
        f"{len(draft.selections)} sections · {draft.total_credits:g} credits",
        "",
        "| CRN | Course | Title | Instructor | RMP | Days | Time | Cr | Requirement |",
        "| --- | --- | --- | --- | --- | --- | --- | --- | --- |",
    ]
    for s in draft.selections:
        lines.append(
            f"| {s.crn} | {s.course}-{s.section} | {s.title} | {s.instructor} | {s.rating.label} | "
            f"{s.days} | {s.times} | {s.credits:g} | {s.requirement} |"
        )
    lines += ["", "```", draft.grid, "```", "", "## Rationale", ""]
    for s in draft.selections:
        lines.append(
            f"* **{s.course}-{s.section}** (CRN {s.crn}) — {s.rationale}"
            + (f"; also satisfies {', '.join(s.covers[1:])}" if len(s.covers) > 1 else "")
        )
    if draft.unplaced_requirements:
        lines += ["", "## Not placed", "", "| Requirement | Group | Reason |", "| --- | --- | --- |"]
        for item in draft.unplaced_requirements:
            lines.append(f"| {item.requirement} | {item.group} | {item.reason} |")
    lines += ["", "## Scoring", ""] + [f"* {line}" for line in draft.score_breakdown]
    return "\n".join(lines) + "\n"


def roadmap_markdown(roadmap: Roadmap) -> str:
    lines = ["# Roadmap", ""]
    for entry in roadmap.terms:
        lines += [f"## {entry.label} ({entry.term}) — {entry.status}", ""]
        if entry.draft and entry.draft.selections:
            lines += [
                "| CRN | Course | Title | Instructor | RMP | Days | Time | Cr |",
                "| --- | --- | --- | --- | --- | --- | --- | --- |",
            ]
            for s in entry.draft.selections:
                lines.append(
                    f"| {s.crn} | {s.course}-{s.section} | {s.title} | {s.instructor} | {s.rating.label} | "
                    f"{s.days} | {s.times} | {s.credits:g} |"
                )
            lines += ["", f"Credits: {entry.draft.total_credits:g}", ""]
        elif entry.status == "no_catalog":
            lines += ["_Catalog not published for this term; nothing to draft._", ""]
        for note in entry.notes:
            lines.append(f"* {note}")
        if entry.remaining:
            lines += ["", "Still open after this term:"]
            lines += [f"* {item}" for item in entry.remaining]
        lines.append("")
    lines += ["## Final remaining", ""] + [f"* {item}" for item in roadmap.final_remaining]
    if roadmap.warnings:
        lines += ["", "## Availability notes", ""] + [f"* {item}" for item in roadmap.warnings]
    return "\n".join(lines) + "\n"


@app.command()
def main(
    term: str = typer.Option("202620", help="Term to draft."),
    roadmap: bool = typer.Option(False, "--roadmap", help="Draft the whole multi-term roadmap."),
    matrix: bool = typer.Option(False, "--matrix", help="Write the offering-pattern matrix."),
    max_credits: float = typer.Option(16, help="Credit cap per term."),
    refresh_terms: bool = typer.Option(False, "--refresh-terms", help="Re-fetch Banner snapshots."),
    plans: bool = typer.Option(False, "--plans", help="Build the three advising drafts (A/B/C)."),
    publish_plans: bool = typer.Option(False, "--publish", help="Write the A/B/C drafts into Banner (plan writes)."),
) -> None:
    CAPTURES.mkdir(parents=True, exist_ok=True)
    audit = AuditClient().audit()
    constraints = Constraints(max_credits=max_credits)

    if plans or publish_plans:
        snapshots = load_snapshots([term], refresh_terms)
        planner = Planner(audit, snapshots)
        built = []
        for key, name, slug, constraints in PLAN_SPECS:
            draft = planner.draft(term, constraints)
            problems = verify_draft(draft, snapshots[term])
            if problems:
                for problem in problems:
                    console.print(f"[red]{key}: {problem}[/]")
                raise typer.Exit(code=1)
            built.append((key, name, slug, draft))
        write_plan_artifacts(built)
        for key, name, slug, draft in built:
            average, count = average_rating(draft)
            console.print(
                f"{name}: {draft.total_credits:g} cr, {len(draft.selections)} sections, "
                f"avg RMP {average:.2f} ({count} rated) -> "
                + ", ".join(f"{s.course}/{s.crn}" for s in draft.selections)
            )
        if publish_plans:
            publish(built)
        return

    if matrix:
        snapshots = load_snapshots(HISTORY_TERMS, refresh_terms)
        requirements = [r for r in requirements_from_audit(audit) if not r.satisfied]
        histories = offering_matrix(requirements, HISTORY_TERMS, snapshots)
        (CAPTURES / "offering_matrix.json").write_text(
            json.dumps([history.model_dump(mode="json") for history in histories], indent=1),
            encoding="utf-8",
        )
        markdown = (
            "# Offering pattern matrix\n\n"
            f"Terms observed: {', '.join(f'{term_label(t)} ({t})' for t in HISTORY_TERMS)}\n\n"
            + render_matrix(histories, HISTORY_TERMS)
            + "\n"
        )
        (CAPTURES / "offering_matrix.md").write_text(markdown, encoding="utf-8")
        console.print(f"[green]wrote[/] captures/offering_matrix.md ({len(histories)} targets)")
        console.print(f"index sections: {index_summary()}")

    if roadmap:
        snapshots = load_snapshots(sorted(set(ROADMAP_TERMS + [term])), refresh_terms)
        planner = Planner(audit, snapshots)
        result = planner.roadmap(ROADMAP_TERMS, constraints, include_summer=True)
        (CAPTURES / "roadmap.md").write_text(roadmap_markdown(result), encoding="utf-8")
        (CAPTURES / "roadmap.json").write_text(result.model_dump_json(indent=1), encoding="utf-8")
        for entry in result.terms:
            console.print(f"\n[bold]{entry.label}[/] ({entry.term}) — {entry.status}")
            if entry.draft and entry.draft.selections:
                console.print(draft_table(entry.draft))
            for note in entry.notes:
                console.print(f"  [yellow]·[/] {note[:160]}")
        console.print("\n[bold]Final remaining[/]")
        for item in result.final_remaining:
            console.print(f"  - {item}")
        console.print("[green]wrote[/] captures/roadmap.md")
        return

    snapshots = load_snapshots([term], refresh_terms)
    planner = Planner(audit, snapshots)
    draft = planner.draft(term, constraints)
    problems = verify_draft(draft, snapshots[term])
    if problems:
        console.print("[red]sanity check failed:[/]")
        for problem in problems:
            console.print(f"  - {problem}")
        raise typer.Exit(code=1)
    console.print(f"[green]sanity check:[/] {len(draft.selections)} CRNs exist, seats ok, no overlaps")
    console.print(draft_table(draft))
    console.print(draft.grid)
    if draft.unplaced_requirements:
        table = Table(title="Not placed")
        table.add_column("Requirement")
        table.add_column("Reason")
        for item in draft.unplaced_requirements:
            table.add_row(item.requirement, item.reason)
        console.print(table)
    for line in draft.score_breakdown:
        console.print(f"  · {line}")
    (CAPTURES / f"draft_{term}.json").write_text(draft.model_dump_json(indent=1), encoding="utf-8")
    (CAPTURES / f"draft_{term}.md").write_text(draft_markdown(draft), encoding="utf-8")
    console.print(f"[green]wrote[/] captures/draft_{term}.json and .md")


if __name__ == "__main__":
    app()
