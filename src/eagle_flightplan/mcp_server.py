"""Eagle Flightplan MCP server (stdio) and shared tool implementation.

The module is both the MCP entry point (``eagle-flightplan-mcp`` / ``python -m
eagle_flightplan.mcp_server``) and the single implementation behind the CLI's read
commands. Every public ``*_payload`` function returns a JSON-serialisable dict
whose ``ok`` key is ``True`` on success; failures are converted to
``{"ok": False, "error": ..., "hint": ...}`` by the MCP wrappers (the CLI calls
the same functions and renders the same payloads).

Design rules:

* Domain modules are used as-is; this file only composes and normalises them.
* Nothing here writes to Banner registration. The only Banner writes are the
  Plan Ahead tools (create/add/remove/delete), which touch plan worksheets.
* Cached data always reports its provenance (``catalog.source`` /
  ``age_seconds``) so an agent can tell a live fetch from a cached snapshot.
"""

from __future__ import annotations

import json
import logging
import os
import re
import threading
from collections.abc import Callable, Sequence
from datetime import UTC, datetime
from typing import Any

import httpx
from mcp.server.mcpserver import MCPServer

from . import auth
from .audit import AuditClient, parse_audit
from .audit.models import (
    Audit,
    CourseRef,
    GroupKind,
    RequirementBlock,
    RequirementGroup,
    RequirementStatus,
)
from .banner.plan import (
    DuplicateCourseError,
    Plan,
    PlanAheadUnavailable,
    PlanClient,
    PlanError,
    PlanNotFoundError,
)
from .banner.search import (
    TermIndex,
    TermSnapshot,
    cache_age_seconds,
    fetch_term,
    load_snapshot,
)
from .banner.ssb import SSBClient, TermEligibility
from .catalog import (
    CourseKey,
    OfferingHistory,
    Priority,
    Requirement,
    applied_codes,
    applied_grades,
    availability_of,
    offering_history_attribute,
    offering_history_course,
    offering_matrix,
    requirements_from_audit,
    resolve,
    term_label,
)
from .config import REPO_ROOT, STATE_PATH, TERMS_DIR, get_settings, login_command
from .models import Section
from .planner import (
    Constraints,
    DraftPlan,
    Planner,
    PrereqProvider,
    RatingInfo,
    RatingProvider,
    verify_draft,
)
from .rmp import RMPError
from .schedule import (
    Weekday,
    conflict_summary,
    find_conflicts,
    grid_legend,
    grid_note,
    parse_banner_time,
    render_grid,
)

SERVER_NAME = "eagle-flightplan"
SERVER_VERSION = "0.1.0"

#: The term the student is currently enrolled in (used to distinguish "closed"
#: from "not open yet" in future terms). The planner uses the same default.
CURRENT_TERM = os.environ.get("UNW_CURRENT_TERM", "202610")

#: Exact command that repairs an expired/stale session in a fresh process.
#: The packaged CLI works for editable and wheel installs alike.
FIX_COMMAND = login_command()
FIX_COMMAND_ALT = "eagle-flightplan login --force"

# Keep per-request HTTP chatter off the operator's console; tool payloads carry
# everything the agent needs.
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("httpcore").setLevel(logging.WARNING)
logger = logging.getLogger("eagle_flightplan.mcp_server")

_DAY_ALIASES = {
    "m": "Mon",
    "mo": "Mon",
    "mon": "Mon",
    "monday": "Mon",
    "tu": "Tue",
    "tue": "Tue",
    "tues": "Tue",
    "tuesday": "Tue",
    "w": "Wed",
    "we": "Wed",
    "wed": "Wed",
    "weds": "Wed",
    "wednesday": "Wed",
    "th": "Thu",
    "thu": "Thu",
    "thur": "Thu",
    "thurs": "Thu",
    "thursday": "Thu",
    "f": "Fri",
    "fr": "Fri",
    "fri": "Fri",
    "friday": "Fri",
    "sa": "Sat",
    "sat": "Sat",
    "saturday": "Sat",
    "su": "Sun",
    "sun": "Sun",
    "sunday": "Sun",
}
_SEASON_ORDER = {"20": 0, "30": 1, "10": 2}
_TERM_ELIGIBILITY: dict[str, dict[str, Any]] = {}
# Banner's session holds one bound term. Serialise calls that bind it so two
# concurrent tool calls cannot read one term's sections under another term's key.
_BANNER_LOCK = threading.RLock()
_SEASON_LABEL = {"10": "Fall", "20": "Spring", "30": "Summer", "40": "Winter"}

#: Hard cap on one search page. A whole-term dump (800+ sections, ~1.2 MB) is too
#: large for a stdio MCP response, so callers paginate with limit/offset.
SEARCH_LIMIT_CAP = 200


# --------------------------------------------------------------------------- #
# small helpers
# --------------------------------------------------------------------------- #
def _number(value: Any) -> float | None:
    if value is None or value == "":
        return None
    try:
        return float(str(value).replace(",", "").strip())
    except (TypeError, ValueError):
        return None


def _int(value: Any) -> int | None:
    number = _number(value)
    return int(number) if number is not None else None


def _iso(value: datetime | None) -> str | None:
    return value.isoformat() if value is not None else None


def _utc_iso(timestamp: float) -> str:
    return datetime.fromtimestamp(timestamp, tz=UTC).isoformat()


def _age_human(seconds: float | None) -> str:
    if seconds is None:
        return "unknown"
    if seconds < 90:
        return f"{seconds:.0f}s"
    if seconds < 5400:
        return f"{seconds / 60:.0f} min"
    return f"{seconds / 3600:.1f} h"


def _norm_list(value: Sequence[str | int] | str | int | None) -> list[str]:
    if value is None:
        return []
    if isinstance(value, str):
        return [part.strip() for part in value.split(",") if part.strip()]
    if isinstance(value, int):
        return [str(value)]
    return [str(item).strip() for item in value if str(item).strip()]


def _norm_names(value: Sequence[str] | str) -> list[str]:
    """Professor names keep their comma: "Zderad, Jonathan" is one person."""
    if isinstance(value, str):
        return [value.strip()] if value.strip() else []
    return [str(item).strip() for item in value if str(item).strip()]


def _error(exc: Exception) -> dict[str, Any]:
    """Turn an exception into an actionable tool response (never a traceback)."""
    kind = type(exc).__name__
    hint = ""
    if isinstance(exc, auth.SessionExpiredError):
        hint = f"The Banner/Degree Works session is dead. Run: {FIX_COMMAND}"
    elif isinstance(exc, PlanError):
        hint = (
            "Banner rejected the plan operation. Re-read the plan list before retrying; "
            "never repeat a write after an HTTP 500 without checking unw_session."
        )
    elif isinstance(exc, auth.AuthError):
        hint = f"Authentication failed. Run: {FIX_COMMAND}"
    elif isinstance(exc, RMPError):
        hint = "Rate My Professors is optional: unknown ratings are null, not 0.0."
    elif isinstance(exc, FileNotFoundError):
        hint = "A required local file is missing; run `eagle-flightplan doctor`."
    elif isinstance(exc, httpx.HTTPError):
        hint = (
            "Network failure talking to UNW or RMP; retry once, then run "
            "`eagle-flightplan doctor`."
        )
    elif isinstance(exc, ValueError):
        hint = "Invalid argument; see the tool description for accepted values."
    return {"ok": False, "error": f"{kind}: {exc}", "error_type": kind, "hint": hint}


def _safe(fn: Callable[..., dict], /, *args: Any, **kwargs: Any) -> dict:
    try:
        return fn(*args, **kwargs)
    except Exception as exc:
        return _error(exc)


_HEADINGS = {"catalog prerequisites", "corequisites", "prerequisites", "restrictions"}


def _drop_headings(lines: Sequence[str]) -> list[str]:
    return [line for line in lines if line and line.lower() not in _HEADINGS]


# --------------------------------------------------------------------------- #
# normalisation
# --------------------------------------------------------------------------- #
def _meeting_payload(meeting) -> dict[str, Any]:
    return {
        "day": meeting.day.value,
        "start": meeting.start.strftime("%H:%M") if meeting.start else None,
        "end": meeting.end.strftime("%H:%M") if meeting.end else None,
        "location": meeting.location,
        "kind": meeting.kind,
        "scheduled": meeting.scheduled,
    }


def section_payload(section: Section, *, current_term: str | None = CURRENT_TERM) -> dict:
    """Normalised section row used by search/candidates/draft/conflicts."""
    state = availability_of(section, current_term)
    return {
        "crn": section.crn,
        "term": section.term,
        "term_label": term_label(section.term) if len(section.term) == 6 else section.term,
        "subject": section.subject,
        "number": section.number,
        "section": section.section,
        "code": section.code,
        "section_code": section.section_code,
        "title": section.title,
        "credits": section.credits,
        "credits_low": section.credits_low,
        "credits_high": section.credits_high,
        "planning_credits": section.planning_credits,
        "credit_range": section.credit_range,
        "variable_credits": section.credits_high is not None
        and section.credits_low is not None
        and section.credits_high != section.credits_low,
        "credits_semantics": (
            "credits=display high end; planning_credits=reserved (low end unless "
            "overridden; a 0-low range reserves the high end); credit_range=raw range"
        ),
        "part_of_term_note": (
            "Banner-native part-of-term code; '1' is full term, other codes are "
            "session-specific; labels are not mapped"
        ),
        "capacity": section.capacity,
        "enrollment": section.enrollment,
        "seats_available": section.seats_available,
        "wait_available": section.wait_available,
        "open": section.open,
        "availability": state.value,
        "days": section.day_label,
        "times": section.time_label,
        "unscheduled": section.unscheduled,
        "instructors": section.instructors,
        "faculty": [
            {"name": person.name, "email": person.email, "primary": person.primary}
            for person in section.faculty
        ],
        "meetings": [_meeting_payload(meeting) for meeting in section.meetings],
        "attributes": [
            {"code": attribute.code, "description": attribute.description}
            for attribute in section.attributes
        ],
        "attribute_codes": sorted(section.attribute_codes),
        "part_of_term": section.part_of_term,
        "campus": section.campus,
        "instructional_method": section.instructional_method,
        "schedule_type": section.schedule_type,
        "link_identifier": section.link_identifier,
        "is_linked": section.is_linked,
        "partial_tba": section.partial_tba,
    }


def rating_payload(info: RatingInfo) -> dict:
    """RMP evidence. ``rating`` stays ``None`` when unrated -- never 0.0."""
    return {
        "name": info.name,
        "matched_name": info.matched_name,
        "rated": info.rated,
        "rating": info.rating if info.rated else None,
        "difficulty": info.difficulty if info.rated else None,
        "num_ratings": info.num_ratings,
        "confidence": info.confidence,
        "source": info.source,
        "note": info.note,
        "weak_evidence": bool(info.rated and info.num_ratings < 3),
    }


def _plan_course_payload(course) -> dict:
    return {
        "id": course.id,
        "code": course.code,
        "title": course.title,
        "crn": course.crn,
        "section": course.section,
        "term": course.term,
        "credits": course.credits,
        "status": course.status,
    }


def plan_payload(plan: Plan) -> dict:
    return {
        "id": plan.id,
        "name": plan.name,
        "term": plan.term,
        "term_label": term_label(plan.term) if len(plan.term) == 6 else plan.term,
        "term_description": plan.term_description,
        "sequence_number": plan.sequence_number,
        "preferred": plan.preferred,
        "total_planned_credit_hours": plan.total_planned_credit_hours,
        "courses": [_plan_course_payload(course) for course in plan.courses],
    }


def _option_payload(option: CourseRef) -> dict:
    return {
        "code": option.code,
        "subject": option.subject,
        "number": option.number,
        "title": option.title,
        "credits": option.credits,
        "grade": option.grade,
        "status": option.status,
        "term": option.term_taken,
        "in_progress": option.in_progress,
        "transfer": option.transfer,
        "ap_credit": option.ap_credit,
        "satisfies_notes": option.satisfies_notes,
    }


def _block_payload(block: RequirementBlock) -> dict:
    return {
        "label": block.label,
        "status": block.status.value,
        "pick_n_of": block.pick_n_of,
        "notes": block.notes,
        "options": [_option_payload(option) for option in block.options],
    }


def _group_payload(group: RequirementGroup) -> dict:
    return {
        "id": group.id,
        "title": group.title,
        "kind": group.kind.value,
        "status": group.status.value,
        "required_credits": group.required_credits,
        "earned_credits": group.earned_credits,
        "remaining_credits": group.remaining_credits,
        "notes": group.notes,
        "blocks": [_block_payload(block) for block in group.blocks],
        "sub_groups": [_group_payload(sub) for sub in group.sub_groups],
    }


def _filter_group(group: RequirementGroup, needle: str) -> RequirementGroup | None:
    """Keep a group when it, a descendant or one of its blocks matches ``needle``."""
    if needle in group.title.lower() or needle in group.kind.value:
        return group
    sub_groups = [
        filtered
        for filtered in (_filter_group(sub, needle) for sub in group.sub_groups)
        if filtered is not None
    ]
    blocks = [block for block in group.blocks if needle in block.label.lower()]
    if not sub_groups and not blocks:
        return None
    return group.model_copy(update={"sub_groups": sub_groups, "blocks": blocks})


def _block_status_index(audit: Audit) -> dict[str, RequirementStatus]:
    index: dict[str, RequirementStatus] = {}

    def visit(group: RequirementGroup) -> None:
        for position, block in enumerate(group.blocks):
            index[f"{group.id}:{position}"] = block.status
        for child in group.sub_groups:
            visit(child)

    for group in audit.groups:
        visit(group)
    return index


def _requirement_payload(requirement: Requirement, status: RequirementStatus) -> dict:
    options = [target.code for target in requirement.targets]
    options += [f"ATTRIBUTE {code}" for code in requirement.attributes]
    options += [f"{subject} *" for subject in requirement.subject_wildcards]
    return {
        "id": requirement.id,
        "title": requirement.label,
        "group": requirement.parent_group,
        "kind": requirement.kind.value,
        "status": status.value,
        "priority": requirement.priority.value,
        "credits_remaining": requirement.remaining_credits,
        "min_credits": requirement.min_credits,
        "applied_credits": requirement.applied_credits,
        "pick_n_of": requirement.pick_n_of,
        "applied_count": requirement.applied_count,
        "remaining_picks": (
            max(0, requirement.pick_n_of - requirement.applied_count)
            if requirement.pick_n_of is not None
            else None
        ),
        "options": options,
        "attributes": requirement.attributes,
        "subject_wildcards": requirement.subject_wildcards,
        "major_specific": requirement.major_specific,
        "optional": requirement.optional,
        "requires_arrangement": requirement.requires_arrangement,
        "notes": requirement.notes,
    }


