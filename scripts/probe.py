#!/usr/bin/env python
"""Cookie-transplant smoke test: pure httpx, no browser.

    python scripts/probe.py                 # uses the cached session
    python scripts/probe.py --term 202620 --subject CYS --course 2081

Proves the persisted cookie jar is enough to call Banner:
1. ``GET  /ssb/classSearch/getTerms``              term list
2. ``POST /ssb/term/search``                       bind the term to the session
3. ``GET  /ssb/searchResults/searchResults``       class search
4. ``GET  /ssb/plan/selectPlan``                   planner list (bootstrap JSON)
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

import typer
from rich.console import Console
from rich.table import Table

from flightplan import auth
from flightplan.banner.plan import PlanClient
from flightplan.banner.ssb import SSBClient

app = typer.Typer(add_completion=False)
console = Console()


@app.command()
def main(
    term: str = typer.Option("202620", help="Term code to probe."),
    subject: str = typer.Option("CYS", help="Subject for the class-search probe."),
    course: str = typer.Option("2081", help="Course number for the class-search probe."),
) -> None:
    if not auth.session_valid():
        console.print("[yellow]cached session invalid — logging in once[/]")
        auth.login()

    client = auth.httpx_client()
    console.print(f"cookies replayed: {len(auth.load_state().get('cookies', []))}")

    ssb = SSBClient(client)
    terms = ssb.get_terms()
    term_table = Table(title="Terms (GET /ssb/classSearch/getTerms)")
    term_table.add_column("code")
    term_table.add_column("description")
    for item in terms:
        term_table.add_row(item.code, item.description)
    console.print(term_table)

    eligibility = ssb.set_term(term, mode="search")
    console.print(
        f"term {term} bound: fwdURL={eligibility.fwd_url or '-'} "
        f"studentEligValid={eligibility.student_elig_valid}"
    )

    page = ssb.search(term=term, subject=subject, course_number=course, page_max_size=10)
    results = Table(title=f"Search {subject} {course} — {page.total_count} section(s)")
    for column in ("CRN", "Course", "Title", "Seats", "Days", "Time", "Instructor"):
        results.add_column(column)
    for section in page.sections:
        days = "/".join(meeting.days[0].value if len(meeting.days) == 1 else "".join(d.value[0] for d in meeting.days) for meeting in section.meeting_times) or "TBA"
        spans = [
            f"{meeting.begin_time}-{meeting.end_time}"
            for meeting in section.meeting_times
            if meeting.begin_time and meeting.end_time
        ]
        times = ", ".join(spans) or "TBA"
        results.add_row(
            section.crn,
            section.code,
            section.title[:38],
            f"{section.seats_available}/{section.maximum_enrollment}",
            days,
            times,
            ", ".join(section.instructors) or "-",
        )
    console.print(results)

    plans = PlanClient(ssb).list_plans(term)
    console.print(f"plans in {term}: {len(plans)}")
    for plan in plans:
        console.print(
            f"  #{plan.id} seq={plan.sequence_number} {plan.name!r} "
            f"courses={[course.code for course in plan.courses]}"
        )

    console.print("[green]pure-httpx replay works[/]")


if __name__ == "__main__":
    app()
