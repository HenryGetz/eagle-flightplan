"""`unwplanner` command line: session, audit, search, planning and Banner plans.

    unwplanner doctor
    unwplanner login --force
    unwplanner profile --json
    unwplanner remaining
    unwplanner search --term 202620 --subject CYS --json
    unwplanner candidates --term 202620 --requirement Python
    unwplanner rate "Zderad, Jonathan"
    unwplanner conflicts --term 202620 22201 23189
    unwplanner draft --term 202620 --max-credits 16
    unwplanner roadmap --start-term 202620 --include-summer
    unwplanner offering-matrix "CYS 4245" "ATTRIBUTE NATS"
    unwplanner plan list --term 202620
    unwplanner mcp

Read commands accept ``--json`` for machine output. The MCP server exposes the
same payloads; both paths call the functions in :mod:`unw_planner.mcp_server`.
"""

from __future__ import annotations

import json
import logging
import stat
import tempfile
import time
from pathlib import Path
from typing import Any

import typer
from rich.console import Console
from rich.table import Table

from . import auth
from .audit import AuditClient
from .banner.plan import PlanClient
from .config import ENV_PATH, STATE_PATH, TERMS_DIR, get_settings
from .mcp_server import (
    FIX_COMMAND,
    audit_payload,
    candidates_payload,
    conflicts_payload,
    draft_payload,
    offering_matrix_payload,
    plan_add_payload,
    plan_create_payload,
    plan_delete_payload,
    plan_remove_payload,
    plans_payload,
    profile_payload,
    ratings_payload,
    remaining_payload,
    roadmap_payload,
    search_payload,
    terms_payload,
)

logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("httpcore").setLevel(logging.WARNING)

app = typer.Typer(add_completion=False, help="UNW Banner 9 planner client.")
plan_app = typer.Typer(add_completion=False, help="Banner Plan Ahead worksheets (term-scoped).")
app.add_typer(plan_app, name="plan")
console = Console()


# --------------------------------------------------------------------------- #
# output helpers
# --------------------------------------------------------------------------- #
def _emit(payload: dict, as_json: bool) -> None:
    """Print a payload and exit non-zero when ``ok`` is false."""
    if as_json:
        typer.echo(json.dumps(payload, indent=2, default=str))
        if not payload.get("ok", True):
            raise typer.Exit(1)
        return
    if not payload.get("ok", True):
        console.print(f"[red]{payload.get('error', 'command failed')}[/red]")
        if payload.get("hint"):
            console.print(f"[yellow]{payload['hint']}[/yellow]")
        raise typer.Exit(1)


def _short(text: str | None, width: int) -> str:
    value = (text or "").strip()
    return value if len(value) <= width else value[: width - 1] + "…"


def _rmp(rating: dict | None) -> str:
    if not rating or not rating.get("rated"):
        return "unrated"
    return f"{rating['rating']:.1f}/5 (n={rating['num_ratings']})"


# --------------------------------------------------------------------------- #
# existing commands
# --------------------------------------------------------------------------- #
@app.command()
def login(force: bool = typer.Option(False, "--force", help="Re-run the browser login.")) -> None:
    """Run the Entra ID + TOTP login and save the replayable session state."""
    path = auth.login(force=force)
    console.print(f"state: {path} (valid={auth.session_valid()})")


@app.command("terms")
def terms_command(json_out: bool = typer.Option(False, "--json", help="Emit JSON.")) -> None:
    """Banner term codes, descriptions and registration eligibility."""
    payload = terms_payload()
    if json_out:
        _emit(payload, True)
        return
    table = Table("code", "description", "next", "current", "eligible", "failures")
    for row in payload["terms"]:
        table.add_row(
            row["code"],
            row["description"],
            "yes" if row["is_next"] else "",
            "yes" if row["is_current"] else "",
            {True: "yes", False: "no", None: "-"}[row["registration_eligible"]],
            "; ".join(row["eligibility_failures"])[:60],
        )
    console.print(table)


