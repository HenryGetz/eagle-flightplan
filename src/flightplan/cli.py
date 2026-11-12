"""`flightplan` command line: session, search, plans, audit and the advising brief."""

from __future__ import annotations

import json
import os
import sys

import typer
from rich.console import Console
from rich.table import Table

from . import auth
from .audit import AuditClient
from .banner.plan import PlanClient, PlanWriteBlocked
from .banner.ssb import SSBClient
from .report import build_brief

app = typer.Typer(add_completion=False, help="FlightPlan: course planning for UNW Eagles.")
console = Console()


@app.command()
def login(force: bool = typer.Option(False, "--force", help="Re-run the browser login.")) -> None:
    path = auth.login(force=force)
    console.print(f"state: {path} (valid={auth.session_valid()})")


@app.command("terms")
def terms_command() -> None:
    table = Table("code", "description")
    for term in SSBClient().get_terms():
        table.add_row(term.code, term.description)
    console.print(table)


@app.command()
def search(
    subject: str = typer.Option("", help="Subject code, e.g. CYS."),
    course: str = typer.Option("", help="Course number, e.g. 2081."),
    term: str = typer.Option("202620", help="Term code."),
    limit: int = typer.Option(20, help="Rows to print."),
) -> None:
    page = SSBClient().search(term=term, subject=subject, course_number=course, page_max_size=limit)
    table = Table("CRN", "Course", "Title", "Seats", "Days", "Time", "Instructors")
    for section in page.sections[:limit]:
        spans = [
            f"{meeting.begin_time}-{meeting.end_time}"
            for meeting in section.meeting_times
            if meeting.begin_time and meeting.end_time
        ]
        days = "".join(
            sorted({day.value[0] for meeting in section.meeting_times for day in meeting.days})
        ) or "TBA"
        table.add_row(
            section.crn,
            section.code,
            section.title[:40],
            f"{section.seats_available}/{section.maximum_enrollment}",
            days,
            ", ".join(spans) or "TBA",
            ", ".join(section.instructors) or "-",
        )
    console.print(f"{page.total_count} section(s) in {term}")
    console.print(table)


@app.command()
def plans(term: str = typer.Option("202620", help="Term code.")) -> None:
    table = Table("id", "seq", "name", "courses", "credits", "preferred")
    for plan in PlanClient().list_plans(term):
        table.add_row(
            str(plan.id),
            str(plan.sequence_number),
            plan.name,
            ", ".join(f"{course.code}/{course.crn or '-'}" for course in plan.courses) or "-",
            f"{plan.total_planned_credit_hours:g}",
            "yes" if plan.preferred else "no",
        )
    console.print(table)


@app.command()
def audit(
    unmet: bool = typer.Option(False, "--unmet", help="Only print outstanding groups."),
    json_out: bool = typer.Option(False, "--json", help="Emit the parsed audit as JSON."),
) -> None:
    parsed = AuditClient().audit()
    if json_out:
        console.print_json(parsed.model_dump_json())
        return
    console.print(
        f"{parsed.student_name} · catalog {parsed.catalog_year} · "
        f"majors {', '.join(parsed.majors)} · minor {', '.join(parsed.minors)} · "
        f"{parsed.credits_earned:g} cr earned + {parsed.credits_in_progress:g} in progress · "
        f"GPA {parsed.gpa}"
    )
    table = Table("kind", "title", "required", "earned", "remaining", "status")
    for group in parsed.groups:
        if unmet and group.status.value == "met":
            continue
        table.add_row(
            group.kind.value,
            group.title,
            f"{group.required_credits:g}" if group.required_credits is not None else "-",
            f"{group.earned_credits:g}" if group.earned_credits is not None else "-",
            f"{group.remaining_credits:g}" if group.remaining_credits is not None else "-",
            group.status.value,
        )
    console.print(table)