# --------------------------------------------------------------------------- #
# session / profile / audit
# --------------------------------------------------------------------------- #
def session_status_payload() -> dict:
    """Session health without ever reading cookie values."""
    cfg = get_settings()
    state_exists = STATE_PATH.exists()
    age: float | None = None
    state: dict[str, Any] = {}
    if state_exists:
        try:
            age = auth.state_age_seconds()
        except OSError:
            age = None
        try:
            state = auth.load_state()
        except auth.AuthError:
            state = {}
    cookies = state.get("cookies") or []
    expiring = [
        (float(cookie["expires"]), cookie.get("domain"))
        for cookie in cookies
        if isinstance(cookie.get("expires"), (int, float)) and cookie.get("expires", 0) > 0
    ]
    soonest = min(expiring, default=None)
    valid = auth.session_valid(cfg)
    degraded = state_exists and not valid
    payload: dict[str, Any] = {
        "ok": True,
        "authenticated": valid,
        "degraded": degraded,
        "state_file": str(STATE_PATH),
        "state_exists": state_exists,
        "state_age_seconds": round(age, 1) if age is not None else None,
        "state_age_human": _age_human(age),
        "cookie_count": len(cookies),
        "cookie_domains": sorted({cookie.get("domain", "") for cookie in cookies}),
        "session_cookies": sum(
            1 for cookie in cookies if not isinstance(cookie.get("expires"), (int, float))
            or cookie.get("expires", 0) <= 0
        ),
        "earliest_cookie_expiry": _utc_iso(soonest[0]) if soonest else None,
        "earliest_cookie_domain": soonest[1] if soonest else None,
        "fix_command": FIX_COMMAND,
        "fix_command_alt": FIX_COMMAND_ALT,
        "login_script": FIX_COMMAND,
        "read_hint": "try unw_profile/unw_terms once; read-only tools may still work",
        "write_hint": "run fix_command before any Banner plan write",
    }
    if valid:
        payload["message"] = "Banner and Degree Works sessions are live."
    elif degraded:
        payload["message"] = (
            "Cached state exists but the live session check failed. Read-only tools "
            "may still work: try unw_profile or unw_terms once before asking for a "
            "login. Run fix_command before any Banner plan write."
        )
    else:
        payload["message"] = (
            "No cached session: run fix_command before using any Banner tool. "
            "Do not improvise a login."
        )
    return payload


def _name_tokens(name: str) -> set[str]:
    return {token for token in re.split(r"[^a-z]+", name.lower()) if token}


def build_profile(payload: dict, student: dict | None = None) -> dict:
    """Build the student profile from raw ``/api/audit`` (+ optional identity)."""
    header = payload.get("auditHeader") or {}
    degree_info = payload.get("degreeInformation") or {}
    degree_rows = degree_info.get("degreeDataArray") or []
    degree_row = degree_rows[0] if degree_rows else {}
    goals = degree_info.get("goalArray") or []
    reports = {row.get("code"): row.get("value") for row in degree_info.get("reportArray") or []}

    def goal_values(code: str) -> list[str]:
        return [
            goal.get("valueLiteral") or goal.get("value") or ""
            for goal in goals
            if goal.get("code") == code
        ]

    advisors: list[dict[str, Any]] = []
    for goal in goals:
        if goal.get("code") == "ADVISOR" and goal.get("advisorName"):
            advisors.append(
                {
                    "name": goal["advisorName"],
                    "email": goal.get("advisorEmail"),
                    "source": "goalArray",
                }
            )
    if reports.get("ADVISOR"):
        known = [_name_tokens(advisor["name"]) for advisor in advisors]
        for name in str(reports["ADVISOR"]).split(","):
            name = name.strip()
            tokens = _name_tokens(name)
            if not name or not tokens:
                continue
            if any(tokens <= seen or seen <= tokens for seen in known):
                continue
            advisors.append({"name": name, "email": None, "source": "reportArray"})
            known.append(tokens)

    credits_earned = sum(
        value or 0
        for value in (
            _number(header.get("residentApplied")),
            _number(header.get("transferApplied")),
            _number(header.get("examAppliedCredits")),
        )
    )
    gpa = _number(header.get("degreeworksGpa"))
    if gpa is None:
        gpa = _number(header.get("studentSystemGpa"))
    catalog_codes = sorted(
        {
            str(value)
            for value in [degree_row.get("catalogYear")]
            + [goal.get("catalogYear") for goal in goals]
            if value
        }
    )
    identity = student or {}
    return {
        "ok": True,
        "name": header.get("studentName") or identity.get("name"),
        "id": header.get("studentId") or identity.get("id"),
        "classification": degree_row.get("studentLevelLiteral"),
        "classification_code": degree_row.get("studentLevel"),
        "degree": degree_row.get("degreeLiteral"),
        "school": degree_row.get("schoolLiteral"),
        "catalog_year": degree_row.get("catalogYearLit") or degree_row.get("catalogYear"),
        "catalog_year_code": degree_row.get("catalogYear"),
        "catalog_years": catalog_codes,
        "active_term": degree_row.get("activeTerm") or identity.get("active_term"),
        "active_term_label": degree_row.get("activeTermLiteral"),
        "majors": goal_values("MAJOR"),
        "minors": goal_values("MINOR"),
        "programs": goal_values("PROGRAM"),
        "college": goal_values("COLLEGE"),
        "advisors": advisors,
        "advisor_names": reports.get("ADVISOR"),
        "credits_earned": round(credits_earned, 2),
        "credits_in_progress": _number(header.get("residentAppliedInProgress")),
        "gpa": gpa,
        "percent_complete": _int(header.get("percentComplete")),
        "anticipated_graduation": reports.get("GRADTERMUG"),
        "academic_standing": reports.get("ACSTAND"),
        "holds": None,
        "holds_note": (
            "Holds are not exposed by the mapped Degree Works /api/audit payload or the "
            "Banner endpoints in docs/banner-api.md. null means UNKNOWN, not 'none'; "
            "check Self-Service > Holds manually."
        ),
        "source": "Degree Works /api/audit (auditHeader + degreeInformation)",
    }


def profile_payload() -> dict:
    client = AuditClient()
    student = client.student()
    payload = client.fetch()
    return build_profile(payload, student)


def audit_payload(group: str | None = None) -> dict:
    audit = AuditClient().audit()
    needle = (group or "").strip().lower()
    if needle:
        filtered = [
            candidate
            for candidate in (_filter_group(item, needle) for item in audit.groups)
            if candidate is not None
        ]
    else:
        filtered = audit.groups
    return {
        "ok": True,
        "student_name": audit.student_name,
        "student_id": audit.student_id,
        "catalog_year": audit.catalog_year,
        "majors": audit.majors,
        "minors": audit.minors,
        "gpa": audit.gpa,
        "credits_earned": audit.credits_earned,
        "credits_in_progress": audit.credits_in_progress,
        "filter": group,
        "group_count": len(filtered),
        "groups": [_group_payload(item) for item in filtered],
    }


def remaining_payload() -> dict:
    audit = AuditClient().audit()
    statuses = _block_status_index(audit)
    requirements = requirements_from_audit(audit)
    rows = [
        _requirement_payload(requirement, statuses.get(requirement.id, RequirementStatus.NOT_MET))
        for requirement in requirements
        if not requirement.satisfied
    ]
    order = {
        Priority.MAJOR_REQUIRED.value: 0,
        Priority.MAJOR_POOL.value: 1,
        Priority.MINOR_REQUIRED.value: 2,
        Priority.GEN_ED_REQUIRED.value: 3,
        Priority.GEN_ED_FILL.value: 4,
    }
    rows.sort(key=lambda row: (order.get(row["priority"], 9), row["group"], row["title"]))
    groups: dict[tuple[str, tuple[str, ...]], list[str]] = {}
    for row in rows:
        key = (row["title"].strip().lower(), tuple(row["options"]))
        groups.setdefault(key, []).append(row["id"])
    for row in rows:
        key = (row["title"].strip().lower(), tuple(row["options"]))
        row["shared_title_with"] = [other for other in groups[key] if other != row["id"]]
    known = round(
        sum(row["credits_remaining"] for row in rows if row["credits_remaining"] is not None),
        2,
    )
    unknown = [
        {
            "id": row["id"],
            "title": row["title"],
            "group": row["group"],
            "priority": row["priority"],
        }
        for row in rows
        if row["credits_remaining"] is None
    ]
    return {
        "ok": True,
        "student_name": audit.student_name,
        "count": len(rows),
        "known_credits_remaining": known,
        "unknown_credits_count": len(unknown),
        "unknown_credits_rows": unknown,
        "credits_note": (
            "known_credits_remaining sums only rows with a numeric credits_remaining; "
            "unknown_credits_rows need pick-N or option-level credit data."
        ),
        "requirements": rows,
    }


# --------------------------------------------------------------------------- #
# snapshots / search / courses
# --------------------------------------------------------------------------- #
def _snapshot(term: str, refresh: bool = False) -> TermSnapshot:
    with _BANNER_LOCK:
        return fetch_term(term, refresh=refresh)


def _catalog_meta(snapshot: TermSnapshot) -> dict:
    age = cache_age_seconds(snapshot.term)
    return {
        "term": snapshot.term,
        "label": term_label(snapshot.term) if len(snapshot.term) == 6 else snapshot.term,
        "source": snapshot.meta.source,
        "fetched_at": _iso(snapshot.meta.fetched_at),
        "age_seconds": round(age, 1) if age is not None else None,
        "age_human": _age_human(age),
        "sections": len(snapshot),
        "request_count": snapshot.meta.request_count,
    }


def search_payload(
    term: str,
    subject: str | None = None,
    number: str | int | None = None,
    keyword: str | None = None,
    attribute: str | None = None,
    instructor: str | None = None,
    open_only: bool = False,
    limit: int = 50,
    offset: int = 0,
    refresh: bool = False,
    include_ratings: bool = False,
) -> dict:
    snapshot = _snapshot(term, refresh)
    rows = snapshot.sections
    if subject:
        subject_upper = subject.strip().upper()
        rows = [row for row in rows if row.subject.upper() == subject_upper]
    if number is not None and str(number).strip():
        wanted = str(number).strip().upper()
        rows = [row for row in rows if row.number.upper() == wanted]
    if keyword:
        needle = keyword.strip().lower()
        rows = [
            row
            for row in rows
            if needle in row.title.lower()
            or needle in row.code.lower()
            or needle in row.subject_course.lower()
            or any(needle in person.name.lower() for person in row.faculty)
        ]
    if attribute:
        code = attribute.strip().upper()
        rows = [row for row in rows if code in row.attribute_codes]
    if instructor:
        needle = instructor.strip().lower()
        rows = [
            row
            for row in rows
            if any(needle in person.name.lower() for person in row.faculty)
        ]
    if open_only:
        rows = [row for row in rows if row.open]
    rows = sorted(rows, key=lambda row: (row.subject, row.number, row.section, row.crn))
    total = len(rows)
    offset = max(0, offset)
    requested_limit = max(0, limit)
    limit = min(requested_limit, SEARCH_LIMIT_CAP)
    page = rows[offset : offset + limit] if limit else []
    next_offset = (offset + len(page)) if limit else None
    payload: dict[str, Any] = {
        "ok": True,
        "term": term,
        "label": term_label(term) if len(term) == 6 else term,
        "filters": {
            "subject": subject,
            "number": str(number) if number is not None else None,
            "keyword": keyword,
            "attribute": attribute,
            "instructor": instructor,
            "open_only": open_only,
        },
        "total_matched": total,
        "offset": offset,
        "limit": limit,
        "requested_limit": requested_limit,
        "limit_capped": requested_limit > SEARCH_LIMIT_CAP,
        "next_offset": next_offset if next_offset is not None and next_offset < total else None,
        "count": len(page),
        "catalog": _catalog_meta(snapshot),
        "sections": [section_payload(row) for row in page],
        "note": (
            "Filtering is client-side: Banner ignores txt_attribute/txt_instructor/"
            "chk_open_only on this deployment, so this tool filters the term snapshot."
        ),
    }
    if include_ratings:
        provider = RatingProvider()
        ratings: dict[str, dict[str, Any]] = {}
        for name in sorted({person.name for row in page for person in row.faculty if person.name}):
            info = provider.get(name)
            ratings[name] = {
                "rated": info.rated,
                "rating": info.rating if info.rated else None,
                "difficulty": info.difficulty if info.rated else None,
                "num_ratings": info.num_ratings,
                "confidence": info.confidence,
                "weak_evidence": bool(info.rated and info.num_ratings < 3),
            }
        payload["ratings"] = ratings
    return payload


def course_payload(term: str, crn: str | int, refresh: bool = False) -> dict:
    crn = str(crn).strip()
    snapshot = _snapshot(term, refresh)
    section = snapshot.by_crn(crn)
    if section is None:
        raise ValueError(
            f"CRN {crn} is not in term {term} ({len(snapshot)} sections). "
            "Use unw_search with subject/number to find the right CRN."
        )
    ssb = SSBClient(auth.ensure())
    ratings = RatingProvider()
    faculty = []
    for person in section.faculty:
        faculty.append(
            {
                "name": person.name,
                "email": person.email,
                "primary": person.primary,
                "rmp": rating_payload(ratings.get(person.name)),
            }
        )
    from .planner import parse_prerequisites

    with _BANNER_LOCK:
        prerequisites = _drop_headings(ssb.prerequisites(crn, term))
        corequisites = _drop_headings(ssb.corequisites(crn, term))
        restrictions = _drop_headings(ssb.restrictions(crn, term))
        detail_attributes = ssb.attributes(crn, term)
        linked = ssb.linked_sections(crn, term)
        description = ssb.description(crn, term)
        enrollment_text = ssb.enrollment_text(crn, term)
    prereq_text = "\n".join(prerequisites)
    parsed = parse_prerequisites(prereq_text)
    attributes = [
        {"code": attribute.code, "description": attribute.description}
        for attribute in (detail_attributes or section.attributes)
    ]
    return {
        "ok": True,
        "term": term,
        "crn": section.crn,
        "course": section_payload(section),
        "detail": {
            "description": description,
            "prerequisites": prerequisites,
            "prerequisites_text": prereq_text,
            "prerequisite_codes": parsed.codes,
            "prerequisite_expression": parsed.tree,
            "corequisites": corequisites,
            "restrictions": restrictions,
            "attributes": attributes,
            "linked_sections": [
                {
                    "crn": item.crn,
                    "code": f"{item.subject} {item.number}".strip(),
                    "title": item.title,
                    "link_identifier": item.link_identifier,
                }
                for item in linked
            ],
            "enrollment_text": enrollment_text,
            "faculty": faculty,
        },
        "catalog": _catalog_meta(snapshot),
    }