@app.command()
def search(
    subject: str = typer.Option("", help="Subject code, e.g. CYS."),
    course: str = typer.Option("", help="Course number, e.g. 2081."),
    term: str = typer.Option("202620", help="Term code."),
    limit: int = typer.Option(20, help="Rows to print."),
    keyword: str = typer.Option("", help="Match title/code/instructor substring."),
    attribute: str = typer.Option("", help="Attribute code, e.g. CCUL."),
    instructor: str = typer.Option("", help="Instructor substring."),
    open_only: bool = typer.Option(False, "--open-only", help="Only sections with seats."),
    refresh: bool = typer.Option(False, "--refresh", help="Refetch the term snapshot."),
    json_out: bool = typer.Option(False, "--json", help="Emit JSON."),
) -> None:
    """Search one term's sections (filters are applied client-side)."""
    payload = search_payload(
        term,
        subject=subject,
        number=course,
        keyword=keyword,
        attribute=attribute,
        instructor=instructor,
        open_only=open_only,
        limit=limit,
        offset=0,
        refresh=refresh,
    )
    if json_out:
        _emit(payload, True)
        return
    _emit(payload, False)
    table = Table("CRN", "Course", "Title", "Seats", "Days", "Time", "Instructors", "Avail")
    for section in payload["sections"]:
        table.add_row(
            section["crn"],
            section["code"],
            _short(section["title"], 38),
            f"{section['seats_available']}/{section['capacity']}",
            section["days"],
            section["times"],
            _short(", ".join(section["instructors"]), 26),
            section["availability"],
        )
    console.print(f"{payload['total_matched']} section(s) in {term}")
    console.print(table)


@app.command()
def plans(
    term: str = typer.Option("202620", help="Term code."),
    json_out: bool = typer.Option(False, "--json", help="Emit JSON."),
) -> None:
    """List Banner Plan Ahead worksheets for one term (term-scoped)."""
    payload = plans_payload(term)
    if json_out:
        _emit(payload, True)
        return
    _emit(payload, False)
    table = Table("id", "seq", "name", "courses", "preferred")
    for plan in payload["plans"]:
        table.add_row(
            str(plan["id"]),
            str(plan["sequence_number"]),
            plan["name"],
            ", ".join(f"{course['code']}/{course['crn'] or '-'}" for course in plan["courses"])
            or "-",
            "yes" if plan["preferred"] else "no",
        )
    console.print(table)
    console.print(f"[dim]{payload['note']}[/dim]")


@app.command()
def audit(
    unmet: bool = typer.Option(False, "--unmet", help="Only print outstanding groups."),
    group: str = typer.Option("", help="Filter by group substring."),
    json_out: bool = typer.Option(False, "--json", help="Emit the parsed audit as JSON."),
) -> None:
    """Degree audit requirement tree."""
    if json_out:
        _emit(audit_payload(group or None), True)
        return
    parsed = AuditClient().audit()
    console.print(
        f"{parsed.student_name} · catalog {parsed.catalog_year} · "
        f"majors {', '.join(parsed.majors)} · minors {', '.join(parsed.minors)} · "
        f"{parsed.credits_earned or 0:g} cr earned + "
        f"{parsed.credits_in_progress or 0:g} in progress · GPA {parsed.gpa}"
    )
    needle = group.strip().lower()
    table = Table("kind", "title", "required", "earned", "remaining", "status")
    for item in parsed.groups:
        if unmet and item.status.value == "met":
            continue
        if needle and needle not in item.title.lower() and needle not in item.kind.value:
            continue
        table.add_row(
            item.kind.value,
            item.title,
            f"{item.required_credits:g}" if item.required_credits is not None else "-",
            f"{item.earned_credits:g}" if item.earned_credits is not None else "-",
            f"{item.remaining_credits:g}" if item.remaining_credits is not None else "-",
            item.status.value,
        )
    console.print(table)


