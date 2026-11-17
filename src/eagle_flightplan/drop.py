"""Confirmed course drops and term withdrawals through Banner's registration UI.

Mapped live on 2026-11-17 for term ``202620`` (read-only inspection; no drop was
submitted):

* every committed row renders a hidden ``select.actionDdl`` with
  ``id="action-<crn>-ddl"``, ``data-property="selectedAction"``, ``data-term`` and
  ``data-coursereferencenumber``. The only action option is ``value="DW"``,
  ``text="Web Dropped"`` (``None``/``null`` is the default);
* Banner exposes **no separate withdraw** action, control or endpoint. A
  withdrawal is the same ``DW`` action selected on every registered row followed
  by one Submit;
* ``#saveButton`` (text ``Submit``) carries
  ``data-registration-endpoint="/StudentRegistrationSsb/ssb/classRegistration/submitRegistration"``.
  Banner's Backbone batch save appends ``/batch`` and POSTs
  ``{"create": [], "update": [<full registration row with selectedAction="DW">],
  "destroy": []}`` as JSON. That serialisation comes from the shipped client JS
  (``ss-bannerSelfService``), not from an exercised request: this module drives
  the real UI and never guesses a direct write endpoint;
* selecting ``DW`` flips the row's ``recordStatus`` to ``Q`` and enables Submit;
  no network request is sent until Submit.

Safety rules: one Submit per call, exactly one CRN for a drop, every registered
CRN (enumerated first) for a withdraw, never a CRN that is not currently
``Registered``, no retries after Submit, and a read-back of the schedule before
and after.
"""

from __future__ import annotations

import contextlib
import json
import sys
import time
from collections.abc import Callable
from datetime import datetime
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, Field

from .auth import AuthError, SessionExpiredError
from .banner.ssb import SSBClient
from .config import CAPTURES_DIR, STATE_PATH

CAPTURES = CAPTURES_DIR
ACTION_SELECT = "select.actionDdl"
DROP_ACTION = "DW"
DROP_ACTION_LABEL = "Web Dropped"
SAVE_BUTTON = "#saveButton"
SUBMIT_ENDPOINT_SUFFIX = "/submitRegistration"
SUBMIT_BATCH_SUFFIX = "/submitRegistration/batch"

#: Statuses that prove the requested action completed.
SUCCESS_STATUSES = frozenset({"dropped", "withdrawn", "dry_run"})

#: Banner's row status text when a registration row is live.
REGISTERED_STATUS = "registered"


class DropError(RuntimeError):
    pass


class DropSafetyError(DropError):
    pass


class RegisteredCourse(BaseModel):
    crn: str
    course: str = ""
    title: str = ""
    credits: float | None = None
    term: str = ""
    status: str = "registered"
    row_id: int | None = None


class DropOptions(BaseModel):
    term: str
    crn: str | None = None
    action: Literal["drop", "withdraw"] = "drop"
    live: bool = False
    i_understand: bool = False
    commit_timeout: float = 8.0
    wait_ms: int = 2500
    expected_crns: list[str] = Field(default_factory=list)


class DropResult(BaseModel):
    status: str
    action: Literal["drop", "withdraw"] = "drop"
    term: str = ""
    crn: str | None = None
    reason: str = ""
    banner_message: str = ""
    response_status: str = ""
    page_status: str = ""
    schedule_before: list[RegisteredCourse] = Field(default_factory=list)
    schedule_after: list[RegisteredCourse] | None = None
    removed: list[RegisteredCourse] = Field(default_factory=list)
    remaining: list[RegisteredCourse] = Field(default_factory=list)
    writes_performed: list[str] = Field(default_factory=list)
    submit_url: str = ""
    capture_path: str | None = None
    dry_run: bool = True
    after_read_error: str = ""


