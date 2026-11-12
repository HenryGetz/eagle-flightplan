"""Registration sniping: target resolution, safety gates and a browser-driven run.

The register/add endpoints have never been observed for this account (no term has
been registration-eligible), so execution drives the real UI instead of guessing
HTTP. Every run can capture its own traffic so a later pass can promote the flow
to direct API calls.

Banner's UI is two-phase: the Add/CRN control stages a pending row and
``#saveButton[data-registration-endpoint]`` commits it. This module never treats
staging as registration. The browser adds ``X-Synchronizer-Token`` from the page
meta automatically; any future direct HTTP write path must fetch that token first
(see ``docs/banner-api.md``), and no direct write endpoint is guessed here.
"""

from __future__ import annotations

import json
import sys
import time
from datetime import datetime, timedelta
from pathlib import Path
from typing import Callable, Iterable, Literal

from pydantic import BaseModel, Field

from .audit import AuditClient, Audit
from .banner.plan import PlanClient
from .banner.search import load_snapshot
from .catalog import requirements_from_audit
from .config import CAPTURES_DIR
from .registration import RegistrationWindow, countdown, window

CAPTURES = CAPTURES_DIR
CRN_INPUT_SELECTORS = (
    "#txt_crn",
    "input[name='crn']",
    "input[id*='crn' i]",
    "input[placeholder*='CRN' i]",
    "input[aria-label*='CRN' i]",
)
ADD_BUTTON_SELECTORS = (
    "button:has-text('Add')",
    "#addButton",
    "button[id*='add' i]",
    "input[type='submit'][value*='Add' i]",
)
SAVE_BUTTON_SELECTORS = (
    "#saveButton",
    "button:has-text('Save')",
    "input[value*='Save' i]",
    "button[id*='save' i]",
)
WAITLIST_SELECTORS = (
    "button:has-text('Waitlist')",
    "button:has-text('Wait List')",
    "a:has-text('Waitlist')",
)
#: The real UI opens these after staging a CRN that needs add authorization or
#: an OLR decision. They are never auto-accepted: the run stops for a human.
AUTHORIZATION_SELECTORS = (
    ".addAuthorizationPopup",
    ".olrPopup",
)

#: A stage response is not a registration. These phrases mean the CRN was added
#: to the pending schedule and the single Save click is still required.
STAGING_SUCCESS_PATTERNS = (
    "course added",
    "added to schedule",
    "added to your schedule",
    "successfully added",
    "added to the schedule",
    "staged",
)

#: A blank/loading stage is genuinely ambiguous; retry it up to max_attempts.
#: Any other unclear stage is a hard stop (`stage_unknown`), never a Save.
STAGING_AMBIGUOUS_PATTERNS = (
    "loading",
    "processing",
    "please wait",
    "working",
)

OUTCOME_PATTERNS: tuple[tuple[str, tuple[str, ...]], ...] = (
    # "already registered" must win over the generic success phrase.
    ("already_registered", ("already registered", "duplicate registration", "already enrolled")),
    ("registered", ("registration successful", "registered for", "successfully registered", "web registered")),
    ("closed", ("closed", "full", "no seats", "section is full")),
    ("waitlist", ("waitlist", "wait list")),
    ("prereq", ("prerequisite", "prereq", "test score", "placement")),
    ("conflict", ("time conflict", "conflicts with", "overlap")),
    ("hold", ("hold", "advisor approval", "authorization required", "permit")),
    ("session_dead", ("sign in", "login.microsoftonline.com", "session expired")),
)


class SnipeError(RuntimeError):
    pass


class SafetyError(SnipeError):
    pass


class Target(BaseModel):
    crn: str
    course: str = ""
    title: str = ""
    priority: str = "unknown"
    credits: float | None = None


class SnipeOptions(BaseModel):
    term: str
    plan_name: str | None = None
    crns: list[str] = Field(default_factory=list)
    mode: Literal["window", "seats"] = "window"
    offset_seconds: int = 0
    interval_seconds: int = 60
    dry_run: bool = True
    live: bool = False
    i_understand: bool = False
    allow_waitlist: bool = False
    probe: bool = False
    max_attempts: int = 5
    timeout_seconds: int = 3600

    model_config = {"arbitrary_types_allowed": True}


class SnipeResult(BaseModel):
    status: str
    reason: str = ""
    attempted: list[str] = Field(default_factory=list)
    capture_path: str | None = None