def _compact_candidate_payload(section: Section) -> dict:
    """Small candidate row: enough to choose a section, no repeated detail."""
    full = section_payload(section)
    keys = (
        "crn", "code", "section", "title", "credits", "planning_credits",
        "credit_range", "seats_available", "open", "availability", "days",
        "times", "instructors", "attribute_codes", "part_of_term", "unscheduled",
    )
    return {key: full.get(key) for key in keys}


def candidates_payload(
    term: str,
    requirement: str | None = None,
    refresh: bool = False,
    open_only: bool = True,
    compact: bool = True,
    limit: int = 0,
) -> dict:
    snapshot = _snapshot(term, refresh)
    audit = AuditClient().audit()
    passed = applied_codes(audit)
    statuses = _block_status_index(audit)
    unmet = [item for item in requirements_from_audit(audit) if not item.satisfied]
    needle = (requirement or "").strip().lower()
    if needle:
        unmet = [
            item
            for item in unmet
            if item.id.lower() == needle
            or needle in item.label.lower()
            or needle in item.parent_group.lower()
            or needle in item.search_label.lower()
        ]
        if not unmet:
            raise ValueError(
                f"No unmet requirement matches {requirement!r}. "
                "Call unw_remaining() to see the requirement ids and titles."
            )
    rows = []
    for item in unmet:
        resolution = resolve(
            item,
            snapshot,
            exclude_codes=passed,
            open_only=open_only,
            current_term=CURRENT_TERM,
        )
        total = len(resolution.candidates)
        selected = resolution.candidates[: max(0, limit)] if limit else resolution.candidates
        rows.append(
            {
                "requirement": _requirement_payload(
                    item, statuses.get(item.id, RequirementStatus.NOT_MET)
                ),
                "found": resolution.found,
                "reason": resolution.reason,
                "attribute_hits": resolution.attribute_hits,
                "count": total,
                "returned": len(selected),
                "truncated": len(selected) < total,
                "candidates": [
                    _compact_candidate_payload(candidate)
                    if compact
                    else section_payload(candidate)
                    for candidate in selected
                ],
            }
        )
    return {
        "ok": True,
        "term": term,
        "label": term_label(term) if len(term) == 6 else term,
        "open_only": open_only,
        "compact": compact,
        "limit": max(0, limit),
        "requirement_count": len(rows),
        "catalog": _catalog_meta(snapshot),
        "requirements": rows,
    }


# --------------------------------------------------------------------------- #
# ratings / conflicts
# --------------------------------------------------------------------------- #
def ratings_payload(names: Sequence[str] | str, refresh: bool = False) -> dict:
    wanted = _norm_names(names)
    if not wanted:
        raise ValueError("Pass at least one professor name.")
    provider = RatingProvider()
    rows = []
    for name in wanted:
        info = provider.get(name, refresh=refresh)
        row = rating_payload(info)
        row["query"] = name
        rows.append(row)
    return {
        "ok": True,
        "count": len(rows),
        "refresh": refresh,
        "ratings": rows,
        "note": (
            "Unrated is null, not 0.0. n<3 ratings are weak evidence; match confidence "
            "reflects name ambiguity, not teaching quality. The cache expires after 7 days."
        ),
    }


def _planned_credits(section: Section, hours: dict[str, float] | None = None) -> float:
    """One credit basis for plans, previews and conflict totals."""
    override = (hours or {}).get(str(section.crn))
    if override is not None:
        return float(override)
    return float(section.planning_credits or 0.0)


def conflicts_payload(
    term: str,
    crns: Sequence[str | int] | str | int,
    refresh: bool = False,
    hours: dict[str, float] | None = None,
) -> dict:
    wanted = [str(item).strip() for item in _norm_list(crns)]
    if not wanted:
        raise ValueError("Pass at least one CRN.")
    snapshot = _snapshot(term, refresh)
    found = []
    missing = []
    for crn in wanted:
        section = snapshot.by_crn(crn)
        if section is None:
            missing.append(crn)
        else:
            found.append(section)
    schedule_sections = [section.to_schedule_section() for section in found]
    conflicts = find_conflicts(schedule_sections)
    overrides = {str(crn): float(value) for crn, value in (hours or {}).items()}
    return {
        "ok": True,
        "term": term,
        "crns": wanted,
        "resolved": [section.crn for section in found],
        "missing_crns": missing,
        "conflict_count": len(conflicts),
        "conflicts": [
            {
                "day": conflict.day.value,
                "start": conflict.start.strftime("%H:%M"),
                "end": conflict.end.strftime("%H:%M"),
                "minutes": conflict.minutes,
                "crn_a": conflict.crn_a,
                "crn_b": conflict.crn_b,
                "title_a": conflict.title_a,
                "title_b": conflict.title_b,
                "part_of_term_a": conflict.part_of_term_a,
                "part_of_term_b": conflict.part_of_term_b,
                "kind": conflict.kind,
                "summary": conflict.summary,
            }
            for conflict in conflicts
        ],
        "conflict_summary": conflict_summary(conflicts),
        "hours": overrides,
        "total_credits": round(sum(_planned_credits(section, overrides) for section in found), 2),
        "grid": render_grid(schedule_sections, include_tba=True) if found else "",
        "grid_note": grid_note(schedule_sections),
        "grid_legend": grid_legend(schedule_sections) if found else {},
        "unscheduled": [
            section_payload(section) for section in found if section.unscheduled
        ],
        "partial_tba_crns": [section.crn for section in found if section.partial_tba],
        "partial_tba": [
            section_payload(section) for section in found if section.partial_tba
        ],
        "catalog": _catalog_meta(snapshot),
    }


# --------------------------------------------------------------------------- #
# planner
# --------------------------------------------------------------------------- #
def _parse_day(value: str) -> Weekday:
    key = value.strip().lower()
    if key in _DAY_ALIASES:
        return Weekday(_DAY_ALIASES[key])
    raise ValueError(
        f"Unknown day {value!r}; use Mon/Tue/Wed/Thu/Fri/Sat/Sun (or full names)."
    )


def _parse_clock(value: str | None) -> Any:
    if value is None or not str(value).strip():
        return None
    parsed = parse_banner_time(value)
    if parsed is None:
        raise ValueError(f"Invalid time {value!r}; use HH:MM (24h) or Banner HHMM.")
    return parsed


def build_constraints(
    max_credits: float = 16,
    avoid_days: Sequence[str] | str | None = None,
    earliest: str | None = None,
    latest: str | None = None,
    *,
    min_credits: float = 0,
    avoid_instructors: Sequence[str] | str | None = None,
    exclude_arrangement: bool = False,
    required_crns: Sequence[str | int] | str | None = None,
    required_courses: Sequence[str] | str | None = None,
    excluded_crns: Sequence[str | int] | str | None = None,
    excluded_courses: Sequence[str] | str | None = None,
    credit_overrides: dict[str, float] | None = None,
) -> Constraints:
    if max_credits <= 0:
        raise ValueError("max_credits must be greater than 0.")
    if min_credits < 0:
        raise ValueError("min_credits must be 0 or greater.")
    days = [_parse_day(day) for day in _norm_list(avoid_days)]
    instructors = _norm_names(avoid_instructors) if avoid_instructors is not None else []
    overrides: dict[str, float] = {}
    for crn, hours in (credit_overrides or {}).items():
        key = str(crn).strip()
        if not key:
            continue
        value = float(hours)
        if value < 0:
            raise ValueError(f"hours for CRN {key} must be 0 or greater.")
        overrides[key] = value
    return Constraints(
        max_credits=float(max_credits),
        min_credits=float(min_credits),
        avoid_days=days,
        earliest=_parse_clock(earliest),
        latest=_parse_clock(latest),
        avoid_instructors=instructors,
        exclude_arrangement=exclude_arrangement,
        required_crns=_norm_list(required_crns),
        required_courses=_norm_list(required_courses),
        excluded_crns=_norm_list(excluded_crns),
        excluded_courses=_norm_list(excluded_courses),
        credit_overrides=overrides,
    )


def _active_term(raw: dict) -> str:
    """The student's active term from the audit, falling back to the configured one."""
    degree = raw.get("degreeInformation") or {}
    for row in degree.get("degreeDataArray") or []:
        candidate = str((row or {}).get("activeTerm") or "").strip()
        if len(candidate) == 6 and candidate.isdigit():
            return candidate
    return CURRENT_TERM


def _planner(
    terms: Sequence[str], refresh: bool = False
) -> tuple[Planner, dict[str, TermSnapshot], Audit, dict, str]:
    client = AuditClient()
    client.student()
    raw = client.fetch()
    audit = parse_audit(raw)
    snapshots: dict[str, TermSnapshot] = {}
    for term in terms:
        snapshots[term] = _snapshot(term, refresh)
    current_term = _active_term(raw)
    planner = Planner(
        audit,
        snapshots,
        ratings=RatingProvider(),
        prereqs=PrereqProvider(),
        current_term=current_term,
    )
    return planner, snapshots, audit, raw, current_term


def _draft_selection_payload(selection, section: Section | None = None) -> dict:
    return {
        "crn": selection.crn,
        "course": selection.course,
        "section": selection.section,
        "title": selection.title,
        "instructor": selection.instructor,
        "rating": rating_payload(selection.rating),
        "days": selection.days,
        "times": selection.times,
        "credits": selection.credits,
        "credit_range": section.credit_range if section is not None else None,
        "credits_low": section.credits_low if section is not None else None,
        "credits_high": section.credits_high if section is not None else None,
        "seats": selection.seats,
        "open": selection.open,
        "availability": selection.availability,
        "verify_at_registration": selection.verify_at_registration,
        "prereq_unverified": selection.prereq_unverified,
        "partial_tba": selection.partial_tba,
        "requires_arrangement": selection.requires_arrangement,
        "requirement_id": selection.requirement_id,
        "requirement": selection.requirement,
        "group": selection.group,
        "covers": selection.covers,
        "meets": selection.meets,
        "rationale": selection.rationale,
    }


def _draft_payload(draft: DraftPlan, snapshot: TermSnapshot | None) -> dict:
    rated = [row.rating for row in draft.selections if row.rating.rated]
    average = round(sum(row.rating or 0 for row in rated) / len(rated), 2) if rated else None
    selections = []
    schedule_sections = []
    for selection in draft.selections:
        section = snapshot.by_crn(selection.crn) if snapshot is not None else None
        selections.append(_draft_selection_payload(selection, section))
        if section is not None:
            schedule_sections.append(section.to_schedule_section())
    payload = {
        "term": draft.term,
        "label": draft.label or term_label(draft.term),
        "total_credits": draft.total_credits,
        "min_credits": draft.min_credits,
        "min_credits_met": draft.min_credits_met,
        "selection_count": len(draft.selections),
        "selections": selections,
        "pinned": [pin.model_dump() for pin in draft.pinned],
        "pin_errors": list(draft.errors),
        "credit_overrides": dict(draft.credit_overrides),
        "unplaced": [
            {
                "requirement_id": item.requirement_id,
                "requirement": item.requirement,
                "group": item.group,
                "reason": item.reason,
            }
            for item in draft.unplaced_requirements
        ],
        "warnings": list(draft.warnings),
        "grid": draft.grid,
        "grid_note": grid_note(schedule_sections),
        "grid_legend": grid_legend(schedule_sections) if schedule_sections else {},
        "score_breakdown": list(draft.score_breakdown),
        "avg_rmp_rating": average,
        "rated_picks": len(rated),
        "unrated_picks": sum(1 for row in draft.selections if not row.rating.rated),
    }
    if snapshot is not None:
        problems = verify_draft(draft, snapshot)
        payload["verify_problems"] = problems
        payload["verified"] = not problems
        payload["verified_scope"] = (
            "internal consistency: every CRN is in the snapshot, open sections have seats, "
            "credits add up, no time conflicts, prerequisites are known, requirements placed, "
            "pins resolved. It is not a registration guarantee."
        )
    return payload