# --------------------------------------------------------------------------- #
# schedule reads (read-only)
# --------------------------------------------------------------------------- #
def read_schedule(term: str, *, ssb: SSBClient | None = None) -> list[RegisteredCourse]:
    """Every currently registered row for ``term``, read from Banner (no writes).

    Uses ``submitSummaryRegistration`` (the same read the registration grid uses),
    so asynchronous/online sections that never appear in the calendar events are
    included. Only ``statusIndicator == "R"`` rows count as registered.
    """
    client = ssb or SSBClient()
    rows = client.registration_rows(term)
    courses: list[RegisteredCourse] = []
    for row in rows:
        indicator = str(row.get("statusIndicator") or "").upper()
        status_text = str(row.get("statusDescription") or "").strip()
        if indicator != "R" and status_text.lower() != REGISTERED_STATUS:
            continue
        subject = str(row.get("subject") or "").strip()
        number = str(row.get("courseNumber") or "").strip()
        credits = row.get("creditHour")
        courses.append(
            RegisteredCourse(
                crn=str(row.get("courseReferenceNumber") or ""),
                course=f"{subject} {number}".strip(),
                title=str(row.get("courseTitle") or ""),
                credits=float(credits) if credits is not None else None,
                term=str(row.get("term") or term),
                status=REGISTERED_STATUS,
                row_id=row.get("id") if isinstance(row.get("id"), int) else None,
            )
        )
    courses.sort(key=lambda course: (0, int(course.crn)) if course.crn.isdigit() else (1, 0))
    return [course for course in courses if course.crn]


def schedule_payload(term: str, courses: list[RegisteredCourse]) -> dict[str, Any]:
    return {
        "term": term,
        "count": len(courses),
        "total_credits": round(sum(course.credits or 0 for course in courses), 2),
        "courses": [course.model_dump() for course in courses],
    }


# --------------------------------------------------------------------------- #
# UI mapping
# --------------------------------------------------------------------------- #
_STATUS_JS = """el => {
  const tr = el.closest('tr');
  const td = tr && tr.querySelector('td[data-property="status"]');
  return td ? td.innerText.trim() : '';
}"""


def page_rows(page, term: str) -> list[RegisteredCourse]:
    """Registered rows currently rendered on the class-registration page."""
    locator = page.locator(ACTION_SELECT)
    rows: list[RegisteredCourse] = []
    for index in range(locator.count()):
        item = locator.nth(index)
        if item.get_attribute("data-term") not in (None, term):
            continue
        crn = str(item.get_attribute("data-coursereferencenumber") or "").strip()
        if not crn:
            continue
        try:
            status_text = str(item.evaluate(_STATUS_JS) or "").strip()
        except Exception:
            status_text = ""
        rows.append(
            RegisteredCourse(
                crn=crn,
                term=term,
                status=status_text.lower() or "unknown",
            )
        )
    return rows


def page_row(page, term: str, crn: str) -> RegisteredCourse | None:
    for row in page_rows(page, term):
        if row.crn == crn:
            return row
    return None


def _is_registered(row: RegisteredCourse | None) -> bool:
    return row is not None and row.status.strip().lower() == REGISTERED_STATUS


def _dropped_text(text: str) -> bool:
    lowered = " ".join((text or "").lower().split())
    return any(
        marker in lowered
        for marker in (
            "web dropped",
            "successfully dropped",
            "has been dropped",
            "course dropped",
            "dropped from",
            "withdrawn",
            "withdrawal successful",
        )
    )


def _row_is_dropped(row: dict[str, Any]) -> bool:
    indicator = str(row.get("statusIndicator") or "").upper()
    status_text = str(row.get("statusDescription") or "").lower()
    return (
        indicator in {"D", "W"}
        or str(row.get("removeIndicator") or "").upper() == "Y"
        or "dropped" in status_text
        or "withdrawn" in status_text
    )


def _row_error(row: dict[str, Any]) -> str:
    messages = row.get("messages") or []
    if isinstance(messages, list):
        for message in messages:
            if isinstance(message, dict) and str(message.get("type") or "").lower() in {
                "error",
                "exception",
                "warning",
            }:
                return str(message.get("message") or "")
    return ""