@app.command("plans-json")
def plans_json(term: str = typer.Option("202620", help="Term code.")) -> None:
    """Machine-readable plan dump (kept for compatibility; prefer `plan list --json`)."""
    payload = [plan.model_dump(mode="json") for plan in PlanClient().list_plans(term)]
    typer.echo(json.dumps(payload, indent=2))


# --------------------------------------------------------------------------- #
# new read commands
# --------------------------------------------------------------------------- #
@app.command()
def profile(json_out: bool = typer.Option(False, "--json", help="Emit JSON.")) -> None:
    """Student profile from the degree audit."""
    payload = profile_payload()
    if json_out:
        _emit(payload, True)
        return
    _emit(payload, False)
    table = Table("field", "value", show_header=False)
    rows: list[tuple[str, Any]] = [
        ("name", payload["name"]),
        ("id", payload["id"]),
        ("classification", payload["classification"]),
        ("degree / school", f"{payload['degree']} / {payload['school']}"),
        ("catalog year", payload["catalog_year"]),
        ("catalog years (programs)", ", ".join(payload["catalog_years"])),
        ("majors", ", ".join(payload["majors"]) or "-"),
        ("minors", ", ".join(payload["minors"]) or "-"),
        ("programs", ", ".join(payload["programs"]) or "-"),
        ("college", ", ".join(payload["college"]) or "-"),
        (
            "advisors",
            "; ".join(
                f"{advisor['name']} <{advisor['email']}>" if advisor["email"] else advisor["name"]
                for advisor in payload["advisors"]
            )
            or "-",
        ),
        ("credits earned", f"{payload['credits_earned']:g}"),
        ("credits in progress", f"{payload['credits_in_progress'] or 0:g}"),
        ("GPA", f"{payload['gpa']:.2f}" if payload["gpa"] is not None else "-"),
        ("percent complete", f"{payload['percent_complete']}%"),
        ("anticipated graduation", payload["anticipated_graduation"]),
        ("academic standing", payload["academic_standing"]),
        ("holds", "unknown (not exposed by the mapped APIs)"),
    ]
    for key, value in rows:
        table.add_row(key, str(value))
    console.print(table)


@app.command()
def remaining(json_out: bool = typer.Option(False, "--json", help="Emit JSON.")) -> None:
    """Unmet requirements with credits, options and priority."""
    payload = remaining_payload()
    if json_out:
        _emit(payload, True)
        return
    _emit(payload, False)
    table = Table("priority", "group", "requirement", "credits", "options", "status")
    for row in payload["requirements"]:
        credits = (
            f"{row['credits_remaining']:g}"
            if row["credits_remaining"] is not None
            else (f"{row['remaining_picks']} pick" if row["remaining_picks"] else "-")
        )
        table.add_row(
            row["priority"],
            _short(row["group"], 34),
            _short(row["title"], 38),
            credits,
            _short(", ".join(row["options"]), 44),
            row["status"],
        )
    console.print(f"{payload['count']} unmet requirement(s) for {payload['student_name']}")
    console.print(table)


@app.command()
def candidates(
    term: str = typer.Option("202620", help="Term code."),
    requirement: str = typer.Option("", help="Requirement id/title substring."),
    open_only: bool = typer.Option(True, "--open-only/--include-closed"),
    refresh: bool = typer.Option(False, "--refresh", help="Refetch the term snapshot."),
    json_out: bool = typer.Option(False, "--json", help="Emit JSON."),
) -> None:
    """Resolve unmet requirements to sections offered in a term."""
    payload = candidates_payload(term, requirement or None, refresh, open_only)
    if json_out:
        _emit(payload, True)
        return
    _emit(payload, False)
    for row in payload["requirements"]:
        item = row["requirement"]
        console.print(
            f"\n[bold]{item['title']}[/bold] · {item['group']} · {item['priority']}"
        )
        if not row["found"]:
            console.print(f"  [yellow]{row['reason']}[/yellow]")
            continue
        table = Table("CRN", "Course", "Title", "Instructor", "Days", "Time", "Cr", "Seats", "Avail")
        for section in row["candidates"]:
            table.add_row(
                section["crn"],
                f"{section['code']}-{section['section']}",
                _short(section["title"], 30),
                _short(", ".join(section["instructors"]), 20),
                section["days"],
                section["times"],
                f"{section['credits']:g}" if section["credits"] is not None else "-",
                str(section["seats_available"]),
                section["availability"],
            )
        console.print(table)