def draft_payload(
    term: str,
    max_credits: float = 16,
    profile: str = "max_rated",
    avoid_days: Sequence[str] | str | None = None,
    earliest: str | None = None,
    latest: str | None = None,
    include_summer: bool = False,
    refresh: bool = False,
    min_credits: float = 0,
    required_crns: Sequence[str | int] | str | None = None,
    required_courses: Sequence[str] | str | None = None,
    exclude_crns: Sequence[str | int] | str | None = None,
    exclude_courses: Sequence[str] | str | None = None,
    avoid_instructors: Sequence[str] | str | None = None,
    exclude_arrangement: bool = False,
    hours: dict[str, float] | None = None,
) -> dict:
    if profile not in {"max_rated"}:
        raise ValueError(
            f"Unknown profile {profile!r}; only 'max_rated' is implemented "
            "(the planner's ranking maximises RMP rating with unrated last)."
        )
    constraints = build_constraints(
        max_credits,
        avoid_days,
        earliest,
        latest,
        min_credits=min_credits,
        avoid_instructors=avoid_instructors,
        exclude_arrangement=exclude_arrangement,
        required_crns=required_crns,
        required_courses=required_courses,
        excluded_crns=exclude_crns,
        excluded_courses=exclude_courses,
        credit_overrides=hours,
    )
    terms = [term]
    summer = _next_summer(term) if include_summer else None
    if summer:
        terms.append(summer)
    planner, snapshots, audit, _raw, current_term = _planner(terms, refresh)
    with _BANNER_LOCK:
        draft = planner.draft(term, constraints)
    payload: dict[str, Any] = {
        "ok": True,
        "requested": {
            "term": term,
            "max_credits": max_credits,
            "min_credits": min_credits,
            "profile": profile,
            "avoid_days": [day.value for day in constraints.avoid_days],
            "earliest": constraints.earliest.strftime("%H:%M") if constraints.earliest else None,
            "latest": constraints.latest.strftime("%H:%M") if constraints.latest else None,
            "include_summer": include_summer,
            "required_crns": list(constraints.required_crns),
            "required_courses": list(constraints.required_courses),
            "excluded_crns": list(constraints.excluded_crns),
            "excluded_courses": list(constraints.excluded_courses),
            "avoid_instructors": list(constraints.avoid_instructors),
            "exclude_arrangement": constraints.exclude_arrangement,
            "hours": dict(constraints.credit_overrides),
        },
        "current_term": current_term,
        "student": audit.student_name,
        "catalog": _catalog_meta(snapshots[term]),
        "draft": _draft_payload(draft, snapshots[term]),
    }
    if draft.errors:
        payload["ok"] = False
        payload["error"] = "pinned course(s) could not be placed: " + "; ".join(draft.errors)
        payload["error_type"] = "PinError"
        payload["hint"] = (
            "Adjust the pinned CRN/course, the term, or the exclusions; the partial "
            "draft is still included under 'draft'."
        )
    if summer and summer in snapshots:
        placed = set()
        for selection in draft.selections:
            placed.add(selection.requirement_id)
            placed.update(selection.covers)
        remaining = [item for item in planner.requirements if item.id not in placed]
        # A spring section satisfies a summer prerequisite even though it is not
        # completed yet; carry the planned codes forward explicitly.
        spring_passed = set(planner.passed)
        spring_passed.update(selection.course.upper() for selection in draft.selections)
        # Pins and the minimum load describe the requested term. The summer pass
        # only picks up requirements that did not fit, so it must not re-place a
        # pinned course or force the same credit load.
        summer_constraints = constraints.model_copy(
            update={"required_crns": [], "required_courses": [], "min_credits": 0}
        )
        with _BANNER_LOCK:
            summer_draft = planner.draft(
                summer, summer_constraints, requirements=remaining, passed=spring_passed
            )
        payload["summer"] = {
            "catalog": _catalog_meta(snapshots[summer]),
            "draft": _draft_payload(summer_draft, snapshots[summer]),
        }
    return payload


def _semester_selection(row: dict[str, Any]) -> dict[str, Any]:
    """Compact one-call selection row: no repeated section dict."""
    rating = row.get("rating") or {}
    return {
        "crn": row.get("crn"),
        "course": row.get("course"),
        "title": row.get("title"),
        "instructor": row.get("instructor"),
        "rating": {
            "rated": rating.get("rated"),
            "rating": rating.get("rating"),
            "difficulty": rating.get("difficulty"),
            "num_ratings": rating.get("num_ratings"),
            "weak_evidence": rating.get("weak_evidence"),
        },
        "days": row.get("days"),
        "times": row.get("times"),
        "credits": row.get("credits"),
        "credit_range": row.get("credit_range"),
        "seats": row.get("seats"),
        "open": row.get("open"),
        "availability": row.get("availability"),
        "covers": row.get("covers") or [],
        "rationale": row.get("rationale"),
        "prereq_unverified": row.get("prereq_unverified"),
        "partial_tba": row.get("partial_tba"),
        "requires_arrangement": row.get("requires_arrangement"),
    }


def _parse_window_stamp(value: Any) -> datetime | None:
    """Parse an ISO window stamp from the registration payload."""
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(str(value))
    except ValueError:
        return None
    return parsed if parsed.tzinfo is not None else parsed.replace(tzinfo=UTC)


def _merge_required_courses(
    required_courses: Sequence[str] | str | None,
    pins: Sequence[str] | str | None,
    courses: Sequence[str] | str | None,
) -> list[str] | None:
    merged: list[str] = []
    for value in (required_courses, pins, courses):
        for item in _norm_list(value):
            if item not in merged:
                merged.append(item)
    return merged or None


def _semester_next_action(
    plan: dict[str, Any],
    registration: dict[str, Any],
    conflict_count: int,
    warnings: Sequence[str],
    plans: dict[str, Any],
    *,
    term: str,
    crns: Sequence[str],
    tba_crns: Sequence[str],
    prereq_unverified_crns: Sequence[str],
    now: datetime | None = None,
) -> str:
    crn_arg = "[" + ", ".join(json.dumps(str(crn)) for crn in crns) + "]"
    now = now or datetime.now(UTC)
    start = _parse_window_stamp(registration.get("start"))
    end = _parse_window_stamp(registration.get("end"))
    plan_bits = []
    for item in plans.get("plans") or []:
        bits = []
        if item.get("id") is not None:
            bits.append(str(item["id"]))
        if item.get("name"):
            bits.append(repr(item["name"]))
        if item.get("credits") is not None:
            bits.append(f"{item['credits']:g} cr")
        if bits:
            plan_bits.append(" ".join(bits))
    plan_note = (
        " Existing Banner plans: " + "; ".join(plan_bits) + "."
        if plan_bits
        else ""
    )

    if plan.get("pin_errors"):
        primary = "Fix the pinned-course error(s) first: " + "; ".join(
            str(item) for item in (plan.get("pin_errors") or [])[:2]
        )
    elif not crns:
        primary = (
            "No sections fit this term under the current constraints. Raise "
            "max_credits or relax avoid_days/earliest/latest/exclude_* and re-run."
        )
    elif end is not None and now > end:
        primary = (
            f"The {term} registration window closed on {end.isoformat()}. "
            "Re-check the ticket with unw_registration_window before any retry; "
            "do not attempt to register."
        )
    elif start is not None and now < start:
        primary = (
            f"Ticket assigned; opens {start.isoformat()}. "
            f"Run unw_register_preview(term={term}, crns={crn_arg}) now, "
            "then submit after opening."
        )
    elif (
        start is not None
        and end is not None
        and start <= now <= end
        and not registration.get("eligible")
    ):
        primary = (
            f"The window is open until {end.isoformat()} but Banner reports the term "
            f"ineligible ({registration.get('reason') or 'no reason text'}). Resolve "
            "the hold/ticket with the registrar; do not attempt to register yet."
        )
    elif registration.get("eligible"):
        primary = (
            f"Run unw_register_preview(term={term}, crns={crn_arg}), "
            "then plan writes with confirm=true or snipe."
        )
    else:
        primary = (
            "Confirm the registration ticket first, then run "
            f"unw_register_preview(term={term}, crns={crn_arg})."
        )

    extras: list[str] = []
    if plan.get("verified") is False and plan.get("verify_problems"):
        extras.append(
            "Verification: " + "; ".join(
                str(item) for item in (plan.get("verify_problems") or [])[:2]
            )
        )
    if conflict_count:
        extras.append(f"Resolve {conflict_count} time conflict(s).")
    if tba_crns:
        extras.append(
            f"TBA/async or partially-TBA CRNs {', '.join(tba_crns)} cannot be fully "
            "time-conflict checked; verify their times."
        )
    if prereq_unverified_crns:
        extras.append(
            f"Prerequisite unverified for CRNs {', '.join(prereq_unverified_crns)}; "
            "confirm before registering."
        )
    if plan.get("min_credits") and not plan.get("min_credits_met"):
        extras.append(
            f"Plan is {plan.get('total_credits')} cr; below the "
            f"{plan.get('min_credits')} cr minimum."
        )
    variable = next(
        (warning for warning in warnings if "variable credit" in warning.lower()),
        None,
    )
    if variable:
        extras.append(variable)
    arranged = next(
        (warning for warning in warnings if "arranged faculty sponsor" in warning.lower()),
        None,
    )
    if arranged:
        extras.append(arranged)
    return primary + plan_note + ((" " + " ".join(extras)) if extras else "")


def _semester_plan_payload_unlocked(
    term: str,
    max_credits: float = 16,
    profile: str = "max_rated",
    avoid_days: Sequence[str] | str | None = None,
    earliest: str | None = None,
    latest: str | None = None,
    include_summer: bool = False,
    refresh: bool = False,
    min_credits: float = 0,
    required_crns: Sequence[str | int] | str | None = None,
    required_courses: Sequence[str] | str | None = None,
    exclude_crns: Sequence[str | int] | str | None = None,
    exclude_courses: Sequence[str] | str | None = None,
    avoid_instructors: Sequence[str] | str | None = None,
    exclude_arrangement: bool = False,
    hours: dict[str, float] | None = None,
    credits: float | None = None,
    pins: Sequence[str] | str | None = None,
    courses: Sequence[str] | str | None = None,
    include_banner_plans: bool = True,
) -> dict:
    """One-call semester report: draft + conflicts + ticket + Banner plans.

    Canonical constraints are ``max_credits`` and ``required_courses``.
    ``credits`` is an alias for ``max_credits``; ``pins`` and ``courses`` append
    to ``required_courses``. The draft is never hidden by a window/plan failure;
    those errors are returned in their own fields. This is read-only.
    """
    if credits is not None:
        max_credits = credits
    merged_courses = _merge_required_courses(required_courses, pins, courses)
    draft = draft_payload(
        term=term,
        max_credits=max_credits,
        profile=profile,
        avoid_days=avoid_days,
        earliest=earliest,
        latest=latest,
        include_summer=include_summer,
        refresh=refresh,
        min_credits=min_credits,
        required_crns=required_crns,
        required_courses=merged_courses,
        exclude_crns=exclude_crns,
        exclude_courses=exclude_courses,
        avoid_instructors=avoid_instructors,
        exclude_arrangement=exclude_arrangement,
        hours=hours,
    )
    requested = draft.get("requested") or {}
    hours_map = {
        str(crn): float(value)
        for crn, value in (requested.get("hours") or hours or {}).items()
    }
    body = draft.get("draft") or {}
    selections = body.get("selections") or []
    selection_rows = [_semester_selection(row) for row in selections]
    crns = [str(row.get("crn")) for row in selections if row.get("crn")]

    conflict_data: dict[str, Any]
    if crns:
        try:
            raw_conflicts = conflicts_payload(term, crns, refresh=False, hours=hours_map)
            conflict_data = {
                "count": raw_conflicts.get("conflict_count", 0),
                "summary": raw_conflicts.get("conflict_summary", ""),
                "total_credits": raw_conflicts.get("total_credits"),
                "grid_note": raw_conflicts.get("grid_note", ""),
                "items": [
                    {
                        "day": row.get("day"),
                        "start": row.get("start"),
                        "end": row.get("end"),
                        "crn_a": row.get("crn_a"),
                        "crn_b": row.get("crn_b"),
                        "title_a": row.get("title_a"),
                        "title_b": row.get("title_b"),
                        "part_of_term_a": row.get("part_of_term_a"),
                        "part_of_term_b": row.get("part_of_term_b"),
                    }
                    for row in raw_conflicts.get("conflicts", [])
                ],
                "unscheduled_crns": [
                    row.get("crn") for row in raw_conflicts.get("unscheduled", [])
                ],
                "partial_tba_crns": list(raw_conflicts.get("partial_tba_crns") or []),
            }
        except Exception as exc:
            conflict_data = {
                "count": None,
                "summary": "",
                "items": [],
                "error": f"{type(exc).__name__}: {exc}",
            }
    else:
        conflict_data = {"count": 0, "summary": "no selections", "items": []}

    warnings = list(body.get("warnings") or [])
    tba_crns = [str(crn) for crn in (conflict_data.get("unscheduled_crns") or [])]
    partial_tba_crns = [
        str(crn) for crn in (conflict_data.get("partial_tba_crns") or [])
    ]
    if not tba_crns and not partial_tba_crns:
        tba_crns = [
            str(row.get("crn"))
            for row in selections
            if str(row.get("times") or "").upper() == "TBA"
        ]
    all_tba = list(dict.fromkeys(tba_crns + partial_tba_crns))
    tba_set = set(all_tba)
    tba_sections = [
        {
            "crn": row.get("crn"),
            "course": row.get("course"),
            "title": row.get("title"),
            "partial_tba": str(row.get("crn")) in set(partial_tba_crns),
        }
        for row in selection_rows
        if str(row.get("crn")) in tba_set
    ]
    if all_tba:
        tba_warning = (
            f"TBA/async or partially-TBA sections cannot be fully time-conflict "
            f"checked ({', '.join(all_tba)}); verify their times."
        )
        if tba_warning not in warnings:
            warnings.append(tba_warning)

    # A pinned-section warning can say "prerequisite could not be verified" while
    # the compact row still says false. Treat the warning as authoritative so the
    # agent never sees an unexplained contradiction.
    for warning in warnings:
        if "prerequisite could not be verified" not in warning.lower():
            continue
        for row in selection_rows:
            crn = str(row.get("crn") or "")
            if crn and crn in warning:
                row["prereq_unverified"] = True

    prereq_rows = [row for row in selection_rows if row.get("prereq_unverified")]
    prereq_unverified_crns = [str(row.get("crn")) for row in prereq_rows if row.get("crn")]
    for row in prereq_rows:
        message = (
            f"{row.get('course')} CRN {row.get('crn')}: prerequisite could not be "
            "verified; confirm before registering"
        )
        if not any(
            str(row.get("crn")) in item and "prerequisite" in item.lower()
            for item in warnings
        ):
            warnings.append(message)

    try:
        raw_window = registration_window_payload(term)
        windows = raw_window.get("windows") or []
        default_row = windows[0] if windows else {}
        row = next(
            (item for item in windows if item.get("term") == term),
            default_row,
        )
        registration = {
            "term": term,
            "eligible": row.get("eligible"),
            "start": row.get("start"),
            "end": row.get("end"),
            "source": row.get("source"),
            "standing": row.get("standing"),
            "registration_standing": row.get("registration_standing"),
            "countdown": row.get("countdown"),
            "parse_status": row.get("parse_status"),
            "reason": row.get("reason"),
            "standing_note": row.get("standing_note"),
        }
        if not windows:
            registration["error"] = raw_window.get("error") or "no window rows returned"
    except Exception as exc:
        registration = {
            "term": term,
            "error": f"{type(exc).__name__}: {exc}",
        }

    if include_banner_plans:
        try:
            raw_plans = plans_payload(term)
            plans: dict[str, Any] = {
                "count": raw_plans.get("count", 0),
                "plan_ahead_available": raw_plans.get("plan_ahead_available"),
                "plans": [
                    {
                        "id": plan.get("id"),
                        "name": plan.get("name"),
                        "credits": plan.get("total_planned_credit_hours"),
                        "course_count": len(plan.get("courses") or []),
                    }
                    for plan in raw_plans.get("plans", [])
                ],
            }
            if not raw_plans.get("ok", True):
                plans["error"] = raw_plans.get("error") or raw_plans.get("note")
        except Exception as exc:
            plans = {"count": None, "error": f"{type(exc).__name__}: {exc}"}
    else:
        plans = {"skipped": True, "count": None}

    pinned = []
    for pin in body.get("pinned", []):
        item = dict(pin)
        pin_crn = str(item.get("crn") or "")
        item["hours_applied"] = hours_map.get(pin_crn) if pin_crn else None
        pinned.append(item)

    plan_summary = {
        "total_credits": body.get("total_credits"),
        "max_credits": requested.get("max_credits", max_credits),
        "min_credits": body.get("min_credits"),
        "min_credits_met": body.get("min_credits_met"),
        "hours": hours_map,
        "selection_count": body.get("selection_count"),
        "verified": body.get("verified"),
        "verified_scope": body.get("verified_scope"),
        "verify_problems": body.get("verify_problems", []),
        "avg_rmp_rating": body.get("avg_rmp_rating"),
        "rated_picks": body.get("rated_picks"),
        "unrated_picks": body.get("unrated_picks"),
        "requested": requested,
        "catalog": draft.get("catalog"),
    }
    result: dict[str, Any] = {
        "ok": bool(draft.get("ok", True)),
        "tool": "unw_semester_plan",
        "term": term,
        "label": draft.get("label") or body.get("label") or (
            term_label(term) if len(term) == 6 else term
        ),
        "student": draft.get("student"),
        "current_term": draft.get("current_term"),
        "aliases": {
            "credits": credits,
            "pins": _norm_list(pins),
            "courses": _norm_list(courses),
        },
        "hours": hours_map,
        "plan": plan_summary,
        "selections": selection_rows,
        "tba_crns": all_tba,
        "tba_sections": tba_sections,
        "prereq_unverified_crns": prereq_unverified_crns,
        "prereq_unverified_sections": [
            {"crn": row.get("crn"), "course": row.get("course")} for row in prereq_rows
        ],
        "grid": body.get("grid", ""),
        "grid_note": body.get("grid_note", ""),
        "grid_legend": body.get("grid_legend", {}),
        "partial_tba_crns": partial_tba_crns,
        "pinned": pinned,
        "pin_errors": body.get("pin_errors", []),
        "unplaced": body.get("unplaced", []),
        "warnings": warnings,
        "conflicts": conflict_data,
        "registration": registration,
        "banner_plans": plans,
    }
    if draft.get("error"):
        result["error"] = draft["error"]
    if draft.get("hint"):
        result["hint"] = draft["hint"]
    if "summer" in draft:
        summer = draft["summer"] or {}
        summer_body = summer.get("draft") or {}
        result["summer_summary"] = {
            "total_credits": summer_body.get("total_credits"),
            "selection_count": summer_body.get("selection_count"),
        }
    result["next_action"] = _semester_next_action(
        {
            "total_credits": plan_summary["total_credits"],
            "min_credits": plan_summary["min_credits"],
            "min_credits_met": plan_summary["min_credits_met"],
            "verified": plan_summary["verified"],
            "verify_problems": plan_summary["verify_problems"],
            "pin_errors": result["pin_errors"],
        },
        registration,
        int(conflict_data.get("count") or 0),
        warnings,
        plans,
        term=term,
        crns=crns,
        tba_crns=all_tba,
        prereq_unverified_crns=prereq_unverified_crns,
    )
    result["note"] = (
        "Read-only one-semester report. It never registers/adds/drops. Canonical "
        "constraints are max_credits and required_courses; credits/pins/courses "
        "are aliases. The draft survives a ticket or Banner-plan failure; inspect "
        "registration.error/banner_plans.error when present."
    )
    return result