def classify_outcome(text: str) -> str:
    lowered = (text or "").lower()
    for status, needles in OUTCOME_PATTERNS:
        if any(needle in lowered for needle in needles):
            return status
    return "unknown"


def authorize_live(
    term: str,
    *,
    i_understand: bool,
    stdin_fn: Callable[[], str] = sys.stdin.readline,
) -> None:
    """Live runs require the flag AND the exact term typed on stdin."""
    if not i_understand:
        raise SafetyError(
            "Live registration requires --i-understand. Use --dry-run (default) to rehearse."
        )
    typed = stdin_fn().strip()
    if typed != term:
        raise SafetyError(
            f"Confirmation failed: type the exact term code ({term}) on stdin to go live."
        )


def resolve_targets(
    term: str,
    *,
    plan_name: str | None = None,
    crns: Iterable[str] = (),
    plan_client: PlanClient | None = None,
    audit: Audit | None = None,
) -> list[Target]:
    """Resolve plan rows (or explicit CRNs) in planner-priority order."""
    snapshot = load_snapshot(term)
    explicit = [str(crn) for crn in crns]
    rows: list[tuple[str, str, str, float | None]] = []
    if plan_name:
        client = plan_client or PlanClient()
        plan = next((item for item in client.list_plans(term) if item.name == plan_name), None)
        if plan is None:
            raise SnipeError(f"No plan named {plan_name!r} in {term}.")
        for course in plan.courses:
            if course.crn:
                rows.append((course.crn, course.code, course.title, course.credits))
    for crn in explicit:
        section = snapshot.by_crn(crn) if snapshot is not None else None
        if section is None:
            raise SnipeError(f"CRN {crn} is not in the {term} catalog snapshot.")
        rows.append((crn, section.code, section.title, section.credits))
    if not rows:
        raise SnipeError("No targets: pass --plan or at least one --crn.")

    requirements = [r for r in requirements_from_audit(audit or AuditClient().audit()) if not r.satisfied]
    order = {
        "major_required": 0,
        "major_pool": 1,
        "gen_ed_required": 2,
        "gen_ed_fill": 3,
    }
    ranked: list[tuple[int, int, Target]] = []
    seen: set[str] = set()
    for index, (crn, code, title, credits) in enumerate(rows):
        if crn in seen:
            continue
        seen.add(crn)
        priority = "unknown"
        for requirement in requirements:
            if code in {target.code for target in requirement.targets}:
                priority = requirement.priority.value
                break
        ranked.append((order.get(priority, 9), index, Target(crn=crn, course=code, title=title, priority=priority, credits=credits)))
    ranked.sort(key=lambda item: (item[0], item[1]))
    return [target for _, _, target in ranked]


def _first_selector(page, selectors: Iterable[str]) -> str | None:
    for selector in selectors:
        try:
            if page.locator(selector).count():
                return selector
        except Exception:  # noqa: BLE001 - probing must never crash the run
            continue
    return None


def _first_visible_selector(page, selectors: Iterable[str]) -> str | None:
    """Like :func:`_first_selector`, but ignores hidden popups/controls."""
    for selector in selectors:
        try:
            locator = page.locator(selector)
            if locator.count() and locator.first.is_visible():
                return selector
        except Exception:  # noqa: BLE001 - probing must never crash the run
            continue
    return None


def _staging_looks_successful(text: str, crn: str | None = None) -> bool:
    lowered = (text or "").lower()
    if any(pattern in lowered for pattern in STAGING_SUCCESS_PATTERNS):
        return True
    # "Pending" is only a staging signal when it refers to a course/class row;
    # "pending financial aid" must not make us click Save.
    if "pending" in lowered and any(
        word in lowered for word in ("course", "class", "schedule", "section")
    ):
        return True
    # A schedule row for this CRN with a staged/scheduled marker is also clear.
    return bool(
        crn
        and crn in (text or "")
        and any(word in lowered for word in ("pending", "staged", "added", "scheduled"))
    )


def _staging_is_ambiguous(text: str) -> bool:
    """True for a blank/loading stage; only these are retried."""
    lowered = " ".join((text or "").lower().split())
    if not lowered:
        return True
    return any(pattern in lowered for pattern in STAGING_AMBIGUOUS_PATTERNS)