def classify_submit_payload(payload: dict[str, Any] | None) -> tuple[str, str]:
    """Map a Submit response to ``(verdict, Banner message)``.

    ``verdict`` is ``dropped`` (proven), ``rejected``, ``confirm`` (Banner asks
    for a separate auto-drop confirmation; never auto-accepted) or ``unknown``.
    """
    if not payload:
        return "unknown", ""
    message = payload.get("message")
    if isinstance(message, dict):
        text = str(message.get("message") or "").strip()
        kind = str(message.get("type") or "").strip().lower()
        if kind == "confirm":
            return "confirm", text
        if kind in {"error", "exception"}:
            return "rejected", text
        if kind == "ok" and _dropped_text(text):
            return "dropped", text
        ok_text = text if kind == "ok" else ""
    elif isinstance(message, str) and message.strip():
        return "rejected", message.strip()
    else:
        ok_text = ""

    data = payload.get("data") or {}
    update_rows = data.get("update") or []
    for row in update_rows:
        if not isinstance(row, dict):
            continue
        error = _row_error(row)
        if error:
            return "rejected", error
        if _row_is_dropped(row):
            text = str(row.get("statusDescription") or DROP_ACTION_LABEL).strip()
            return "dropped", text
    return "unknown", ok_text


def _last_submit(responses: list[dict[str, Any]] | None) -> dict[str, Any] | None:
    if not responses:
        return None
    for entry in reversed(responses):
        if SUBMIT_BATCH_SUFFIX in str(entry.get("url") or ""):
            parsed = entry.get("json")
            if isinstance(parsed, dict):
                return parsed
            body = entry.get("body") or ""
            try:
                parsed = json.loads(body)
            except Exception:
                return {"message": str(body)[:400]} if body else None
            return parsed if isinstance(parsed, dict) else None
    return None


def _last_submit_url(responses: list[dict[str, Any]] | None) -> str:
    if not responses:
        return ""
    for entry in reversed(responses):
        if SUBMIT_BATCH_SUFFIX in str(entry.get("url") or ""):
            return str(entry.get("url") or "")
    return ""


def _wait_enabled(
    save,
    *,
    timeout: float,
    sleep: Callable[[float], None],
    clock: Callable[[], float],
) -> bool:
    deadline = clock() + timeout
    while True:
        try:
            if not save.is_disabled():
                return True
        except Exception:
            return False
        if clock() >= deadline:
            return False
        sleep(0.25)


def _set_action(select, value: str = DROP_ACTION) -> None:
    """Set the real row action and fire Banner's change handler.

    The native select is hidden by select2, so Playwright's actionability-checked
    ``select_option`` is not reliable here. This is the same DOM change the live
    probe used to update the row model and enable Submit; nothing is submitted.
    """
    select.evaluate(
        "(el, v) => { el.value = v; el.dispatchEvent(new Event('change', {bubbles: true})); }",
        value,
    )


def _submit_endpoint(save) -> str:
    try:
        return str(save.get_attribute("data-registration-endpoint") or "")
    except Exception:
        return ""


def _page_status(page, term: str, crn: str) -> str:
    row = page_row(page, term, crn)
    return row.status if row else ""