@app.command()
def advise(
    term: str = typer.Option("202620", help="Term code."),
    out: str | None = typer.Option(None, "--out", help="Write the brief to this file instead of stdout."),
    roadmap: bool = typer.Option(False, "--roadmap", help="Append the multi-term roadmap."),
    max_credits: float = typer.Option(16, help="Credit cap for the recommended draft."),
    json_out: bool = typer.Option(False, "--json", help="Emit structured JSON instead of markdown."),
    no_pii: bool = typer.Option(False, "--no-pii", help="Omit identity-bearing fields (brief is name-free either way)."),
    no_auto_login: bool = typer.Option(False, "--no-auto-login", help="Disable automatic re-login."),
) -> None:
    """Build the one-shot advising brief from live data."""
    if no_auto_login:
        os.environ["UNW_AUTO_LOGIN"] = "0"
    brief = build_brief(
        term,
        max_credits=max_credits,
        include_roadmap=roadmap,
        include_pii=not no_pii,
    )
    payload = json.dumps(brief.data, indent=1, sort_keys=True) if json_out else brief.markdown
    if out:
        from pathlib import Path

        Path(out).parent.mkdir(parents=True, exist_ok=True)
        Path(out).write_text(payload + ("" if payload.endswith("\n") else "\n"), encoding="utf-8")
        console.print(f"wrote {out}")
    else:
        console.print(payload, markup=False, highlight=False)


@app.command()
def windows(
    term: str | None = typer.Option(None, "--term", help="Single term code; default is every term."),
    json_out: bool = typer.Option(False, "--json", help="Emit JSON instead of a table."),
) -> None:
    """Show registration time-ticket windows (read-only)."""
    from .registration import countdown, windows as registration_windows

    items = registration_windows([term] if term else None)
    if json_out:
        console.print_json(json.dumps([item.model_dump(mode="json") for item in items]))
        return
    table = Table("term", "eligible", "opens", "closes", "countdown", "reason")
    for item in items:
        table.add_row(
            item.term,
            "yes" if item.eligible else "no",
            item.start.isoformat(timespec="minutes") if item.start else "-",
            item.end.isoformat(timespec="minutes") if item.end else "-",
            "now" if item.eligible else countdown(item.opens_in_seconds),
            item.reason[:80],
        )
    console.print(table)


@app.command()
def watch(
    term: str = typer.Option(..., "--term", help="Term code to watch."),
    interval: int = typer.Option(60, "--interval", help="Seconds between polls."),
    json_out: bool = typer.Option(False, "--json", help="Emit JSON lines on change."),
    until_open: bool = typer.Option(False, "--until-open", help="Exit non-zero when the term becomes eligible."),
) -> None:
    """Poll one term's eligibility and print a line whenever it changes (read-only)."""
    import time as _time

    from .registration import window as registration_window, countdown

    last: tuple | None = None
    try:
        while True:
            current = registration_window(term)
            signature = (current.eligible, current.start, current.end, current.reason)
            if signature != last:
                if json_out:
                    console.print_json(json.dumps(current.model_dump(mode="json")))
                else:
                    state = "ELIGIBLE" if current.eligible else "waiting"
                    when = current.start.isoformat(timespec="minutes") if current.start else "unknown"
                    console.print(
                        f"{term}: {state} · opens {when} · {countdown(current.opens_in_seconds)} · {current.reason[:100]}"
                    )
                last = signature
            if until_open and current.eligible:
                raise typer.Exit(code=10)
            _time.sleep(max(1, interval))
    except KeyboardInterrupt:
        raise typer.Exit(code=130) from None