@app.command()
def rate(
    names: list[str] = typer.Argument(..., help='"Last, First" or "First Last".'),
    json_out: bool = typer.Option(False, "--json", help="Emit JSON."),
) -> None:
    """Rate My Professors lookup (unrated is null, never 0.0)."""
    payload = ratings_payload(names)
    if json_out:
        _emit(payload, True)
        return
    _emit(payload, False)
    table = Table("query", "matched", "rating", "difficulty", "n", "confidence", "note")
    for row in payload["ratings"]:
        table.add_row(
            row["query"],
            row["matched_name"] or "-",
            f"{row['rating']:.1f}" if row["rated"] else "unrated",
            f"{row['difficulty']:.1f}" if row["difficulty"] is not None else "-",
            str(row["num_ratings"]),
            f"{row['confidence']:.2f}",
            _short(row["note"], 40),
        )
    console.print(table)


@app.command()
def conflicts(
    crns: list[str] = typer.Argument(..., help="CRNs in the term."),
    term: str = typer.Option("202620", help="Term code."),
    refresh: bool = typer.Option(False, "--refresh", help="Refetch the term snapshot."),
    json_out: bool = typer.Option(False, "--json", help="Emit JSON."),
) -> None:
    """Pairwise conflicts, ASCII grid and unscheduled sections."""
    payload = conflicts_payload(term, crns, refresh)
    if json_out:
        _emit(payload, True)
        return
    _emit(payload, False)
    console.print(payload["conflict_summary"])
    if payload["missing_crns"]:
        console.print(f"[yellow]missing CRNs: {', '.join(payload['missing_crns'])}[/yellow]")
    console.print(f"total credits: {payload['total_credits']:g}")
    console.print(payload["grid"])


@app.command()
def draft(
    term: str = typer.Option("202620", help="Term code."),
    max_credits: float = typer.Option(16, "--max-credits", help="Credit budget."),
    avoid_day: list[str] | None = typer.Option(None, "--avoid-day", help="Repeatable."),
    earliest: str = typer.Option("", help="Earliest start HH:MM."),
    latest: str = typer.Option("", help="Latest end HH:MM."),
    include_summer: bool = typer.Option(False, "--include-summer"),
    refresh: bool = typer.Option(False, "--refresh", help="Refetch term snapshots."),
    json_out: bool = typer.Option(False, "--json", help="Emit JSON."),
) -> None:
    """Draft a conflict-free plan from the audit."""
    payload = draft_payload(
        term,
        max_credits,
        "max_rated",
        avoid_day,
        earliest or None,
        latest or None,
        include_summer,
        refresh,
    )
    if json_out:
        _emit(payload, True)
        return
    _emit(payload, False)
    _render_draft(payload["draft"], payload["catalog"])
    if payload.get("summer"):
        console.print("\n[bold]Summer option[/bold]")
        _render_draft(payload["summer"]["draft"], payload["summer"]["catalog"])