def execute_drop(
    page,
    term: str,
    crn: str,
    *,
    dry_run: bool = False,
    responses: list[dict[str, Any]] | None = None,
    commit_timeout: float = 8.0,
    wait_ms: int = 2500,
    sleep: Callable[[float], None] = time.sleep,
    clock: Callable[[], float] = time.monotonic,
) -> DropResult:
    """Drive one drop in the already-open registration page.

    Never touches another row and never clicks Submit when the target row, the
    ``DW`` option or the canonical Submit endpoint is missing.
    """
    result = DropResult(status="", action="drop", term=term, crn=crn, dry_run=dry_run)
    row = page_row(page, term, crn)
    if not _is_registered(row):
        result.status = "not_registered"
        result.reason = (
            f"CRN {crn} is not currently Registered in {term}"
            + (f" (page status: {row.status!r})" if row else " (row not on the page)")
        )
        return result

    select = page.locator(f"#action-{crn}-ddl")
    if not select.count():
        result.status = "registration_ui_unavailable"
        result.reason = f"Action control #action-{crn}-ddl did not resolve; nothing was changed."
        return result
    if select.get_attribute("data-term") not in (None, term):
        result.status = "registration_ui_unavailable"
        result.reason = (
            f"Action control for {crn} belongs to term "
            f"{select.get_attribute('data-term')!r}, not {term}; nothing was changed."
        )
        return result
    if dry_run:
        result.status = "dry_run"
        result.reason = (
            f"would select {DROP_ACTION_LABEL!r} (value {DROP_ACTION}) on CRN {crn} "
            f"and click Submit once"
        )
        return result

    save = page.locator(SAVE_BUTTON)
    endpoint = _submit_endpoint(save)
    if not endpoint.endswith(SUBMIT_ENDPOINT_SUFFIX):
        result.status = "registration_ui_unavailable"
        result.reason = (
            "Submit endpoint attribute is missing or changed "
            f"({endpoint or 'absent'}); nothing was changed."
        )
        return result
    result.submit_url = endpoint + "/batch"

    _set_action(select)
    if select.input_value() != DROP_ACTION:
        result.status = "drop_not_staged"
        result.reason = (
            f"Could not set the {DROP_ACTION_LABEL!r} action on CRN {crn}; "
            "Submit was not clicked."
        )
        return result
    if not _wait_enabled(save, timeout=commit_timeout, sleep=sleep, clock=clock):
        result.status = "drop_not_submitted"
        result.reason = (
            f"{DROP_ACTION_LABEL!r} was selected on CRN {crn} but Submit never "
            "became enabled; nothing was committed."
        )
        return result

    mark = len(responses) if responses is not None else 0
    try:
        save.click()
    except Exception as exc:
        result.status = "drop_not_submitted"
        result.reason = (
            f"{DROP_ACTION_LABEL!r} was selected on CRN {crn} but the Submit click "
            f"failed ({type(exc).__name__}); nothing was committed."
        )
        return result

    result.writes_performed = ["drop_course"]
    page.wait_for_timeout(wait_ms)
    new_responses = responses[mark:] if responses is not None else []
    payload = _last_submit(new_responses)
    verdict, message = classify_submit_payload(payload)
    result.status = "submitted"
    result.response_status = verdict
    result.banner_message = message
    result.submit_url = _last_submit_url(new_responses) or result.submit_url
    result.page_status = _page_status(page, term, crn)
    result.reason = message or (
        "Submit clicked; response did not say whether the drop completed."
    )
    return result


def execute_withdraw(
    page,
    term: str,
    *,
    expected_crns: list[str],
    dry_run: bool = False,
    responses: list[dict[str, Any]] | None = None,
    commit_timeout: float = 8.0,
    wait_ms: int = 2500,
    sleep: Callable[[float], None] = time.sleep,
    clock: Callable[[], float] = time.monotonic,
) -> DropResult:
    """Drive a withdrawal (``DW`` on every registered row, one Submit)."""
    result = DropResult(status="", action="withdraw", term=term, dry_run=dry_run)
    rendered = [row for row in page_rows(page, term) if _is_registered(row)]
    rendered_crns = {row.crn for row in rendered}
    wanted = {str(crn) for crn in expected_crns}
    if rendered_crns != wanted:
        extra = sorted(rendered_crns - wanted)
        missing = sorted(wanted - rendered_crns)
        result.status = "schedule_changed"
        result.reason = (
            "The page no longer matches the schedule read before this call; "
            f"extra on page: {', '.join(extra) or 'none'}; "
            f"missing from page: {', '.join(missing) or 'none'}. Nothing was changed."
        )
        return result
    if not wanted:
        result.status = "not_registered"
        result.reason = f"No registered courses in {term}; nothing was changed."
        return result

    save = page.locator(SAVE_BUTTON)
    endpoint = _submit_endpoint(save)
    if not endpoint.endswith(SUBMIT_ENDPOINT_SUFFIX):
        result.status = "registration_ui_unavailable"
        result.reason = (
            "Submit endpoint attribute is missing or changed "
            f"({endpoint or 'absent'}); nothing was changed."
        )
        return result
    result.submit_url = endpoint + "/batch"

    if dry_run:
        result.status = "dry_run"
        result.reason = (
            f"would select {DROP_ACTION_LABEL!r} on {len(wanted)} registered CRN(s) "
            f"({', '.join(sorted(wanted))}) and click Submit once"
        )
        return result

    for crn in sorted(wanted):
        select = page.locator(f"#action-{crn}-ddl")
        if not select.count():
            result.status = "drop_not_staged"
            result.reason = (
                f"Action control #action-{crn}-ddl did not resolve; Submit was not clicked."
            )
            return result
        _set_action(select)
        if select.input_value() != DROP_ACTION:
            result.status = "drop_not_staged"
            result.reason = (
                f"Could not set the {DROP_ACTION_LABEL!r} action on CRN {crn}; "
                "Submit was not clicked."
            )
            return result
    if not _wait_enabled(save, timeout=commit_timeout, sleep=sleep, clock=clock):
        result.status = "drop_not_submitted"
        result.reason = (
            f"{DROP_ACTION_LABEL!r} was selected on {len(wanted)} CRN(s) but Submit "
            "never became enabled; nothing was committed."
        )
        return result

    mark = len(responses) if responses is not None else 0
    try:
        save.click()
    except Exception as exc:
        result.status = "drop_not_submitted"
        result.reason = (
            f"The Submit click failed ({type(exc).__name__}); nothing was committed."
        )
        return result

    result.writes_performed = ["withdraw"]
    page.wait_for_timeout(wait_ms)
    new_responses = responses[mark:] if responses is not None else []
    payload = _last_submit(new_responses)
    verdict, message = classify_submit_payload(payload)
    result.status = "submitted"
    result.response_status = verdict
    result.banner_message = message
    result.submit_url = _last_submit_url(new_responses) or result.submit_url
    remaining = [row for row in page_rows(page, term) if _is_registered(row)]
    result.page_status = (
        "all dropped"
        if not remaining
        else "still registered: " + ", ".join(row.crn for row in remaining)
    )
    result.reason = message or (
        "Submit clicked; response did not say whether the withdrawal completed."
    )
    return result