def probe_page(page, ssb_base: str) -> dict:
    """Check that the registration UI renders and our selectors resolve."""
    page.goto(f"{ssb_base}/classRegistration/classRegistration", wait_until="domcontentloaded")
    page.wait_for_timeout(2500)
    body = page.inner_text("body")[:2000]
    crn_selector = _first_selector(page, CRN_INPUT_SELECTORS)
    add_selector = _first_selector(page, ADD_BUTTON_SELECTORS)
    return {
        "url": page.url,
        "title": page.title(),
        "text": body,
        "crn_selector": crn_selector,
        "add_selector": add_selector,
        "available": bool(crn_selector and add_selector),
    }


def execute_targets(
    page,
    targets: list[Target],
    *,
    ssb_base: str,
    term: str,
    dry_run: bool,
    allow_waitlist: bool,
    max_attempts: int,
    timeout_seconds: int,
    sleep: Callable[[float], None] = time.sleep,
    clock: Callable[[], float] = time.monotonic,
) -> SnipeResult:
    """Drive Banner's **two-phase** registration UI for the resolved targets.

    Banner staging is not registration: the Add/CRN control creates a pending
    schedule row, and a separate Save click commits the batch
    (``#saveButton[data-registration-endpoint]``). This function therefore:

    * dry-run: fills every target CRN in order, clicks neither Add nor Save, and
      reports how many CRNs it would stage and that it would then click Save;
    * live: fills a CRN, clicks Add (stages), and continues for each staged
      success. After the loop it clicks Save **once**. ``registered`` is only
      returned from the post-Save page, never from the stage page;
    * a missing Save control returns ``staged_not_committed`` naming the staged
      CRNs; a Save click whose result is unclear returns ``save_unknown``;
    * hard stage failures (already registered / closed / prereq / conflict /
      hold / session dead) stop immediately and never click Save;
    * an authorization/OLR popup returns ``authorization_required`` and is never
      auto-accepted;
    * an unknown stage returns ``stage_unknown`` and never clicks Save. Only a
      blank/loading stage (``STAGING_AMBIGUOUS_PATTERNS``) is retried, up to
      ``max_attempts``; a page with any other unclear content stops on the first
      attempt. This avoids the old behaviour of retrying a staged row and then
      clicking Save blindly;
    * waitlist stays as before: ``waitlist`` when not allowed, otherwise click
      the waitlist control and return ``waitlist_joined`` (no Save click), since
      the two-phase bundle does not prove a waitlist commit path.

    Never drops/withdraws, never touches a CRN outside ``targets``.
    """
    started = clock()
    attempted: list[str] = []
    page.goto(f"{ssb_base}/classRegistration/classRegistration", wait_until="domcontentloaded")
    page.wait_for_timeout(2000)
    input_selector = _first_selector(page, CRN_INPUT_SELECTORS)
    add_selector = _first_selector(page, ADD_BUTTON_SELECTORS)
    if not input_selector or not add_selector:
        return SnipeResult(
            status="probe_unavailable",
            reason="Registration UI not reachable: CRN input/Add button selectors did not resolve (term likely not eligible).",
        )

    if dry_run:
        for target in targets:
            attempted.append(target.crn)
            page.fill(input_selector, target.crn)
        return SnipeResult(
            status="dry_run",
            reason=(
                f"would stage {len(attempted)} CRN(s) with Add and then click Save once: "
                + ", ".join(attempted)
            ),
            attempted=attempted,
        )

    staged: list[str] = []
    for target in targets:
        attempts = 0
        while True:
            if clock() - started > timeout_seconds:
                return SnipeResult(status="timeout", reason="hard timeout reached", attempted=attempted)
            attempts += 1
            attempted.append(target.crn)
            page.fill(input_selector, target.crn)
            page.click(add_selector)
            page.wait_for_timeout(2500)
            body = page.inner_text("body")

            popup = _first_visible_selector(page, AUTHORIZATION_SELECTORS)
            if popup:
                return SnipeResult(
                    status="authorization_required",
                    reason=(
                        f"CRN {target.crn}: registration needs manual authorization/OLR "
                        f"({popup}); the popup was not accepted"
                    ),
                    attempted=attempted,
                )

            outcome = classify_outcome(body)
            if outcome in {"already_registered", "closed", "prereq", "conflict", "hold", "session_dead"}:
                return SnipeResult(
                    status=outcome,
                    reason=f"CRN {target.crn}: {outcome}",
                    attempted=attempted,
                )
            if outcome == "waitlist":
                if not allow_waitlist:
                    return SnipeResult(
                        status="waitlist",
                        reason="waitlist offered; not joining without --allow-waitlist",
                        attempted=attempted,
                    )
                wait_selector = _first_selector(page, WAITLIST_SELECTORS)
                if wait_selector is None:
                    return SnipeResult(
                        status="waitlist",
                        reason="waitlist offered but no waitlist control resolved",
                        attempted=attempted,
                    )
                page.click(wait_selector)
                page.wait_for_timeout(2000)
                return SnipeResult(
                    status="waitlist_joined",
                    reason=f"joined waitlist for {target.crn}",
                    attempted=attempted,
                )

            if outcome == "registered" or _staging_looks_successful(body, target.crn):
                # "registered" on the stage page is still only a staged row.
                staged.append(target.crn)
                break

            if _staging_is_ambiguous(body) and attempts < max_attempts:
                sleep(1.0)
                continue
            return SnipeResult(
                status="stage_unknown",
                reason=(
                    f"CRN {target.crn}: Add did not show a clear staging success or a "
                    "known failure; no Save was clicked"
                ),
                attempted=attempted,
            )

    if not staged:
        return SnipeResult(status="no_targets", reason="no target was staged", attempted=attempted)

    save_selector = _first_visible_selector(page, SAVE_BUTTON_SELECTORS)
    if not save_selector:
        return SnipeResult(
            status="staged_not_committed",
            reason=(
                f"Staged CRNs {', '.join(staged)}; Save control not found — "
                "nothing was committed"
            ),
            attempted=attempted,
        )
    try:
        page.click(save_selector)
    except Exception as exc:  # noqa: BLE001 - a failed Save must never look successful
        return SnipeResult(
            status="staged_not_committed",
            reason=(
                f"Staged CRNs {', '.join(staged)}; Save click failed "
                f"({type(exc).__name__}) — nothing was committed"
            ),
            attempted=attempted,
        )
    page.wait_for_timeout(2500)
    body = page.inner_text("body")
    popup = _first_visible_selector(page, AUTHORIZATION_SELECTORS)
    if popup:
        return SnipeResult(
            status="authorization_required",
            reason=(
                f"Save for {', '.join(staged)} opened an authorization/OLR popup "
                f"({popup}); the popup was not accepted"
            ),
            attempted=attempted,
        )
    outcome = classify_outcome(body)
    if outcome == "registered":
        return SnipeResult(
            status="registered",
            reason=f"committed {', '.join(staged)} after Save",
            attempted=attempted,
        )
    if outcome in {"already_registered", "waitlist", "closed", "prereq", "conflict", "hold", "session_dead"}:
        return SnipeResult(
            status=outcome,
            reason=f"Save response for {', '.join(staged)}: {outcome}",
            attempted=attempted,
        )
    snippet = " ".join((body or "").split())[:200]
    return SnipeResult(
        status="save_unknown",
        reason=(
            f"Save clicked for {', '.join(staged)} but the result was unclear: {snippet!r}"
        ),
        attempted=attempted,
    )