def _render_draft(plan: dict, catalog: dict) -> None:
    console.print(
        f"[bold]{plan['label']}[/bold] ({plan['term']}) · "
        f"{plan['total_credits']:g} credits · avg RMP "
        f"{plan['avg_rmp_rating'] if plan['avg_rmp_rating'] is not None else 'n/a'} · "
        f"catalog {catalog['source']} ({catalog['age_human']} old)"
    )
    table = Table("CRN", "Course", "Title", "Instructor", "RMP", "Days", "Time", "Cr", "Avail")
    for row in plan["selections"]:
        table.add_row(
            row["crn"],
            f"{row['course']}-{row['section']}",
            _short(row["title"], 30),
            _short(row["instructor"], 20),
            _rmp(row["rating"]),
            row["days"],
            row["times"],
            f"{row['credits']:g}" if row["credits"] is not None else "-",
            row["availability"],
        )
    console.print(table)
    for row in plan["selections"]:
        console.print(f"  · {row['course']}: {row['rationale']}")
    if plan["unplaced"]:
        console.print("[yellow]unplaced:[/yellow]")
        for row in plan["unplaced"]:
            console.print(f"  · {row['requirement']} — {row['reason']}")
    for line in plan["score_breakdown"]:
        console.print(f"[dim]{line}[/dim]")
    if plan["grid"]:
        console.print(plan["grid"])


@app.command()
def roadmap(
    start_term: str = typer.Option("202620", "--start-term", help="First term to plan."),
    term: list[str] | None = typer.Option(None, "--term", help="Repeatable explicit term."),
    include_summer: bool = typer.Option(False, "--include-summer"),
    max_credits: float = typer.Option(16, "--max-credits"),
    refresh: bool = typer.Option(False, "--refresh", help="Refetch term snapshots."),
    json_out: bool = typer.Option(False, "--json", help="Emit JSON."),
) -> None:
    """Multi-term plan with graduation-risk notes."""
    payload = roadmap_payload(start_term, term, include_summer, max_credits, refresh)
    if json_out:
        _emit(payload, True)
        return
    _emit(payload, False)
    console.print(
        f"[bold]Roadmap from {payload['start_term']}[/bold] · anticipated graduation "
        f"{payload['anticipated_graduation']} · terms {', '.join(payload['terms'])}"
    )
    for row in payload["planned"]:
        plan = row["draft"]
        if plan is None:
            console.print(f"\n[bold]{row['label']}[/bold] ({row['term']}) — no catalog")
            continue
        console.print(
            f"\n[bold]{row['label']}[/bold] ({row['term']}) · {plan['total_credits']:g} credits"
        )
        table = Table("CRN", "Course", "Title", "Instructor", "Cr", "Avail")
        for item in plan["selections"]:
            table.add_row(
                item["crn"],
                item["course"],
                _short(item["title"], 34),
                _short(item["instructor"], 22),
                f"{item['credits']:g}" if item["credits"] is not None else "-",
                item["availability"],
            )
        console.print(table)
    if payload["final_remaining"]:
        console.print("\n[yellow]still remaining:[/yellow]")
        for item in payload["final_remaining"]:
            console.print(f"  · {item}")
    if payload["risks"]:
        console.print("\n[yellow]risks:[/yellow]")
        for item in payload["risks"]:
            console.print(f"  · {item}")


@app.command("offering-matrix")
def offering_matrix(
    courses: list[str] = typer.Argument(
        None, help='Course code, "ATTRIBUTE NATS" or "DAL *"; default: all unmet targets.'
    ),
    refresh: bool = typer.Option(False, "--refresh", help="Refetch every observed term."),
    json_out: bool = typer.Option(False, "--json", help="Emit JSON."),
) -> None:
    """Offering pattern per course/attribute across observed terms."""
    payload = offering_matrix_payload(courses, refresh)
    if json_out:
        _emit(payload, True)
        return
    _emit(payload, False)
    observed = payload["observed_terms"]
    table = Table("target", *payload["observed_labels"], "pattern")
    for row in payload["courses"]:
        by_term = {entry["term"]: entry for entry in row["terms"]}
        cells = []
        for term in observed:
            entry = by_term.get(term)
            cells.append(f"Y{entry['sections']}" if entry and entry["offered"] else "N")
        table.add_row(row["key"], *cells, row["pattern"])
    console.print(table)
    for row in payload["courses"]:
        console.print(f"  · {row['key']}: {row['summary']}")