# --------------------------------------------------------------------------- #
# final read-back composition
# --------------------------------------------------------------------------- #
def finalize_drop(
    action: Literal["drop", "withdraw"],
    term: str,
    crn: str | None,
    execution: DropResult,
    before: list[RegisteredCourse],
    after: list[RegisteredCourse] | None,
    *,
    after_read_error: str = "",
) -> DropResult:
    """Combine the UI result with the post-call schedule read.

    Absence from the read-back proves the row is gone; a Banner rejection stays a
    rejection; an unclear response is never promoted to success.
    """
    result = execution.model_copy(deep=True)
    result.action = action
    result.term = term
    result.crn = crn
    result.schedule_before = before
    result.schedule_after = after
    result.after_read_error = after_read_error
    before_crns = {row.crn for row in before}
    after_crns = {row.crn for row in after} if after is not None else None
    if after is not None:
        result.removed = [row for row in before if row.crn not in after_crns]
        result.remaining = list(after)
    else:
        result.remaining = list(before)

    if execution.status != "submitted":
        return result
    if execution.response_status == "confirm":
        result.status = "drop_confirm_required"
        result.reason = (
            "Banner answered with a confirm prompt instead of committing "
            f"({execution.banner_message or 'no message'}); it was NOT accepted. "
            "Read the schedule before deciding anything else."
        )
        return result
    if execution.response_status == "rejected":
        result.status = "drop_rejected"
        result.reason = execution.banner_message or "Banner rejected the drop."
        return result

    if action == "drop" and crn is not None:
        if after_crns is not None:
            if crn in before_crns and crn not in after_crns:
                result.status = "dropped"
                result.reason = f"CRN {crn} is no longer registered in {term}."
                return result
            if execution.response_status == "dropped" or _dropped_text(execution.page_status):
                result.status = "drop_unconfirmed"
                result.reason = (
                    f"Banner's response suggests the drop, but the read-back still lists "
                    f"CRN {crn}. Verify in Banner; do not resubmit."
                )
                return result
        elif execution.response_status == "dropped" or _dropped_text(execution.page_status):
            result.status = "drop_unconfirmed"
            result.reason = (
                f"Banner reported the drop, but the post-call read-back failed "
                f"({after_read_error or 'unknown error'}). Verify in Banner; "
                "do not resubmit."
            )
            return result
        result.status = "drop_unknown"
        result.reason = (
            "Submit was clicked but the outcome could not be proven; do not retry. "
            "Read the Banner schedule first."
        )
        return result

    # withdraw
    if after is not None:
        if not after:
            result.status = "withdrawn"
            result.reason = f"No registered courses remain in {term}."
            return result
        if result.removed:
            result.status = "withdraw_partial"
            result.reason = (
                f"Withdrew {', '.join(row.crn for row in result.removed)}; "
                f"still registered: {', '.join(row.crn for row in result.remaining)}. "
                "Do not retry blindly."
            )
            return result
    if execution.response_status == "dropped" or execution.page_status.startswith("all dropped"):
        result.status = "withdraw_unconfirmed"
        result.reason = (
            "Banner reported the withdrawal, but the post-call read-back did not confirm "
            f"({after_read_error or 'no schedule read'}). Verify in Banner; do not resubmit."
        )
        return result
    result.status = "withdraw_unknown"
    result.reason = (
        "Submit was clicked but the withdrawal could not be proven; do not retry. "
        "Read the Banner schedule first."
    )
    return result