def capture_traffic(page, path: Path) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    handle = path.open("a", encoding="utf-8")

    def on_response(response):
        if "unw.edu" not in response.url:
            return
        try:
            body = response.text()[:4000]
        except Exception:  # noqa: BLE001
            body = None
        handle.write(json.dumps({"status": response.status, "method": response.request.method, "url": response.url, "body": body}) + "\n")
        handle.flush()

    page.on("response", on_response)
    return path


def wait_for_window(term: str, *, offset_seconds: int, interval_seconds: int, timeout_seconds: int,
                    sleep: Callable[[float], None] = time.sleep, clock: Callable[[], float] = time.monotonic,
                    window_fn: Callable[[str], RegistrationWindow] = window) -> RegistrationWindow:
    """Sleep until the window start (plus offset), then poll until eligible."""
    started = clock()
    current = window_fn(term)
    if current.start is not None and not current.eligible:
        target = current.start + timedelta(seconds=offset_seconds)
        wait = (target - datetime.now(current.start.tzinfo)).total_seconds()
        if wait > 0:
            print(f"Waiting {countdown(wait)} for {term} to open at {target.isoformat()} …", flush=True)
            sleep(wait)
    while clock() - started < timeout_seconds:
        current = window_fn(term)
        if current.eligible:
            return current
        print(f"Not eligible yet ({current.reason[:120]}); retrying in {interval_seconds}s", flush=True)
        sleep(interval_seconds)
    return window_fn(term)


def _capture_path(term: str, label: str) -> Path:
    stamp = datetime.now().strftime("%Y%m%dT%H%M%S")
    return CAPTURES / f"registration-{term}-{label}-{stamp}.jsonl"