# --------------------------------------------------------------------------- #
# Banner plans
# --------------------------------------------------------------------------- #
@plan_app.command("list")
def plan_list(
    term: str = typer.Option("202620", help="Term code."),
    json_out: bool = typer.Option(False, "--json", help="Emit JSON."),
) -> None:
    """List plans for one term (term-scoped)."""
    plans(term=term, json_out=json_out)


@plan_app.command("create")
def plan_create(
    term: str = typer.Option("202620", help="Term code."),
    name: str = typer.Option(..., "--name", help="Plan name (<=80 chars)."),
    json_out: bool = typer.Option(False, "--json", help="Emit JSON."),
) -> None:
    """Create a named plan (writes to Banner)."""
    payload = plan_create_payload(term, name)
    if json_out:
        _emit(payload, True)
        return
    _emit(payload, False)
    console.print(f"created plan {payload['created']['id']} {payload['created']['name']!r}")


@plan_app.command("add")
def plan_add(
    term: str = typer.Option("202620", help="Term code."),
    plan: str = typer.Option(..., "--plan", help="Plan id or exact name."),
    crn: str = typer.Option("", help="Section CRN."),
    subject: str = typer.Option("", help="Course subject (with --number)."),
    number: str = typer.Option("", help="Course number (with --subject)."),
    json_out: bool = typer.Option(False, "--json", help="Emit JSON."),
) -> None:
    """Add a course/section to a plan (writes to Banner)."""
    payload = plan_add_payload(
        term, plan, crn or None, subject or None, number or None
    )
    if json_out:
        _emit(payload, True)
        return
    _emit(payload, False)
    added = payload["added"]
    console.print(f"added {added['code']} ({added['crn'] or 'no CRN'}) to plan {payload['plan']['id']}")


@plan_app.command("remove")
def plan_remove(
    term: str = typer.Option("202620", help="Term code."),
    plan: str = typer.Option(..., "--plan", help="Plan id or exact name."),
    crn: str = typer.Option("", help="Section CRN."),
    subject: str = typer.Option("", help="Course subject (with --number)."),
    number: str = typer.Option("", help="Course number (with --subject)."),
    json_out: bool = typer.Option(False, "--json", help="Emit JSON."),
) -> None:
    """Remove one course row from a plan (writes to Banner)."""
    payload = plan_remove_payload(
        term, plan, crn or None, subject or None, number or None
    )
    if json_out:
        _emit(payload, True)
        return
    _emit(payload, False)
    console.print(f"plan {payload['plan']['id']} now has {len(payload['plan']['courses'])} course(s)")


@plan_app.command("delete")
def plan_delete(
    term: str = typer.Option("202620", help="Term code."),
    plan: str = typer.Option(..., "--plan", help="Plan id or exact name."),
    json_out: bool = typer.Option(False, "--json", help="Emit JSON."),
) -> None:
    """Delete an entire plan (writes to Banner, irreversible)."""
    payload = plan_delete_payload(term, plan)
    if json_out:
        _emit(payload, True)
        return
    _emit(payload, False)
    console.print(
        f"deleted plan {payload['deleted']['id']} {payload['deleted']['name']!r}; "
        f"{payload['remaining_plans']} plan(s) remain for {term}"
    )


# --------------------------------------------------------------------------- #
# mcp + doctor
# --------------------------------------------------------------------------- #
@app.command("mcp")
def mcp_command() -> None:
    """Run the stdio MCP server (same as the `unw-mcp` console script)."""
    from .mcp_server import main as run_mcp

    run_mcp()


def _check(name: str, fn) -> dict:
    try:
        ok, detail, fix = fn()
    except Exception as exc:  # noqa: BLE001 - doctor reports, never crashes
        ok, detail, fix = False, f"{type(exc).__name__}: {exc}", ""
    return {"name": name, "ok": bool(ok), "detail": detail, "fix": "" if ok else fix}