@app.command()
def snipe(
    term: str = typer.Option(..., "--term", help="Term code."),
    plan: str | None = typer.Option(None, "--plan", help="Plan name whose CRNs are the targets."),
    crn: list[str] = typer.Option(None, "--crn", help="Explicit CRN (repeatable)."),
    mode: str = typer.Option("window", "--mode", help="window or seats."),
    offset: int = typer.Option(0, "--offset", help="Seconds after the window opens."),
    interval: int = typer.Option(60, "--interval", help="Poll interval for seats/window waits."),
    live: bool = typer.Option(False, "--live", help="Actually submit (requires --i-understand and typed term)."),
    dry_run: bool = typer.Option(True, "--dry-run/--no-dry-run", help="Rehearse without submitting."),
    i_understand: bool = typer.Option(False, "--i-understand", help="Required for --live."),
    allow_waitlist: bool = typer.Option(False, "--allow-waitlist", help="Join a waitlist if offered."),
    probe: bool = typer.Option(False, "--probe", help="Only verify the registration page and selectors."),
    max_attempts: int = typer.Option(5, "--max-attempts", help="Retries for a blank/loading stage only (not for unclear staging)."),
    timeout: int = typer.Option(3600, "--timeout", help="Hard timeout in seconds."),
) -> None:
    """Targeted registration run (dry-run by default; never drops)."""
    from .registration import window as registration_window
    from .snipe import (
        SafetyError,
        SnipeError,
        SnipeOptions,
        authorize_live,
        resolve_targets,
        run_browser,
    )

    targets = resolve_targets(term, plan_name=plan, crns=crn or [])
    options = SnipeOptions(
        term=term,
        plan_name=plan,
        crns=[target.crn for target in targets],
        mode=mode if mode in {"window", "seats"} else "window",
        offset_seconds=offset,
        interval_seconds=interval,
        dry_run=not live,
        live=live,
        i_understand=i_understand,
        allow_waitlist=allow_waitlist,
        probe=probe,
        max_attempts=max_attempts,
        timeout_seconds=timeout,
    )
    table = Table("order", "CRN", "course", "priority", "credits")
    for index, target in enumerate(targets, start=1):
        table.add_row(str(index), target.crn, target.course, target.priority, f"{target.credits:g}" if target.credits else "-")
    console.print(table)
    current = registration_window(term)
    console.print(
        f"Term {term}: {'ELIGIBLE' if current.eligible else 'not eligible'} · {current.reason[:120]}"
    )
    console.print(f"Mode: {options.mode} · {'PROBE' if probe else ('LIVE' if live else 'DRY RUN')}")
    if live and not probe:
        try:
            authorize_live(term, i_understand=i_understand)
        except SafetyError as exc:
            console.print(f"[red]{exc}[/]")
            raise typer.Exit(code=2) from exc
    try:
        result = run_browser(options, targets)
    except (SnipeError, SafetyError) as exc:
        console.print(f"[red]{exc}[/]")
        raise typer.Exit(code=1) from exc
    console.print(f"status: {result.status}")
    if result.reason:
        console.print(result.reason)
    if result.capture_path:
        console.print(f"captured: {result.capture_path}")
    if result.status in {
        "not_eligible",
        "prereq",
        "conflict",
        "hold",
        "session_dead",
        "timeout",
        "stage_unknown",
        "staged_not_committed",
        "save_unknown",
        "authorization_required",
    }:
        raise typer.Exit(code=1)


@app.command()
def setup(
    yes: bool = typer.Option(False, "--yes", help="Non-interactive: accept defaults."),
    user: str | None = typer.Option(None, "--user", help="UNW email address."),
    totp_secret: str | None = typer.Option(None, "--totp-secret", help="Base32 TOTP secret."),
    password_stdin: bool = typer.Option(False, "--password-stdin", help="Read the password from stdin."),
    no_login: bool = typer.Option(False, "--no-login", help="Do not start the browser login."),
    register_mcp: bool = typer.Option(False, "--register-mcp", help="Register the MCP server if available."),
    force: bool = typer.Option(False, "--force", help="Replace an existing .env."),
    root: str | None = typer.Option(None, "--root", help="Project folder (advanced)."),
) -> None:
    """First-run wizard: install, credentials, doctor checks and login."""
    from pathlib import Path

    from .config import REPO_ROOT
    from .setup_cmd import SetupError, SetupOptions, run_setup

    options = SetupOptions(
        root=Path(root).resolve() if root else REPO_ROOT,
        user=user,
        totp_secret=totp_secret,
        password_stdin=password_stdin,
        yes=yes,
        force=force,
        no_login=no_login,
        register_mcp=register_mcp,
    )
    try:
        code = run_setup(options)
    except SetupError as exc:
        console.print(f"[red]{exc}[/]")
        raise typer.Exit(code=1) from exc
    raise typer.Exit(code=code)