def semester_plan_payload(*args: Any, **kwargs: Any) -> dict:
    """Serialize the whole one-shot Banner sequence behind ``_BANNER_LOCK``.

    Draft, conflicts, registration window and Banner plans each bind/read the
    session term. Holding the existing RLock around the entire sequence prevents
    concurrent one-shot calls from interleaving term bindings; the nested calls
    re-acquire the same RLock. There are no writes in this sequence.
    """
    with _BANNER_LOCK:
        return _semester_plan_payload_unlocked(*args, **kwargs)


def _term_key(term: str) -> tuple[int, int]:
    if len(term) < 6 or not term.isdigit():
        return (9999, 9)
    season = _SEASON_LABEL.get(term[4:6], "Unknown")
    year = int(term[:4]) + (0 if season == "Fall" else 1)
    return (year, _SEASON_ORDER.get(term[4:6], 3))


def _next_summer(term: str) -> str:
    if len(term) != 6 or not term.isdigit():
        raise ValueError(f"Invalid term code {term!r}; expected YYYYSS (e.g. 202620).")
    if term[4:6] == "30":
        return f"{int(term[:4]) + 1}30"
    return f"{term[:4]}30"


def _term_sequence(start_term: str, count: int, include_summer: bool) -> list[str]:
    if len(start_term) != 6 or not start_term.isdigit():
        raise ValueError(f"Invalid term code {start_term!r}; expected YYYYSS (e.g. 202620).")
    start_year = int(start_term[:4])
    candidates: list[str] = []
    for year in range(start_year - 1, start_year + 5):
        for season in ("20", "30", "10"):
            candidates.append(f"{year}{season}")
    start_key = _term_key(start_term)
    upcoming = sorted(
        {
            code
            for code in candidates
            if _term_key(code) > start_key and (include_summer or code[4:6] != "30")
        },
        key=_term_key,
    )
    head = [start_term] if include_summer or start_term[4:6] != "30" else []
    return (head + upcoming)[:count]


def roadmap_payload(
    start_term: str,
    terms: Sequence[str] | str | None = None,
    include_summer: bool = False,
    max_credits: float = 16,
    refresh: bool = False,
) -> dict:
    wanted = _norm_list(terms)
    planned_terms = wanted or _term_sequence(start_term, 4, include_summer)
    constraints = build_constraints(max_credits)
    planner, snapshots, audit, raw, current_term = _planner(planned_terms, refresh)
    catalogs = [_catalog_meta(snapshot) for snapshot in snapshots.values()]
    with _BANNER_LOCK:
        roadmap = planner.roadmap(planned_terms, constraints)
    rows = []
    for entry in roadmap.terms:
        row: dict[str, Any] = {
            "term": entry.term,
            "label": entry.label,
            "status": entry.status,
            "remaining": entry.remaining,
            "notes": entry.notes,
        }
        if entry.draft is not None:
            row["draft"] = _draft_payload(
                entry.draft, snapshots.get(entry.term)
            )
        else:
            row["draft"] = None
        rows.append(row)

    risks: list[str] = list(roadmap.warnings)
    degree_info = raw.get("degreeInformation") or {}
    reports = {
        row.get("code"): row.get("value")
        for row in degree_info.get("reportArray") or []
        if row.get("code")
    }
    anticipated = reports.get("GRADTERMUG")
    if roadmap.final_remaining:
        risks.append(
            f"{len(roadmap.final_remaining)} requirement(s) remain after "
            f"{roadmap.terms[-1].label if roadmap.terms else 'the planned terms'}: "
            + "; ".join(roadmap.final_remaining)
        )
        if anticipated:
            risks.append(
                f"The audit anticipates graduation in {anticipated}; verify the remaining "
                "courses are offered before that term (see unw_offering_matrix)."
            )
    for entry in roadmap.terms:
        if entry.status == "no_catalog":
            risks.append(
                f"{entry.label} ({entry.term}) has no published catalog query; "
                "its requirements were not planned."
            )
        elif entry.draft is not None and entry.draft.total_credits == 0:
            risks.append(
                f"{entry.label} ({entry.term}) placed 0 credits: nothing outstanding is "
                "offered there under these constraints."
            )
    return {
        "ok": True,
        "start_term": start_term,
        "terms": planned_terms,
        "include_summer": include_summer,
        "max_credits": max_credits,
        "current_term": current_term,
        "anticipated_graduation": anticipated,
        "student": audit.student_name,
        "catalogs": catalogs,
        "planned": rows,
        "final_remaining": roadmap.final_remaining,
        "risks": risks,
        "note": (
            "Roadmap honors max_credits and include_summer only. Course/CRN pins, "
            "minimum-load and instructor exclusions apply to unw_plan_draft."
        ),
    }


# --------------------------------------------------------------------------- #
# offering matrix
# --------------------------------------------------------------------------- #
def _observed_terms() -> list[str]:
    index = TermIndex()
    try:
        terms = set(index.terms())
    finally:
        index.close()
    terms |= {
        path.stem
        for path in TERMS_DIR.glob("*.json")
        if path.stem.isdigit() and len(path.stem) == 6
    }
    return sorted(terms, key=_term_key)


def _offering_specs(courses: Sequence[str] | str | None, audit: Audit | None) -> list[str]:
    specs = _norm_list(courses)
    if specs:
        return specs
    if audit is None:
        raise ValueError("Pass at least one course/attribute target.")
    specs = []
    for requirement in requirements_from_audit(audit):
        if requirement.satisfied:
            continue
        specs += [target.code for target in requirement.targets]
        specs += [f"ATTRIBUTE {code}" for code in requirement.attributes]
        specs += [f"{subject} *" for subject in requirement.subject_wildcards]
    return list(dict.fromkeys(specs))


def _history_payload(
    history: OfferingHistory, observed: Sequence[str]
) -> dict:
    entries = history.terms
    offered_labels = ", ".join(term_label(term) for term in history.offered_terms)
    if history.pattern.value == "unknown":
        summary = (
            f"never observed in {len(observed)} cached terms "
            f"({', '.join(term_label(term) for term in observed)})"
        )
    else:
        summary = f"{history.pattern.value}; offered in {offered_labels}"
    return {
        "key": history.key,
        "pattern": history.pattern.value,
        "offered_terms": history.offered_terms,
        "summary": summary,
        "terms": [
            {
                "term": entry.term,
                "label": term_label(entry.term),
                "offered": entry.offered,
                "sections": entry.sections,
                "open_sections": entry.open_sections,
                "instructors": entry.instructors,
                "part_of_terms": entry.part_of_terms,
            }
            for entry in entries
        ],
    }


def offering_matrix_payload(
    courses: Sequence[str] | str | None = None, refresh: bool = False
) -> dict:
    observed = _observed_terms()
    if not observed:
        raise ValueError(
            "No cached term snapshots. Run `eagle-flightplan search --term <term>` once, "
            "or pass refresh=True to fetch every observed term."
        )
    snapshots: dict[str, TermSnapshot] = {}
    catalogs = []
    for term in observed:
        snapshot = fetch_term(term, refresh=True) if refresh else load_snapshot(term)
        if snapshot is None:
            continue
        snapshots[term] = snapshot
        catalogs.append(_catalog_meta(snapshot))
    if not snapshots:
        raise ValueError("Cached snapshot files exist but none could be loaded.")

    audit = None
    specs = _norm_list(courses)
    if not specs:
        audit = AuditClient().audit()
    specs = _offering_specs(specs, audit)
    if not specs:
        raise ValueError("No outstanding requirement targets found; pass courses explicitly.")

    histories: list[OfferingHistory] = []
    for spec in specs:
        text = spec.strip()
        upper = text.upper()
        if upper.startswith(("ATTRIBUTE ", "ATTR ")):
            code = upper.split(None, 1)[1].strip()
            histories.append(offering_history_attribute(code, observed, snapshots))
        elif upper.endswith("*"):
            subject = upper[:-1].strip()
            synthetic = Requirement(
                id=f"synthetic:{subject}",
                parent_group="",
                kind=GroupKind.OTHER,
                label=f"{subject} *",
                subject_wildcards=[subject],
            )
            rows = offering_matrix([synthetic], observed, snapshots)
            if rows:
                histories.append(rows[0])
        else:
            histories.append(
                offering_history_course(CourseKey.parse(text), observed, snapshots)
            )
    return {
        "ok": True,
        "observed_terms": observed,
        "observed_labels": [term_label(term) for term in observed],
        "count": len(histories),
        "catalogs": catalogs,
        "courses": [_history_payload(history, observed) for history in histories],
        "legend": {
            "every-term": "observed in at least one spring and one fall",
            "fall-only": "observed only in fall terms",
            "spring-only": "observed only in spring terms",
            "unknown": "never observed in the cached terms",
        },
        "note": (
            "Section counts are per term; open_sections counts seats>0. Pattern is "
            "inferred from observed terms only, not a catalog guarantee."
        ),
    }


# --------------------------------------------------------------------------- #
# Banner Plan Ahead (term-scoped worksheet writes)
# --------------------------------------------------------------------------- #
def _list_plan_rows(
    client: PlanClient, term: str
) -> tuple[list[Plan], bool, str]:
    """List plans, distinguishing "no plans" from "Plan Ahead unavailable".

    Banner renders a plan list only for selectable terms. For a non-selectable
    term the page has no ``plans:`` bootstrap, and the frozen ``PlanClient``
    heuristically reports a session expiry. When the session is provably alive,
    the honest answer is "Plan Ahead is unavailable for this term", not "log in".
    """
    try:
        return client.list_plans(term), True, ""
    except PlanAheadUnavailable as exc:
        return [], False, str(exc)
    except auth.SessionExpiredError:
        if not auth.session_valid():
            raise
        return (
            [],
            False,
            (
                f"Banner rendered no Plan Ahead list for {term}; the term is not "
                "selectable in Plan Ahead (the session itself is alive). No plans "
                "can be read or written for this term."
            ),
        )