def _seats_ready(term: str, targets: list[Target], ssb: object) -> list[Target]:
    ready: list[Target] = []
    for subject, number in {(target.course.split()[0], target.course.split()[1]) for target in targets if target.course}:
        page = ssb.search(term=term, subject=subject, course_number=number, page_max_size=100)
        open_crns = {section.crn for section in page.sections if section.open and (section.seats_available or 0) > 0}
        ready.extend(target for target in targets if target.course.startswith(f"{subject} {number}") and target.crn in open_crns)
    return ready


def check_registration_eligible(term: str, *, ssb: object | None = None) -> SnipeResult | None:
    """Bind ``term`` in registration mode and return Banner's rejection, if any.

    This is the preflight the browser run uses before opening the registration UI.
    It reports Banner's exact ``studentEligFailures`` text and never guesses an
    eligibility result.
    """
    if ssb is None:
        from .auth import ensure
        from .banner.ssb import SSBClient

        ssb = SSBClient(ensure())
    eligibility = ssb.set_term(term, mode="registration")
    if not eligibility.student_elig_valid:
        reason = "; ".join(eligibility.failures) or "term not eligible"
        return SnipeResult(status="not_eligible", reason=reason)
    return None


def run_browser(options: SnipeOptions, targets: list[Target], *, ssb_base: str | None = None) -> SnipeResult:
    """Execute a probe/dry-run/live run in one browser context.

    For every non-probe run the term is bound in registration mode **before** the
    registration UI is opened; an ineligible term returns ``not_eligible`` with
    Banner's failure text. In ``seats`` mode the seat wait runs first, then the
    registration binding happens immediately before execution. ``--probe`` is
    unchanged and intentionally skips the eligibility gate.
    """
    from playwright.sync_api import sync_playwright

    from .auth import BROWSER_UA, ensure, login, session_valid
    from .config import STATE_PATH, get_settings
    from .banner.ssb import SSBClient as _SSBClient

    settings = get_settings()
    base = ssb_base or settings.ssb_base
    if not session_valid(settings):
        login(force=True, settings=settings)
    state_path = STATE_PATH
    capture = _capture_path(options.term, "probe" if options.probe else ("live" if options.live else "dry"))

    if not options.probe:
        ssb = _SSBClient(ensure(settings))
        if options.mode == "window" and not options.dry_run:
            wait_for_window(
                options.term,
                offset_seconds=options.offset_seconds,
                interval_seconds=options.interval_seconds,
                timeout_seconds=options.timeout_seconds,
            )
        if options.mode == "seats":
            ssb.set_term(options.term, mode="search")
            if options.dry_run:
                ready = _seats_ready(options.term, targets, ssb)
                print(f"Seat check: {len(ready)}/{len(targets)} target(s) currently open.", flush=True)
            else:
                deadline = time.monotonic() + options.timeout_seconds
                ready = []
                while time.monotonic() < deadline:
                    ready = _seats_ready(options.term, targets, ssb)
                    if ready:
                        break
                    print(f"No seats yet for {', '.join(t.crn for t in targets)}; waiting {options.interval_seconds}s", flush=True)
                    time.sleep(options.interval_seconds)
            targets = ready or []
            if not targets:
                return SnipeResult(
                    status="no_seats",
                    reason="no target section has an open seat yet",
                    capture_path=str(capture),
                )
        blocked = check_registration_eligible(options.term, ssb=ssb)
        if blocked is not None:
            return blocked

    result: SnipeResult
    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=True)
        context = browser.new_context(
            storage_state=str(state_path),
            user_agent=BROWSER_UA,
            viewport={"width": 1500, "height": 1000},
            locale="en-US",
            timezone_id="America/Chicago",
        )
        page = context.new_page()
        capture_traffic(page, capture)
        try:
            if options.probe:
                info = probe_page(page, base)
                result = SnipeResult(
                    status="probe_ok" if info["available"] else "probe_unavailable",
                    reason=json.dumps(info, ensure_ascii=False),
                    capture_path=str(capture),
                )
            else:
                result = execute_targets(
                    page,
                    targets,
                    ssb_base=base,
                    term=options.term,
                    dry_run=options.dry_run,
                    allow_waitlist=options.allow_waitlist,
                    max_attempts=options.max_attempts,
                    timeout_seconds=options.timeout_seconds,
                )
                result.capture_path = str(capture)
        finally:
            context.close()
            browser.close()
    return result