# --------------------------------------------------------------------------- #
# plan writes: explicit opt-in only
# --------------------------------------------------------------------------- #
def _passed_codes() -> set[str] | None:
    try:
        from .audit import AuditClient
        from .catalog import applied_codes

        return applied_codes(AuditClient().audit())
    except Exception:  # noqa: BLE001 - validation degrades to "prereq unchecked"
        return None


def _require_yes(description: str, yes: bool) -> None:
    console.print(f"[bold]About to:[/] {description}")
    if yes:
        return
    if sys.stdin.isatty():
        if typer.confirm("Proceed?", default=False):
            return
    raise typer.Exit("refusing to write without --yes")


@app.command("plan-create")
def plan_create(
    name: str = typer.Argument(..., help="Plan name."),
    term: str = typer.Option("202620", help="Term code."),
    yes: bool = typer.Option(False, "--yes", help="Skip the interactive confirmation."),
) -> None:
    _require_yes(f"create plan {name!r} in {term}", yes)
    plan = PlanClient().create_plan(name, term)
    console.print(f"created plan {plan.id} {plan.name!r} ({len(plan.courses)} rows)")


@app.command("plan-add")
def plan_add(
    plan_id: int = typer.Argument(..., help="Plan id (from `flightplan plans`)."),
    term: str = typer.Option("202620", help="Term code."),
    crn: list[str] = typer.Option(..., "--crn", help="CRN to add (repeatable)."),
    yes: bool = typer.Option(False, "--yes", help="Skip the interactive confirmation."),
    force: bool = typer.Option(False, "--force", help="Override hard validation blockers."),
) -> None:
    _require_yes(f"add CRNs {', '.join(crn)} to plan {plan_id} in {term}", yes)
    client = PlanClient()
    passed = _passed_codes()
    for item in crn:
        try:
            course = client.add_course(
                plan_id, term, crn=item, force=force, passed_codes=passed
            )
        except PlanWriteBlocked as exc:
            console.print("[red]blocked:[/]")
            for issue in exc.issues:
                console.print(f"  - {issue.message}")
            raise typer.Exit(code=2) from exc
        console.print(f"added {item} -> {course.code} ({course.credits:g} cr)")
    for issue in client.validate(plan_id, term, passed_codes=passed):
        console.print(f"[yellow]warning:[/] {issue.message}")


@app.command("plan-delete")
def plan_delete(
    plan_id: int = typer.Argument(..., help="Plan id."),
    term: str = typer.Option("202620", help="Term code."),
    yes: bool = typer.Option(False, "--yes", help="Skip the interactive confirmation."),
    force: bool = typer.Option(False, "--force", help="Skip the typed-name confirmation."),
) -> None:
    client = PlanClient()
    plan = client.get_plan(plan_id, term)
    _require_yes(f"DELETE plan {plan.name!r} (id {plan.id}, {len(plan.courses)} rows)", yes)
    if not force:
        if sys.stdin.isatty():
            typed = typer.prompt(f"Type the plan name to confirm deletion: {plan.name!r}")
            if typed != plan.name:
                raise typer.Exit("name did not match; nothing deleted")
        else:
            raise typer.Exit("destructive delete requires --force when stdin is not a TTY")
    client.delete_plan(plan_id, term)
    console.print(f"deleted plan {plan_id}")


@app.command("plans-json")
def plans_json(term: str = typer.Option("202620", help="Term code.")) -> None:
    """Machine-readable plan dump (used by the future MCP tools)."""
    payload = [plan.model_dump() for plan in PlanClient().list_plans(term)]
    console.print_json(json.dumps(payload))


if __name__ == "__main__":
    app()