def plans_payload(term: str) -> dict:
    with _BANNER_LOCK:
        plans, available, unavailable_note = _list_plan_rows(PlanClient(), term)
    note = (
        "Plans are TERM-SCOPED: this list contains only plans whose planHeader.term "
        f"is {term}. An empty list means no plans for THIS term, not that no plans exist."
    )
    if unavailable_note:
        note = f"{note} {unavailable_note}"
    return {
        "ok": True,
        "term": term,
        "term_label": term_label(term) if len(term) == 6 else term,
        "term_scoped": True,
        "plan_ahead_available": available,
        "count": len(plans),
        "plans": [plan_payload(plan) for plan in plans],
        "note": note,
    }


def _resolve_plan(plans: Sequence[Plan], reference: str | int) -> Plan:
    """Resolve an id or exact name; ambiguous/missing refs list candidates."""
    text = str(reference).strip()
    if not text:
        raise PlanNotFoundError("Empty plan reference; pass a plan id or exact name.")
    if text.isdigit():
        matches = [plan for plan in plans if plan.id == int(text)]
        if len(matches) == 1:
            return matches[0]
        available = "; ".join(f"{plan.id} {plan.name!r}" for plan in plans) or "none"
        raise PlanNotFoundError(
            f"No plan with id {text} in this term. Available: {available}"
        )
    matches = [plan for plan in plans if plan.name == text]
    if not matches:
        matches = [plan for plan in plans if plan.name.lower() == text.lower()]
    if len(matches) == 1:
        return matches[0]
    available = "; ".join(f"{plan.id} {plan.name!r}" for plan in plans) or "none"
    if len(matches) > 1:
        candidates = "; ".join(f"{plan.id} {plan.name!r}" for plan in matches)
        raise PlanError(
            f"Plan name {text!r} is ambiguous; candidates: {candidates}"
        )
    raise PlanNotFoundError(
        f"No plan named {text!r} in this term. Available: {available}"
    )


def _plan_total(plan: Plan) -> float:
    return round(sum(course.credits or 0 for course in plan.courses), 2)


def _course_identity(course) -> tuple:
    if course.id is not None:
        return ("id", course.id)
    return ("row", course.subject, course.number, course.crn or "", course.title)


def _plan_state(plan: Plan) -> dict:
    return {
        "plan_id": plan.id,
        "plan_name": plan.name,
        "term": plan.term,
        "sequence_number": plan.sequence_number,
        "course_count": len(plan.courses),
        "total_credits": _plan_total(plan),
        "courses": [_plan_course_payload(course) for course in plan.courses],
    }


def _plan_diff(before: Plan | None, after: Plan | None, action: str) -> dict:
    """Rows added/removed and credits before/after, from two real plan reads."""
    before_courses = list(before.courses) if before else []
    after_courses = list(after.courses) if after else []
    before_keys = {_course_identity(course) for course in before_courses}
    after_keys = {_course_identity(course) for course in after_courses}
    anchor = after or before
    credits_before = round(sum(course.credits or 0 for course in before_courses), 2)
    credits_after = round(sum(course.credits or 0 for course in after_courses), 2)
    return {
        "action": action,
        "plan_id": anchor.id if anchor else None,
        "plan_name": anchor.name if anchor else None,
        "rows_added": [
            _plan_course_payload(course)
            for course in after_courses
            if _course_identity(course) not in before_keys
        ],
        "rows_removed": [
            _plan_course_payload(course)
            for course in before_courses
            if _course_identity(course) not in after_keys
        ],
        "course_count_before": len(before_courses),
        "course_count_after": len(after_courses),
        "credits_before": credits_before,
        "credits_after": credits_after,
        "credits_delta": round(credits_after - credits_before, 2),
    }


def _write_readback_failure(action: str, plan_id: int, exc: Exception) -> dict:
    return {
        "ok": False,
        "error": (
            f"write {action} was sent but the Banner read-back failed: "
            f"{type(exc).__name__}: {exc}"
        ),
        "writes_performed": [action],
        "plan_id": plan_id,
        "hint": (
            "Do not repeat the write. Call unw_banner_plans to inspect the plan, "
            "then decide with the user whether a retry is needed."
        ),
    }


def _refuse_write(
    intended_effect: str, current_state: dict, required: dict, reason: str = ""
) -> dict:
    message = f"Confirmation required: {intended_effect}. No write was performed."
    if reason:
        message = f"{message} {reason}"
    return {
        "ok": False,
        "refused": True,
        "confirmation_required": True,
        "writes_performed": [],
        "intended_effect": intended_effect,
        "current_state": current_state,
        "required_confirmation": required,
        "error": message,
        "hint": (
            "Show the intended effect to the user and get explicit approval before "
            "re-calling with the required confirmation."
        ),
    }


def _plan_summaries(plans: Sequence[Plan]) -> list[dict]:
    return [
        {
            "id": plan.id,
            "name": plan.name,
            "sequence_number": plan.sequence_number,
            "preferred": plan.preferred,
            "course_count": len(plan.courses),
            "total_credits": _plan_total(plan),
        }
        for plan in plans
    ]


def _describe_add_target(
    term: str,
    crn: str | int | None,
    subject: str | None,
    number: str | int | None,
) -> str:
    snapshot = _snapshot(term)
    if crn is not None:
        section = snapshot.by_crn(str(crn))
        if section is None:
            raise ValueError(
                f"CRN {crn} is not in term {term}; use unw_search to find the right section."
            )
        credits = f"{section.credits:g} cr" if section.credits is not None else "variable credits"
        return f"{section.code} CRN {section.crn} ({section.title}, {credits})"
    assert subject and number is not None
    sections = snapshot.by_course(subject, str(number))
    if not sections:
        raise ValueError(f"{subject.upper()} {number} is not offered in {term}.")
    return (
        f"{subject.upper()} {number} (course-level; {len(sections)} section(s) offered in {term})"
    )


def _match_plan_course(
    plan: Plan,
    *,
    crn: str | int | None = None,
    subject: str | None = None,
    number: str | int | None = None,
):
    for course in plan.courses:
        if crn is not None and course.crn == str(crn):
            return course
        if (
            subject
            and number is not None
            and course.subject.upper() == subject.upper()
            and course.number == str(number)
        ):
            return course
    return None


def plan_create_payload(term: str, name: str) -> dict:
    client = PlanClient()
    with _BANNER_LOCK:
        plans, available, unavailable_note = _list_plan_rows(client, term)
        if not available:
            raise PlanError(unavailable_note)
        if len(plans) >= 5:
            raise PlanError(
                f"Term {term} already has {len(plans)} plans; Banner allows at most 5 per term."
            )
        plan = client.create_plan(name, term)
        try:
            final = client.get_plan(plan.id, term)
            plans_after = client.list_plans(term)
        except Exception as exc:
            return _write_readback_failure("create_plan", plan.id, exc)
    diff = _plan_diff(None, final, "create_plan")
    diff["plan_count_before"] = len(plans)
    diff["plan_count_after"] = len(plans_after)
    return {
        "ok": True,
        "term": term,
        "created": plan_payload(final),
        "diff": diff,
        "writes_performed": ["create_plan"],
        "note": "The new plan is term-scoped; read it back with unw_banner_plans(term).",
    }


def _audit_prereq_gate() -> tuple[set[str] | None, dict[str, str] | None]:
    """Audit codes + grades for the prereq gate; ``(None, None)`` if unavailable."""
    try:
        audit = AuditClient().audit()
        return applied_codes(audit), applied_grades(audit)
    except Exception as exc:
        logger.warning("audit unavailable for plan prereq gate: %s", type(exc).__name__)
        return None, None


def plan_add_payload(
    term: str,
    plan: str | int,
    crn: str | int | None = None,
    subject: str | None = None,
    number: str | int | None = None,
    passed_codes: set[str] | None = None,
    grades: dict[str, str] | None = None,
) -> dict:
    if crn is None and not (subject and number is not None):
        raise ValueError("Pass crn, or subject and number.")
    client = PlanClient()
    with _BANNER_LOCK:
        plans, available, unavailable_note = _list_plan_rows(client, term)
        if not available:
            raise PlanError(unavailable_note)
        resolved = _resolve_plan(plans, plan)
        course = client.add_course(
            resolved.id,
            term,
            crn=str(crn) if crn is not None else None,
            subject=subject,
            number=str(number) if number is not None else None,
            passed_codes=passed_codes,
            grades=grades,
        )
        try:
            final = client.get_plan(resolved.id, term)
        except Exception as exc:
            return _write_readback_failure("add_course", resolved.id, exc)
    return {
        "ok": True,
        "term": term,
        "plan": plan_payload(final),
        "added": _plan_course_payload(course),
        "diff": _plan_diff(resolved, final, "add_course"),
        "writes_performed": ["add_course"],
        "prereq_checked": passed_codes is not None,
    }


def plan_remove_payload(
    term: str,
    plan: str | int,
    crn: str | int | None = None,
    subject: str | None = None,
    number: str | int | None = None,
) -> dict:
    if crn is None and not (subject and number is not None):
        raise ValueError("Pass crn, or subject and number.")
    client = PlanClient()
    with _BANNER_LOCK:
        plans, available, unavailable_note = _list_plan_rows(client, term)
        if not available:
            raise PlanError(unavailable_note)
        resolved = _resolve_plan(plans, plan)
        client.remove_course(
            resolved.id,
            term,
            crn=str(crn) if crn is not None else None,
            subject=subject,
            number=str(number) if number is not None else None,
        )
        try:
            final = client.get_plan(resolved.id, term)
        except Exception as exc:
            return _write_readback_failure("remove_course", resolved.id, exc)
    return {
        "ok": True,
        "term": term,
        "plan": plan_payload(final),
        "diff": _plan_diff(resolved, final, "remove_course"),
        "writes_performed": ["remove_course"],
    }


def plan_delete_payload(term: str, plan: str | int) -> dict:
    client = PlanClient()
    with _BANNER_LOCK:
        plans, available, unavailable_note = _list_plan_rows(client, term)
        if not available:
            raise PlanError(unavailable_note)
        resolved = _resolve_plan(plans, plan)
        client.delete_plan(resolved.id, term)
        try:
            plans_after = client.list_plans(term)
        except Exception as exc:
            return _write_readback_failure("delete_plan", resolved.id, exc)
    diff = _plan_diff(resolved, None, "delete_plan")
    diff["plan_count_before"] = len(plans)
    diff["plan_count_after"] = len(plans_after)
    return {
        "ok": True,
        "term": term,
        "deleted": {"id": resolved.id, "name": resolved.name},
        "remaining_plans": len(plans_after),
        "diff": diff,
        "writes_performed": ["delete_plan"],
    }


def plan_create_guarded(term: str, name: str, confirm: bool) -> dict:
    client = PlanClient()
    with _BANNER_LOCK:
        plans, available, unavailable_note = _list_plan_rows(client, term)
        if not available:
            raise PlanError(unavailable_note)
        if len(plans) >= 5:
            raise PlanError(
                f"Term {term} already has {len(plans)} plans; Banner allows at most 5 per term."
            )
        state = {
            "term": term,
            "plan_count": len(plans),
            "plan_limit": 5,
            "plans": _plan_summaries(plans),
        }
        effect = (
            f"would create a Plan Ahead worksheet {name!r} for term {term} "
            f"(current plans: {len(plans)}/5)"
        )
        if not confirm:
            return _refuse_write(effect, state, {"confirm": True})
        return plan_create_payload(term, name)


def plan_add_guarded(
    term: str,
    plan: str | int,
    confirm: bool,
    crn: str | int | None = None,
    subject: str | None = None,
    number: str | int | None = None,
) -> dict:
    if crn is None and not (subject and number is not None):
        raise ValueError("Pass crn, or subject and number.")
    client = PlanClient()
    with _BANNER_LOCK:
        plans, available, unavailable_note = _list_plan_rows(client, term)
        if not available:
            raise PlanError(unavailable_note)
        resolved = _resolve_plan(plans, plan)
        existing = _match_plan_course(resolved, crn=crn, subject=subject, number=number)
        if existing is not None:
            raise DuplicateCourseError(
                f"{existing.code} CRN {existing.crn or '-'} is already in plan "
                f"{resolved.id} {resolved.name!r}; remove it first or pick another section."
            )
        target = _describe_add_target(term, crn, subject, number)
        state = _plan_state(resolved)
        effect = (
            f"would add {target} to plan {resolved.id} {resolved.name!r} "
            f"(currently {state['course_count']} rows, {state['total_credits']:g} credits)"
        )
        if not confirm:
            return _refuse_write(effect, state, {"confirm": True})
        passed_codes, grades = _audit_prereq_gate()
        return plan_add_payload(
            term,
            resolved.id,
            crn=crn,
            subject=subject,
            number=number,
            passed_codes=passed_codes,
            grades=grades,
        )


def plan_remove_guarded(
    term: str,
    plan: str | int,
    confirm: bool,
    crn: str | int | None = None,
    subject: str | None = None,
    number: str | int | None = None,
) -> dict:
    if crn is None and not (subject and number is not None):
        raise ValueError("Pass crn, or subject and number.")
    client = PlanClient()
    with _BANNER_LOCK:
        plans, available, unavailable_note = _list_plan_rows(client, term)
        if not available:
            raise PlanError(unavailable_note)
        resolved = _resolve_plan(plans, plan)
        target = _match_plan_course(resolved, crn=crn, subject=subject, number=number)
        if target is None:
            rows = "; ".join(
                f"{course.code} CRN {course.crn or '-'}" for course in resolved.courses
            )
            raise PlanNotFoundError(
                f"No matching course in plan {resolved.id} ({crn=}, {subject=}, {number=}). "
                f"Rows: {rows or 'none'}"
            )
        state = _plan_state(resolved)
        credits = f"{target.credits:g} cr" if target.credits is not None else "variable credits"
        projected = round(state["total_credits"] - (target.credits or 0), 2)
        effect = (
            f"would remove {target.code} CRN {target.crn or '-'} ({credits}) from plan "
            f"{resolved.id} {resolved.name!r}; credits {state['total_credits']:g} -> "
            f"{projected:g}"
        )
        if not confirm:
            return _refuse_write(effect, state, {"confirm": True})
        return plan_remove_payload(
            term,
            resolved.id,
            crn=crn,
            subject=subject,
            number=number,
        )