def _check_env() -> tuple[bool, str, str]:
    cfg = get_settings()
    missing = [
        key
        for key, value in (
            ("UNW_USER", cfg.user),
            ("UNW_PASS", cfg.password),
            ("UNW_TOTP_SECRET", cfg.totp_secret),
        )
        if not value
    ]
    exists = ENV_PATH.exists()
    mode = stat.S_IMODE(ENV_PATH.stat().st_mode) if exists else 0
    detail = f"{ENV_PATH} {'present' if exists else 'MISSING'}"
    if exists:
        detail += f", mode {mode:o}, credentials {3 - len(missing)}/3"
    if missing:
        detail += f", missing {', '.join(missing)}"
    ok = exists and not missing and mode & 0o077 == 0
    return ok, detail, "cp .env.example .env && chmod 600 .env, then fill the three values"


def _check_state() -> tuple[bool, str, str]:
    if not STATE_PATH.exists():
        return False, f"{STATE_PATH} missing", FIX_COMMAND
    age = auth.state_age_seconds()
    return True, f"{STATE_PATH} age {age / 60:.0f} min", FIX_COMMAND


def _check_session() -> tuple[bool, str, str]:
    valid = auth.session_valid()
    return (
        valid,
        "Banner reports an authenticated fullName" if valid else "Banner rendered as guest/login",
        FIX_COMMAND,
    )


def _check_chromium() -> tuple[bool, str, str]:
    from playwright.sync_api import sync_playwright

    with sync_playwright() as playwright:
        executable = playwright.chromium.executable_path.strip()
    path = Path(executable)
    return (
        path.exists(),
        f"chromium {path}" if path.exists() else f"not installed ({executable})",
        f"{Path(sys_executable()).parent}/playwright install chromium",
    )


def _check_term_cache() -> tuple[bool, str, str]:
    files = sorted(TERMS_DIR.glob("*.json")) if TERMS_DIR.exists() else []
    if not files:
        return False, "no term snapshots", "unwplanner search --term 202620 --subject CYS"
    newest = max(files, key=lambda path: path.stat().st_mtime)
    age = time.time() - newest.stat().st_mtime
    return (
        True,
        f"{len(files)} snapshot(s); newest {newest.stem} at {age / 60:.0f} min",
        "unwplanner search --term <term>",
    )


def _check_rmp() -> tuple[bool, str, str]:
    from .rmp.cache import SqliteCache
    from .rmp.client import RMPClient

    with tempfile.TemporaryDirectory() as tmp:
        cache = SqliteCache(Path(tmp) / "rmp.sqlite")
        client = RMPClient(cache=cache, timeout=12.0, max_retries=1)
        try:
            school = client.get_school(706)
        finally:
            client.close()
    return (
        True,
        f"{school.name} ({school.num_ratings} ratings)",
        "RMP may be blocked/rate-limited; ratings are optional (null = unknown)",
    )


def sys_executable() -> str:
    import sys

    return sys.executable


@app.command()
def doctor(json_out: bool = typer.Option(False, "--json", help="Emit JSON.")) -> None:
    """Check .env, session, Chromium, term cache and RMP with fixes."""
    checks = [
        _check("env", _check_env),
        _check("state", _check_state),
        _check("session", _check_session),
        _check("chromium", _check_chromium),
        _check("term-cache", _check_term_cache),
        _check("rmp", _check_rmp),
    ]
    ok = all(check["ok"] for check in checks)
    payload = {"ok": ok, "checks": checks}
    if json_out:
        typer.echo(json.dumps(payload, indent=2))
        if not ok:
            raise typer.Exit(1)
        return
    for check in checks:
        mark = "[green]✓[/green]" if check["ok"] else "[red]✗[/red]"
        console.print(f"{mark} {check['name']}: {check['detail']}")
        if not check["ok"] and check["fix"]:
            console.print(f"    fix: {check['fix']}")
    if not ok:
        raise typer.Exit(1)


if __name__ == "__main__":
    app()