# --------------------------------------------------------------------------- #
# browser runner
# --------------------------------------------------------------------------- #
def _capture_path(term: str, action: str) -> Path:
    stamp = datetime.now().strftime("%Y%m%dT%H%M%S")
    return CAPTURES / f"registration-{term}-{action}-{stamp}.jsonl"


def _open_registration_page(page, base: str, term: str, *, label: str = "") -> None:
    """Landing card -> term selection -> class registration (the proven path)."""
    from .snipe import select_term

    page.goto(f"{base}/registration/registration", wait_until="domcontentloaded")
    page.wait_for_timeout(1500)
    link = page.locator("#registerLink")
    if link.count():
        link.first.click()
        page.wait_for_timeout(3000)
    selected = select_term(page, base, term, label)
    if selected is not None:
        raise DropError(f"{selected.status}: {selected.reason}")
    deadline = time.monotonic() + 30
    while time.monotonic() < deadline:
        if page.locator(ACTION_SELECT).count() or page.locator("#txt_crn").count():
            return
        page.wait_for_timeout(1000)
    raise DropError("the class-registration schedule did not render")


def run_drop(
    options: DropOptions,
    *,
    ssb: SSBClient | None = None,
    base: str | None = None,
    state_path: Path | None = None,
) -> DropResult:
    """Read the schedule, then (live only) drive one drop/withdraw in Chromium."""
    from . import auth
    from .config import get_settings

    if options.action == "drop" and not options.crn:
        raise DropError("A drop needs exactly one CRN.")
    if options.action == "withdraw" and options.crn:
        raise DropError("A withdraw takes no CRN; it targets every registered course.")
    if options.live and not options.i_understand:
        raise DropSafetyError(
            "Live drop/withdraw requires explicit consent (i_understand)."
        )

    settings = get_settings()
    base = base or settings.ssb_base
    state = state_path or STATE_PATH
    if not auth.session_valid(settings):
        if not auth.auto_login_enabled():
            return DropResult(
                status="session_dead",
                action=options.action,
                term=options.term,
                crn=options.crn,
                reason=(
                    "No valid UNW session and auto-login is disabled (UNW_AUTO_LOGIN=0). "
                    f"Run `{auth.MANUAL_LOGIN_COMMAND}` to sign in."
                ),
            )
        auth.login(force=True, settings=settings)

    before = read_schedule(options.term, ssb=ssb or SSBClient(auth.ensure(settings)))
    if options.action == "drop":
        target = next((row for row in before if row.crn == str(options.crn)), None)
        if target is None:
            return DropResult(
                status="not_registered",
                action="drop",
                term=options.term,
                crn=str(options.crn),
                reason=(
                    f"CRN {options.crn} is not currently registered in {options.term}; "
                    "nothing was changed."
                ),
                schedule_before=before,
                schedule_after=before,
            )
    elif not before:
        return DropResult(
            status="not_registered",
            action="withdraw",
            term=options.term,
            reason=f"No registered courses in {options.term}; nothing was changed.",
            schedule_before=before,
            schedule_after=before,
        )
    if options.action == "withdraw" and options.expected_crns:
        expected = {str(crn) for crn in options.expected_crns}
        current = {row.crn for row in before}
        if expected != current:
            return DropResult(
                status="schedule_changed",
                action="withdraw",
                term=options.term,
                reason=(
                    "The registered schedule changed since it was confirmed: "
                    f"new {', '.join(sorted(current - expected)) or 'none'}; "
                    f"gone {', '.join(sorted(expected - current)) or 'none'}. "
                    "Nothing was changed; re-read and confirm again."
                ),
                schedule_before=before,
                schedule_after=before,
                remaining=before,
            )

    if not options.live:
        counted = 1 if options.action == "drop" else len(before)
        listed = (
            str(options.crn)
            if options.action == "drop"
            else ", ".join(row.crn for row in before)
        )
        return DropResult(
            status="dry_run",
            action=options.action,
            term=options.term,
            crn=options.crn,
            reason=(
                f"would select {DROP_ACTION_LABEL!r} on {counted} CRN(s) "
                f"({listed}) and click Submit once"
            ),
            schedule_before=before,
            schedule_after=before,
            remaining=before,
        )

    from playwright.sync_api import sync_playwright

    from .auth import BROWSER_UA

    capture = _capture_path(options.term, options.action)
    responses: list[dict[str, Any]] = []

    def on_response(response):
        try:
            if SUBMIT_BATCH_SUFFIX not in response.url:
                return
            body = response.text()[:20000]
            try:
                parsed = json.loads(body)
            except Exception:
                parsed = None
            responses.append(
                {
                    "status": response.status,
                    "url": response.url,
                    "body": body,
                    "json": parsed if isinstance(parsed, dict) else None,
                }
            )
        except Exception:
            return

    execution: DropResult
    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=True)
        context = browser.new_context(
            storage_state=str(state),
            user_agent=BROWSER_UA,
            viewport={"width": 1500, "height": 1000},
            locale="en-US",
            timezone_id="America/Chicago",
        )
        page = context.new_page()
        from .snipe import capture_traffic

        capture_handle = capture_traffic(page, capture)
        page.on("response", on_response)
        try:
            _open_registration_page(page, base, options.term)
            if options.action == "drop":
                execution = execute_drop(
                    page,
                    options.term,
                    str(options.crn),
                    responses=responses,
                    commit_timeout=options.commit_timeout,
                    wait_ms=options.wait_ms,
                )
            else:
                execution = execute_withdraw(
                    page,
                    options.term,
                    expected_crns=[row.crn for row in before],
                    responses=responses,
                    commit_timeout=options.commit_timeout,
                    wait_ms=options.wait_ms,
                )
        except (SessionExpiredError, AuthError) as exc:
            execution = DropResult(
                status="session_dead",
                action=options.action,
                term=options.term,
                crn=options.crn,
                reason=f"session expired ({type(exc).__name__})",
            )
        except Exception as exc:
            execution = DropResult(
                status="run_failed",
                action=options.action,
                term=options.term,
                crn=options.crn,
                reason=f"{type(exc).__name__}: {exc}",
            )
        finally:
            with contextlib.suppress(Exception):
                page.remove_listener("response", on_response)
            capture_handle.close()
            context.close()
            browser.close()

    after: list[RegisteredCourse] | None = None
    after_error = ""
    try:
        after = read_schedule(options.term, ssb=SSBClient(auth.ensure(settings)))
    except Exception as exc:
        after_error = f"{type(exc).__name__}: {exc}"

    result = finalize_drop(
        options.action,
        options.term,
        options.crn,
        execution,
        before,
        after,
        after_read_error=after_error,
    )
    result.capture_path = str(capture)
    return result


# --------------------------------------------------------------------------- #
# CLI consent
# --------------------------------------------------------------------------- #
def authorize_live_drop(
    term: str,
    *,
    i_understand: bool,
    confirm_term: str | None,
    stdin_fn: Callable[[], str] = sys.stdin.readline,
    isatty: bool | None = None,
) -> None:
    """Live drop/withdraw needs ``--i-understand`` and the exact term typed."""
    if not i_understand:
        raise DropSafetyError(
            "Live drop/withdraw requires --i-understand. Re-run without --yes for a dry run."
        )
    interactive = sys.stdin.isatty() if isatty is None else isatty
    if interactive:
        print(f"Type the exact term code ({term}) to confirm the live drop: ", end="", flush=True)
        typed = stdin_fn().strip()
        if typed != term:
            raise DropSafetyError(
                f"Confirmation failed: type the exact term code ({term}) to go live."
            )
        return
    if confirm_term != term:
        raise DropSafetyError(
            f"Non-interactive live drop requires --confirm-term {term}."
        )