def plan_delete_guarded(
    term: str,
    plan: str | int,
    confirm: bool,
    confirm_name: str | None,
) -> dict:
    client = PlanClient()
    with _BANNER_LOCK:
        plans, available, unavailable_note = _list_plan_rows(client, term)
        if not available:
            raise PlanError(unavailable_note)
        resolved = _resolve_plan(plans, plan)
        state = _plan_state(resolved)
        required = {"confirm": True, "confirm_name": resolved.name}
        effect = (
            f"would DELETE plan {resolved.id} {resolved.name!r} for term {term} "
            f"({state['course_count']} rows, {state['total_credits']:g} credits); "
            "deletion cannot be undone"
        )
        if not confirm:
            return _refuse_write(effect, state, required)
        if confirm_name != resolved.name:
            return _refuse_write(
                effect,
                state,
                required,
                reason=(
                    f"confirm_name {confirm_name!r} does not match the exact plan name "
                    f"{resolved.name!r}."
                ),
            )
        return plan_delete_payload(term, resolved.id)


# --------------------------------------------------------------------------- #
# registration preview (read-only)
# --------------------------------------------------------------------------- #
def registration_preview_payload(
    term: str,
    crns: Sequence[str | int] | str | int,
    refresh: bool = False,
    hours: dict[str, float] | None = None,
) -> dict:
    """Dry run: Banner eligibility + local conflict/seat/prereq checks."""
    wanted = [str(item).strip() for item in _norm_list(crns)]
    if not wanted:
        raise ValueError("Pass at least one CRN.")
    overrides = {str(crn): float(value) for crn, value in (hours or {}).items()}
    ssb = SSBClient(auth.ensure())
    with _BANNER_LOCK:
        eligibility: TermEligibility = ssb.set_term(term, mode="registration")
        snapshot = _snapshot(term, refresh)
    sections = []
    missing = []
    for crn in wanted:
        section = snapshot.by_crn(crn)
        if section is None:
            missing.append(crn)
        else:
            sections.append(section)
    schedule_sections = [section.to_schedule_section() for section in sections]
    conflicts = find_conflicts(schedule_sections)

    passed: set[str] | None
    grades: dict[str, str] = {}
    prereq_note = ""
    try:
        audit = AuditClient().audit()
        passed = applied_codes(audit)
        grades = applied_grades(audit)
    except Exception as exc:
        passed = None
        prereq_note = f"prerequisite check skipped: audit unavailable ({type(exc).__name__})"
    prereq_rows = []
    if passed is not None:
        provider = PrereqProvider(ssb=ssb)
        with _BANNER_LOCK:
            for section in sections:
                ok, reason = provider.check(section, passed, grades)
                prereq_rows.append(
                    {
                        "crn": section.crn,
                        "course": section.code,
                        "prereq_ok": ok,
                        "prereq_status": (
                            "verified" if ok is True else "blocked" if ok is False else "unknown"
                        ),
                        "reason": reason,
                    }
                )

    failures = list(eligibility.failures)
    can_register = bool(eligibility.student_elig_valid)
    return {
        "ok": True,
        "dry_run": True,
        "registration_attempted": False,
        "writes_performed": [],
        "term": term,
        "term_label": term_label(term) if len(term) == 6 else term,
        "can_register": can_register,
        "eligibility": {
            "reg_allowed": eligibility.reg_allowed,
            "student_elig_valid": eligibility.student_elig_valid,
            "student_elig_failures": failures,
            "fwd_url": eligibility.fwd_url,
        },
        "banner_message": (
            "; ".join(failures)
            if failures
            else "Banner reports this term as registration-eligible."
        ),
        "sections": [
            {
                **section_payload(section),
                "hours_applied": overrides.get(str(section.crn)),
                "planned_credits": _planned_credits(section, overrides),
            }
            for section in sections
        ],
        "missing_crns": missing,
        "hours": overrides,
        "total_credits": round(
            sum(_planned_credits(section, overrides) for section in sections), 2
        ),
        "conflict_count": len(conflicts),
        "conflicts": [conflict.summary for conflict in conflicts],
        "grid": render_grid(schedule_sections, include_tba=True) if sections else "",
        "grid_note": grid_note(schedule_sections),
        "partial_tba_crns": [section.crn for section in sections if section.partial_tba],
        "prerequisites": {
            "checked": passed is not None,
            "note": prereq_note,
            "rows": prereq_rows,
        },
        "note": (
            "This endpoint NEVER posts add/drop/register. Banner's registration write path "
            "is not mapped and every observed term is registration-ineligible; the "
            "eligibility failure text above is the authoritative answer."
        ),
    }


# --------------------------------------------------------------------------- #
# registration readiness (read-only)
# --------------------------------------------------------------------------- #
def registration_window_payload(term: str | None = None) -> dict:
    """Read-only personal time-ticket window(s) from Banner.

    ``term/search`` supplies eligibility; when it has no window, the Prepare for
    Registration page supplies the assigned future ticket. The latter never makes
    a term eligible.
    """
    from .registration import TZ, countdown
    from .registration import window as registration_window

    client = SSBClient(auth.ensure())
    next_term: str | None = None
    with _BANNER_LOCK:
        try:
            listed = client.get_terms()
        except Exception:
            listed = []
        codes = [item.code for item in listed if "view only" not in item.description.lower()]
        upcoming = [code for code in codes if _term_key(code) > _term_key(CURRENT_TERM)]
        next_term = min(upcoming, key=_term_key) if upcoming else None
        if term:
            wanted = [str(term).strip()]
        else:
            wanted = [CURRENT_TERM]
            if next_term and next_term != CURRENT_TERM:
                wanted.append(next_term)
        rows = []
        for code in wanted:
            try:
                result = registration_window(code, ssb=client)
                data = result.model_dump(mode="json")
                countdown_text = countdown(result.opens_in_seconds)
                if result.end is not None and result.end < datetime.now(TZ):
                    countdown_text = "closed"
                data.update(
                    {
                        "term_label": term_label(code) if len(code) == 6 else code,
                        "opens_in_seconds": result.opens_in_seconds,
                        "countdown": countdown_text,
                        "registration_standing": result.standing,
                        "standing_note": (
                            "registration_standing is Banner's standing for registration "
                            "purposes for this term; Degree Works classification may differ"
                        ),
                    }
                )
                rows.append(data)
            except Exception as exc:
                rows.append(
                    {
                        "term": code,
                        "term_label": term_label(code) if len(code) == 6 else code,
                        "eligible": None,
                        "parse_status": "unknown",
                        "start": None,
                        "end": None,
                        "reason": "window probe failed",
                        "raw_text": "",
                        "error": f"{type(exc).__name__}: {exc}",
                        "opens_in_seconds": None,
                        "countdown": "unknown",
                        "registration_standing": None,
                        "standing_note": (
                            "registration_standing is Banner's standing for registration "
                            "purposes for this term; Degree Works classification may differ"
                        ),
                    }
                )
    return {
        "ok": True,
        "read_only": True,
        "writes_performed": [],
        "timezone": "America/Chicago",
        "requested_term": term,
        "current_term": CURRENT_TERM,
        "next_term": next_term,
        "count": len(rows),
        "windows": rows,
        "note": (
            "READ-ONLY: parses Banner's registration eligibility prose "
            "(POST /term/search?mode=registration) and, when that has no window, the "
            "server-rendered Prepare for Registration page "
            "(GET /ssb/prepareRegistration/prepareRegistration) into America/Chicago "
            "datetimes. eligible=false with a start means the ticket is assigned but "
            "the window is not open yet. It never registers, adds, drops or withdraws. "
            "parse_status=unknown means Banner's text was not understood; report "
            "raw_text verbatim."
        ),
    }


# --------------------------------------------------------------------------- #
# MCP server
# --------------------------------------------------------------------------- #
INSTRUCTIONS = """\
Eagle Flightplan: Degree Works audit + Banner 9 class search + planning tools.
START HERE
- Call unw_session() first. If authenticated=false, try one read tool
  (unw_profile/unw_terms); reads may work when degraded. Stop only on auth/session
  errors or before a write, then use fix_command; never improvise a login.
- Then unw_profile() and unw_remaining(). The audit already knows the student's
  majors, minors, catalog year, classifications and advisors: never ask the student
  for facts the audit can answer.
- DO confirm declared programs against what the student says. They diverge often
  (e.g. a student says "ministry minor" while the audit says "Bible"). Report the
  discrepancy, do not silently pick one.
- For "what should I take" call unw_semester_plan once (draft+conflicts+ticket+plans);
  use unw_plan_draft only for full section objects.

ASK BEFORE ASSUMING
- Credit budget, blocked days/times, earliest start, latest end, summer
  willingness, and graduation target. unw_roadmap and unw_plan_draft have
  defaults; defaults are not the student's preferences.

PLANNING ORDER
1. Scarce major-required courses first. Check unw_offering_matrix before proposing
   any course: never propose a fall-only course for spring (or vice versa).
2. Then the major credit pool / electives, then minor-required slots.
3. Then gen-ed slots, choosing the highest RMP rating among eligible sections.
- Never propose two sections with a time conflict. Use unw_conflicts on the final
  CRN set and show the ASCII grid.
- Flag NOT_YET_OPEN / CLOSED sections, single-section courses, unassigned faculty
  ("Staff"), and variable-credit rows. Seats are a 15-minute snapshot.

RATINGS
- Unrated is unknown, not bad: null is never 0.0. Prefer rated instructors but say
  when the evidence is thin (n<3 is weak). Match confidence reflects name
  ambiguity, not teaching quality.

BANNER PLANS (term-scoped)
- Plans belong to one term. unw_banner_plans(term) returning [] means "no plans
  for THIS term", not "no plans exist". Other terms' plans are invisible.
- Confirmation is ENFORCED by the server, not advisory. Every write tool requires
  confirm=true; unw_banner_plan_delete also requires confirm_name equal to the
  plan's exact name. Calling without them returns refused=true, the intended
  effect and the current plan state, and performs no write.
- Still show that intended effect to the user and get explicit approval before
  re-calling with confirmation. Add/remove need a plan id or exact name;
  ambiguous names are rejected with candidates, never guessed.
- Every successful write returns a read-back before/after diff (rows, credits). Report it.
- Do not create/delete plans just to test something: the student's plans are real.

REGISTRATION
- unw_register_preview is a DRY RUN. It never POSTs add/drop/register. Use
  unw_registration_window for time tickets; report Banner's eligibility failure
  text verbatim instead of implying registration can proceed.

OUTPUT
- Always show the ASCII grid and a one-line rationale per pick. Report
  discrepancies (already-completed course, requirement the plan drops, audit vs
  catalog conflicts) instead of quietly resolving them.
CACHING
- Term snapshots are cached for 15 minutes. Pass refresh=true only when the user
  needs current seats. unw_offering_matrix reads historical snapshots and is the
  right tool for "can this course be taken next term?".
"""

mcp = MCPServer(
    name=SERVER_NAME,
    version=SERVER_VERSION,
    description=(
        "UNW degree audit, Banner class search, conflict checking, degree planning, "
        "offering history, Rate My Professors and term-scoped Plan Ahead worksheets."
    ),
    instructions=INSTRUCTIONS,
)


@mcp.tool(structured_output=False)
def unw_session() -> dict:
    """Check the UNW session: ``authenticated`` (live check), ``degraded`` (state
    exists but the live check failed), state-file age, cookie count and earliest
    cookie expiry (values are never exposed), plus ``read_hint``/``write_hint``
    and the exact packaged CLI command that repairs an expired session. Call this
    before any other tool. If unauthenticated but degraded, try one read tool
    before asking for a login."""
    return _safe(session_status_payload)


@mcp.tool(structured_output=False)
def unw_profile() -> dict:
    """Student profile from Degree Works: name, id, classification, degree/school,
    catalog year(s), majors, minors, programs, college, advisors, credits earned
    and in progress, GPA, percent complete, anticipated graduation, academic
    standing and holds. Holds are not exposed by the mapped APIs: null = unknown."""
    return _safe(profile_payload)


@mcp.tool(structured_output=False)
def unw_audit(group: str | None = None) -> dict:
    """Structured degree-audit requirement tree (groups -> blocks -> course
    options, with applied/in-progress/transfer flags). ``group`` filters by a
    case-insensitive substring of a group title/kind or block label."""
    return _safe(audit_payload, group)


@mcp.tool(structured_output=False)
def unw_remaining() -> dict:
    """Unmet requirements only: id, title, parent group, kind, credits remaining,
    pick-N progress, course/attribute/wildcard options, status, priority class and
    ``shared_title_with`` (other ids with the same title/options: double-counting
    risk). This is the authoritative "what is left" list."""
    return _safe(remaining_payload)


@mcp.tool(structured_output=False)
def unw_terms(refresh: bool = False) -> dict:
    """Banner term codes with descriptions plus is_next, is_current, view_only and
    registration_eligible flags. Eligibility is probed through Banner's own
    mode=registration response (cached per process); refresh=True re-probes it.
    Failures are included verbatim."""
    return _safe(terms_payload, refresh)


@mcp.tool(structured_output=False)
def unw_search(
    term: str,
    subject: str | None = None,
    number: str | int | None = None,
    keyword: str | None = None,
    attribute: str | None = None,
    instructor: str | None = None,
    open_only: bool = False,
    limit: int = 50,
    offset: int = 0,
    refresh: bool = False,
    include_ratings: bool = False,
) -> dict:
    """Search one term's sections (normalised rows: CRN, course, title, credits,
    seats, days/times, faculty, attributes, availability). Banner ignores the
    attribute/instructor/open-only query parameters, so filtering happens
    client-side over a cached term snapshot. ``limit`` is capped at 200; when
    ``limit_capped`` is true follow ``next_offset`` to page through the rest.
    ``include_ratings=True`` adds a compact RMP map for the unique instructors on
    the returned page."""
    return _safe(
        search_payload,
        term,
        subject,
        number,
        keyword,
        attribute,
        instructor,
        open_only,
        limit,
        offset,
        refresh,
        include_ratings,
    )


@mcp.tool(structured_output=False)
def unw_course(term: str, crn: str | int, refresh: bool = False) -> dict:
    """Full detail for one CRN: meetings, faculty with RMP evidence, attributes,
    catalog description, prerequisite/corequisite/restriction text plus the parsed
    prerequisite codes, linked lecture/lab CRNs, enrollment text and availability."""
    return _safe(course_payload, term, crn, refresh)


@mcp.tool(structured_output=False)
def unw_candidates(
    term: str,
    requirement: str | None = None,
    refresh: bool = False,
    open_only: bool = True,
    compact: bool = True,
    limit: int = 0,
) -> dict:
    """Resolve unmet requirements to sections offered in ``term`` (wraps the
    catalog resolver). ``requirement`` matches an id, title, parent group or
    target; omit it for every unmet requirement. Every empty result carries the
    reason (never offered / all closed / already completed). ``limit`` caps
    candidates per requirement (0=all); ``compact`` (default true) omits repeated
    section detail. Pass ``compact=false`` for full section rows. One requirement
    can be large, so prefer limit for broad queries."""
    return _safe(candidates_payload, term, requirement, refresh, open_only, compact, limit)


@mcp.tool(structured_output=False)
def unw_ratings(names: Sequence[str] | str, refresh: bool = False) -> dict:
    """Batch Rate My Professors lookup. Accepts "Last, First" or "First Last".
    Unrated professors return rating=null (never 0.0); weak_evidence flags n<3.
    Cache entries expire after 7 days; ``refresh=True`` refetches now. Transient
    RMP failures are never cached."""
    return _safe(ratings_payload, names, refresh)


@mcp.tool(structured_output=False)
def unw_conflicts(
    term: str,
    crns: Sequence[str | int] | str | int,
    refresh: bool = False,
    hours: dict[str, float] | None = None,
) -> dict:
    """Pairwise time conflicts for a CRN set, plus the ASCII week grid and the list
    of unscheduled (TBA/asynchronous) sections. Half-open intervals; only
    lecture/lab (or lecture/clinical) linked pairs are exempt. ``hours`` sets the
    credit hours reserved per CRN so ``total_credits`` matches the draft basis."""
    return _safe(conflicts_payload, term, crns, refresh, hours)


@mcp.tool(structured_output=False)
def unw_plan_draft(
    term: str,
    max_credits: float = 16,
    profile: str = "max_rated",
    avoid_days: Sequence[str] | str | None = None,
    earliest: str | None = None,
    latest: str | None = None,
    include_summer: bool = False,
    refresh: bool = False,
    min_credits: float = 0,
    required_crns: Sequence[str | int] | str | None = None,
    required_courses: Sequence[str] | str | None = None,
    exclude_crns: Sequence[str | int] | str | None = None,
    exclude_courses: Sequence[str] | str | None = None,
    avoid_instructors: Sequence[str] | str | None = None,
    exclude_arrangement: bool = False,
    hours: dict[str, float] | None = None,
    credits: float | None = None,
    pins: Sequence[str] | str | None = None,
    courses: Sequence[str] | str | None = None,
) -> dict:
    """Draft a conflict-free term plan from the audit, prioritizing major-required
    courses, then major pools, then minor-required, then gen-ed by RMP rating.

    Canonical constraints are ``max_credits`` and ``required_courses``; the
    ``credits``/``pins``/``courses`` aliases mirror unw_semester_plan.
    ``required_crns``/``required_courses`` pin sections the student insists on;
    each pin reports placed/unavailable/conflict/prereq_blocked/excluded/budget/
    already_completed, and a failed pin makes ok=false. ``exclude_crns``/
    ``exclude_courses`` remove candidates. ``min_credits`` keeps filling elective
    pools until the load is met (and reports min_credits_met). ``hours`` maps a CRN
    to the credit hours to reserve for a variable-credit section (default: the low
    end; a 0-low range reserves the high end unless overridden). Pins apply to
    this single-term draft only, not to unw_roadmap. include_summer=True also
    drafts the following summer for requirements that did not fit."""
    merged_courses = _merge_required_courses(required_courses, pins, courses)
    return _safe(
        draft_payload,
        term,
        credits if credits is not None else max_credits,
        profile,
        avoid_days,
        earliest,
        latest,
        include_summer,
        refresh,
        min_credits,
        required_crns,
        merged_courses,
        exclude_crns,
        exclude_courses,
        avoid_instructors,
        exclude_arrangement,
        hours,
    )


@mcp.tool(structured_output=False)
def unw_semester_plan(
    term: str,
    max_credits: float = 16,
    profile: str = "max_rated",
    avoid_days: Sequence[str] | str | None = None,
    earliest: str | None = None,
    latest: str | None = None,
    include_summer: bool = False,
    refresh: bool = False,
    min_credits: float = 0,
    required_crns: Sequence[str | int] | str | None = None,
    required_courses: Sequence[str] | str | None = None,
    exclude_crns: Sequence[str | int] | str | None = None,
    exclude_courses: Sequence[str] | str | None = None,
    avoid_instructors: Sequence[str] | str | None = None,
    exclude_arrangement: bool = False,
    hours: dict[str, float] | None = None,
    credits: float | None = None,
    pins: Sequence[str] | str | None = None,
    courses: Sequence[str] | str | None = None,
    include_banner_plans: bool = True,
) -> dict:
    """ONE-CALL semester report (read-only): draft + conflicts + personal
    registration window + Banner Plan Ahead summaries. Canonical constraints are
    ``max_credits`` and ``required_courses``. ``credits`` is an alias for
    ``max_credits``; ``pins`` and ``courses`` append to ``required_courses``.
    Returns a compact plan (totals, applied max/min/hours, verified, selections
    with ratings/credits/covers/rationale/prereq_unverified), ``tba_crns`` /
    ``tba_sections``, ``prereq_unverified_crns``, grid, pinned/pin_errors/unplaced/
    warnings, a conflict summary, the registration window and Banner plan
    summaries, plus an executable ``next_action`` naming the CRNs. A window or
    Banner-plan failure is reported in its own field without hiding the draft.
    It never registers/adds/drops."""
    return _safe(
        semester_plan_payload,
        term=term,
        max_credits=max_credits,
        profile=profile,
        avoid_days=avoid_days,
        earliest=earliest,
        latest=latest,
        include_summer=include_summer,
        refresh=refresh,
        min_credits=min_credits,
        required_crns=required_crns,
        required_courses=required_courses,
        exclude_crns=exclude_crns,
        exclude_courses=exclude_courses,
        avoid_instructors=avoid_instructors,
        exclude_arrangement=exclude_arrangement,
        hours=hours,
        credits=credits,
        pins=pins,
        courses=courses,
        include_banner_plans=include_banner_plans,
    )


@mcp.tool(structured_output=False)
def unw_roadmap(
    start_term: str,
    terms: Sequence[str] | str | None = None,
    include_summer: bool = False,
    max_credits: float = 16,
    refresh: bool = False,
) -> dict:
    """Multi-term plan starting at ``start_term`` (default: the start term plus
    three non-summer terms; summer only when include_summer). Carries requirements
    and prerequisites forward, reports terms with no published catalog, and returns
    graduation-risk notes against the audit's anticipated graduation term. Course
    pins, minimum-load and instructor exclusions are draft-only: use
    unw_plan_draft for those."""
    return _safe(roadmap_payload, start_term, terms, include_summer, max_credits, refresh)


@mcp.tool(structured_output=False)
def unw_offering_matrix(
    courses: Sequence[str] | str | None = None, refresh: bool = False
) -> dict:
    """Offering pattern per course/attribute across every observed term: every-term,
    fall-only, spring-only, summer-only or never observed, with section counts,
    open counts and instructors per term. Accepts "CYS 4245", "ATTRIBUTE NATS" or
    "DAL *". Omit courses for every unmet requirement target. Use this before
    proposing a course for a future term."""
    return _safe(offering_matrix_payload, courses, refresh)


@mcp.tool(structured_output=False)
def unw_banner_plans(term: str) -> dict:
    """List the student's Banner Plan Ahead worksheets for one term. Plans are
    TERM-SCOPED: this list only contains plans whose planHeader.term equals
    ``term``; an empty list means "no plans for THIS term", not "no plans exist"
    (check other terms, and remember the session term must be bound)."""
    return _safe(plans_payload, term)


@mcp.tool(structured_output=False)
def unw_banner_plan_create(term: str, name: str, confirm: bool) -> dict:
    """Create a named Banner Plan Ahead worksheet for ``term`` (writes to Banner).

    Confirmation is ENFORCED, not advisory: ``confirm`` is required and must be
    true, otherwise the tool returns the intended effect and the current plan
    count without writing. Verify the effect with the user first. The student may
    hold at most five plans per term; names cannot be renamed later."""
    return _safe(plan_create_guarded, term, name, confirm)


@mcp.tool(structured_output=False)
def unw_banner_plan_add(
    term: str,
    plan: str | int,
    confirm: bool,
    crn: str | int | None = None,
    subject: str | None = None,
    number: str | int | None = None,
) -> dict:
    """Add a section (crn) or a course (subject+number) to a Banner plan (writes
    to Banner).

    Confirmation is ENFORCED: ``confirm`` is required and must be true, otherwise
    the tool names the exact section/course and current plan state and performs no
    write. ``plan`` is an id or exact name; ambiguous names are rejected with
    candidates. Successful writes return a before/after diff read back from Banner."""
    return _safe(plan_add_guarded, term, plan, confirm, crn, subject, number)


@mcp.tool(structured_output=False)
def unw_banner_plan_remove(
    term: str,
    plan: str | int,
    confirm: bool,
    crn: str | int | None = None,
    subject: str | None = None,
    number: str | int | None = None,
) -> dict:
    """Remove one course row from a Banner plan (writes to Banner).

    Confirmation is ENFORCED: ``confirm`` is required and must be true, otherwise
    the tool names the exact row and projected credits and performs no write.
    Identify the row by crn or subject+number; ``plan`` is an id or exact name.
    Successful writes return a before/after diff read back from Banner."""
    return _safe(plan_remove_guarded, term, plan, confirm, crn, subject, number)


@mcp.tool(structured_output=False)
def unw_banner_plan_delete(
    term: str, plan: str | int, confirm: bool, confirm_name: str
) -> dict:
    """Delete an entire Banner plan (writes to Banner, irreversible).

    Confirmation is ENFORCED twice: ``confirm`` must be true AND ``confirm_name``
    must equal the plan's exact name. Otherwise the tool reports the intended
    deletion and current plan state and performs no write. Successful deletion
    returns the removed plan, remaining plan count and a before/after diff read
    back from Banner."""
    return _safe(plan_delete_guarded, term, plan, confirm, confirm_name)


@mcp.tool(structured_output=False)
def unw_register_preview(
    term: str,
    crns: Sequence[str | int] | str | int,
    refresh: bool = False,
    hours: dict[str, float] | None = None,
) -> dict:
    """DRY RUN ONLY: never posts add/drop/register. Returns Banner's real term
    eligibility flags and failure text for ``term``, then local checks for the
    requested CRNs: seats/availability, time conflicts and prerequisites
    (``prereq_ok`` is null when unverified; minimum grade thresholds are
    enforced). ``hours`` sets the reserved credit hours per CRN and
    ``total_credits`` uses that same basis as the draft. If Banner reports the
    term as ineligible, treat its failure text as the answer; never attempt
    real registration."""
    return _safe(registration_preview_payload, term, crns, refresh, hours)


@mcp.tool(structured_output=False)
def unw_registration_window(term: str | None = None) -> dict:
    """READ-ONLY registration time-ticket window(s). With ``term``, returns that
    term's window; without it, returns the configured current term and the next
    selectable term. Each row carries Banner's verbatim eligibility failure text,
    parse_status (parsed/no_ticket/not_eligible/unknown), America/Chicago
    start/end, standing, source (banner_term/banner_prepare), seconds until
    opening and a countdown. A future ticket can exist while eligible=false.
    It NEVER registers, adds, drops or withdraws; parse_status=unknown must be
    reported from raw_text."""
    return _safe(registration_window_payload, term)


# --------------------------------------------------------------------------- #
# terms + entry point
# --------------------------------------------------------------------------- #
def _eligibility_for(client: SSBClient, term: str, refresh: bool) -> dict[str, Any]:
    if not refresh and term in _TERM_ELIGIBILITY:
        return _TERM_ELIGIBILITY[term]
    try:
        result = client.set_term(term, mode="registration")
        record = {
            "registration_eligible": result.student_elig_valid,
            "reg_allowed": result.reg_allowed,
            "failure": list(result.failures),
        }
    except Exception as exc:
        record = {
            "registration_eligible": None,
            "reg_allowed": None,
            "failure": [f"{type(exc).__name__}: {exc}"],
        }
    _TERM_ELIGIBILITY[term] = record
    return record


def terms_payload(refresh: bool = False) -> dict:
    if refresh:
        _TERM_ELIGIBILITY.clear()
    with _BANNER_LOCK:
        return _terms_payload_locked(refresh)


def _terms_payload_locked(refresh: bool) -> dict:
    client = SSBClient(auth.ensure())
    terms = client.get_terms()
    next_candidates = [
        term.code
        for term in terms
        if "view only" not in term.description.lower()
        and _term_key(term.code) > _term_key(CURRENT_TERM)
    ]
    next_code = min(next_candidates, key=_term_key) if next_candidates else None
    rows = []
    for term in terms:
        view_only = "view only" in term.description.lower()
        eligibility = (
            {"registration_eligible": None, "reg_allowed": None, "failure": []}
            if view_only
            else _eligibility_for(client, term.code, refresh)
        )
        rows.append(
            {
                "code": term.code,
                "description": term.description,
                "season": _SEASON_LABEL.get(term.code[4:6], "Unknown"),
                "academic_year": term.code[:4],
                "label": term_label(term.code),
                "is_next": term.code == next_code,
                "is_current": term.code == CURRENT_TERM,
                "view_only": view_only,
                "registration_eligible": eligibility["registration_eligible"],
                "reg_allowed": eligibility["reg_allowed"],
                "eligibility_failures": eligibility["failure"],
            }
        )
    return {
        "ok": True,
        "next_term": next_code,
        "current_term": CURRENT_TERM,
        "count": len(rows),
        "terms": rows,
        "note": (
            "registration_eligible is Banner's studentEligValid for mode=registration; "
            "null means the probe failed or the term is view-only."
        ),
    }


def main() -> None:
    """Run the stdio MCP server (console script ``eagle-flightplan-mcp``)."""
    os.chdir(REPO_ROOT)
    mcp.run(transport="stdio")


if __name__ == "__main__":
    main()
