"""UNW planner MCP server (stdio) and shared tool implementation.

The module is both the MCP entry point (``unw-mcp`` / ``python -m
unw_planner.mcp_server``) and the single implementation behind the CLI's read
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

import logging
import os
import re
import threading
from datetime import datetime, timezone
from typing import Any, Callable, Sequence

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
from .banner.plan import Plan, PlanClient, PlanError, PlanNotFoundError
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
    availability_of,
    offering_history_attribute,
    offering_history_course,
    offering_matrix,
    requirements_from_audit,
    resolve,
    term_label,
)
from .config import REPO_ROOT, STATE_PATH, TERMS_DIR, get_settings
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
    parse_banner_time,
    render_grid,
)

SERVER_NAME = "unw-planner"
SERVER_VERSION = "0.1.0"

#: The term the student is currently enrolled in (used to distinguish "closed"
#: from "not open yet" in future terms). The planner uses the same default.
CURRENT_TERM = os.environ.get("UNW_CURRENT_TERM", "202610")

#: Exact command that repairs an expired/stale session in a fresh process.
FIX_COMMAND = (
    f"cd {REPO_ROOT} && {REPO_ROOT}/.venv/bin/python scripts/login.py --force"
)
FIX_COMMAND_ALT = f"uv run --project {REPO_ROOT} unwplanner login --force"

# Keep per-request HTTP chatter off the operator's console; tool payloads carry
# everything the agent needs.
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("httpcore").setLevel(logging.WARNING)

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
    return datetime.fromtimestamp(timestamp, tz=timezone.utc).isoformat()


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
        hint = "A required local file is missing; run `unwplanner doctor`."
    elif isinstance(exc, httpx.HTTPError):
        hint = "Network failure talking to UNW or RMP; retry once, then run `unwplanner doctor`."
    elif isinstance(exc, ValueError):
        hint = "Invalid argument; see the tool description for accepted values."
    return {"ok": False, "error": f"{kind}: {exc}", "error_type": kind, "hint": hint}


def _safe(fn: Callable[..., dict], /, *args: Any, **kwargs: Any) -> dict:
    try:
        return fn(*args, **kwargs)
    except Exception as exc:  # noqa: BLE001 - the tool boundary must not crash
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
        "variable_credits": section.credits_high is not None
        and section.credits_low is not None
        and section.credits_high != section.credits_low,
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
    payload: dict[str, Any] = {
        "ok": True,
        "authenticated": valid,
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
        "login_script": str(REPO_ROOT / "scripts" / "login.py"),
    }
    if valid:
        payload["message"] = "Banner and Degree Works sessions are live."
    else:
        payload["message"] = (
            "No usable session: the Banner page rendered as a guest or required a login. "
            "Tell the user to run the fix_command -- do not improvise a login."
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
        Priority.GEN_ED_REQUIRED.value: 2,
        Priority.GEN_ED_FILL.value: 3,
    }
    rows.sort(key=lambda row: (order.get(row["priority"], 9), row["group"], row["title"]))
    return {
        "ok": True,
        "student_name": audit.student_name,
        "count": len(rows),
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
    limit = max(0, limit)
    page = rows[offset : offset + limit] if limit else []
    return {
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
        "count": len(page),
        "catalog": _catalog_meta(snapshot),
        "sections": [section_payload(row) for row in page],
        "note": (
            "Filtering is client-side: Banner ignores txt_attribute/txt_instructor/"
            "chk_open_only on this deployment, so this tool filters the term snapshot."
        ),
    }


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


def candidates_payload(
    term: str,
    requirement: str | None = None,
    refresh: bool = False,
    open_only: bool = True,
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
        rows.append(
            {
                "requirement": _requirement_payload(
                    item, statuses.get(item.id, RequirementStatus.NOT_MET)
                ),
                "found": resolution.found,
                "reason": resolution.reason,
                "attribute_hits": resolution.attribute_hits,
                "count": len(resolution.candidates),
                "candidates": [
                    section_payload(candidate) for candidate in resolution.candidates
                ],
            }
        )
    return {
        "ok": True,
        "term": term,
        "label": term_label(term) if len(term) == 6 else term,
        "open_only": open_only,
        "requirement_count": len(rows),
        "catalog": _catalog_meta(snapshot),
        "requirements": rows,
    }


# --------------------------------------------------------------------------- #
# ratings / conflicts
# --------------------------------------------------------------------------- #
def ratings_payload(names: Sequence[str] | str) -> dict:
    wanted = _norm_names(names)
    if not wanted:
        raise ValueError("Pass at least one professor name.")
    provider = RatingProvider()
    rows = []
    for name in wanted:
        info = provider.get(name)
        row = rating_payload(info)
        row["query"] = name
        rows.append(row)
    return {
        "ok": True,
        "count": len(rows),
        "ratings": rows,
        "note": (
            "Unrated is null, not 0.0. n<3 ratings are weak evidence; match confidence "
            "reflects name ambiguity, not teaching quality."
        ),
    }


def conflicts_payload(
    term: str, crns: Sequence[str | int] | str | int, refresh: bool = False
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
                "kind": conflict.kind,
                "summary": conflict.summary,
            }
            for conflict in conflicts
        ],
        "conflict_summary": conflict_summary(conflicts),
        "total_credits": round(sum(section.credits or 0 for section in found), 2),
        "grid": render_grid(schedule_sections, include_tba=True) if found else "",
        "unscheduled": [
            section_payload(section) for section in found if section.unscheduled
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
) -> Constraints:
    days = [_parse_day(day) for day in _norm_list(avoid_days)]
    return Constraints(
        max_credits=float(max_credits),
        avoid_days=days,
        earliest=_parse_clock(earliest),
        latest=_parse_clock(latest),
    )


def _planner(
    terms: Sequence[str], refresh: bool = False
) -> tuple[Planner, dict[str, TermSnapshot], Audit, dict]:
    client = AuditClient()
    client.student()
    raw = client.fetch()
    audit = parse_audit(raw)
    snapshots: dict[str, TermSnapshot] = {}
    for term in terms:
        snapshots[term] = _snapshot(term, refresh)
    planner = Planner(
        audit,
        snapshots,
        ratings=RatingProvider(),
        prereqs=PrereqProvider(),
        current_term=CURRENT_TERM,
    )
    return planner, snapshots, audit, raw


def _draft_selection_payload(selection) -> dict:
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
        "seats": selection.seats,
        "open": selection.open,
        "availability": selection.availability,
        "verify_at_registration": selection.verify_at_registration,
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
    payload = {
        "term": draft.term,
        "label": draft.label or term_label(draft.term),
        "total_credits": draft.total_credits,
        "selection_count": len(draft.selections),
        "selections": [
            _draft_selection_payload(selection) for selection in draft.selections
        ],
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
        "score_breakdown": list(draft.score_breakdown),
        "avg_rmp_rating": average,
        "rated_picks": len(rated),
        "unrated_picks": sum(1 for row in draft.selections if not row.rating.rated),
    }
    if snapshot is not None:
        problems = verify_draft(draft, snapshot)
        payload["verify_problems"] = problems
        payload["verified"] = not problems
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
) -> dict:
    if profile not in {"max_rated"}:
        raise ValueError(
            f"Unknown profile {profile!r}; only 'max_rated' is implemented "
            "(the planner's ranking maximises RMP rating with unrated last)."
        )
    constraints = build_constraints(max_credits, avoid_days, earliest, latest)
    terms = [term]
    summer = _next_summer(term) if include_summer else None
    if summer:
        terms.append(summer)
    planner, snapshots, audit, _raw = _planner(terms, refresh)
    with _BANNER_LOCK:
        draft = planner.draft(term, constraints)
    payload: dict[str, Any] = {
        "ok": True,
        "requested": {
            "term": term,
            "max_credits": max_credits,
            "profile": profile,
            "avoid_days": [day.value for day in constraints.avoid_days],
            "earliest": constraints.earliest.strftime("%H:%M") if constraints.earliest else None,
            "latest": constraints.latest.strftime("%H:%M") if constraints.latest else None,
            "include_summer": include_summer,
        },
        "student": audit.student_name,
        "catalog": _catalog_meta(snapshots[term]),
        "draft": _draft_payload(draft, snapshots[term]),
    }
    if summer and summer in snapshots:
        placed = set()
        for selection in draft.selections:
            placed.add(selection.requirement_id)
            placed.update(selection.covers)
        remaining = [item for item in planner.requirements if item.id not in placed]
        with _BANNER_LOCK:
            summer_draft = planner.draft(summer, constraints, requirements=remaining)
        payload["summer"] = {
            "catalog": _catalog_meta(snapshots[summer]),
            "draft": _draft_payload(summer_draft, snapshots[summer]),
        }
    return payload


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
    planner, snapshots, audit, raw = _planner(planned_terms, refresh)
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
        "anticipated_graduation": anticipated,
        "student": audit.student_name,
        "catalogs": catalogs,
        "planned": rows,
        "final_remaining": roadmap.final_remaining,
        "risks": risks,
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
            "No cached term snapshots. Run `unwplanner search --term <term>` once, "
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
    return {
        "ok": True,
        "created": plan_payload(plan),
        "term": term,
        "note": "The new plan is term-scoped; read it back with unw_banner_plans(term).",
    }


def plan_add_payload(
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
        course = client.add_course(
            resolved.id,
            term,
            crn=str(crn) if crn is not None else None,
            subject=subject,
            number=str(number) if number is not None else None,
        )
        updated = client.get_plan(resolved.id, term)
    return {
        "ok": True,
        "term": term,
        "plan": plan_payload(updated),
        "added": _plan_course_payload(course),
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
        updated = client.remove_course(
            resolved.id,
            term,
            crn=str(crn) if crn is not None else None,
            subject=subject,
            number=str(number) if number is not None else None,
        )
    return {"ok": True, "term": term, "plan": plan_payload(updated)}


def plan_delete_payload(term: str, plan: str | int) -> dict:
    client = PlanClient()
    with _BANNER_LOCK:
        plans, available, unavailable_note = _list_plan_rows(client, term)
        if not available:
            raise PlanError(unavailable_note)
        resolved = _resolve_plan(plans, plan)
        client.delete_plan(resolved.id, term)
        remaining = []
        try:
            remaining = client.list_plans(term)
        except Exception:  # noqa: BLE001 - deletion already succeeded
            pass
    return {
        "ok": True,
        "term": term,
        "deleted": {"id": resolved.id, "name": resolved.name},
        "remaining_plans": len(remaining),
    }


# --------------------------------------------------------------------------- #
# registration preview (read-only)
# --------------------------------------------------------------------------- #
def registration_preview_payload(
    term: str, crns: Sequence[str | int] | str | int, refresh: bool = False
) -> dict:
    """Dry run: Banner eligibility + local conflict/seat/prereq checks."""
    wanted = [str(item).strip() for item in _norm_list(crns)]
    if not wanted:
        raise ValueError("Pass at least one CRN.")
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
    prereq_note = ""
    try:
        passed = applied_codes(AuditClient().audit())
    except Exception as exc:  # noqa: BLE001 - preview must still report eligibility
        passed = None
        prereq_note = f"prerequisite check skipped: audit unavailable ({type(exc).__name__})"
    prereq_rows = []
    if passed is not None:
        provider = PrereqProvider(ssb=ssb)
        with _BANNER_LOCK:
            for section in sections:
                ok, reason = provider.check(section, passed)
                prereq_rows.append(
                    {
                        "crn": section.crn,
                        "course": section.code,
                        "prereq_ok": ok,
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
            "; ".join(failures) if failures else "Banner reports this term as registration-eligible."
        ),
        "sections": [section_payload(section) for section in sections],
        "missing_crns": missing,
        "conflict_count": len(conflicts),
        "conflicts": [conflict.summary for conflict in conflicts],
        "grid": render_grid(schedule_sections, include_tba=True) if sections else "",
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
# MCP server
# --------------------------------------------------------------------------- #
INSTRUCTIONS = """\
UNW planner: Degree Works audit + Banner 9 class search + planning tools.

START HERE
- Call unw_session() first. If authenticated=false, tell the user to run the exact
  fix_command it returns (scripts/login.py --force, which handles TOTP). Never
  improvise a login, never ask the user for a password or TOTP code.
- Then unw_profile() and unw_remaining(). The audit already knows the student's
  majors, minors, catalog year, classifications and advisors: never ask the student
  for facts the audit can answer.
- DO confirm declared programs against what the student says. They diverge often
  (e.g. a student says "ministry minor" while the audit says "Bible"). Report the
  discrepancy, do not silently pick one.

ASK BEFORE ASSUMING
- Credit budget, blocked days/times, earliest start, latest end, summer
  willingness, and graduation target. unw_roadmap and unw_plan_draft have
  defaults; defaults are not the student's preferences.

PLANNING ORDER
1. Scarce major-required courses first. Check unw_offering_matrix before proposing
   any course: never propose a fall-only course for spring (or vice versa).
2. Then the major credit pool / electives.
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
- Before any create/add/remove/delete, read the plan list, show the exact change,
  and get explicit user confirmation. Add and remove need the plan id or exact
  name; ambiguous names are rejected with candidates, not guessed.
- Do not create/delete plans just to test something: the student's plans are real.

REGISTRATION
- unw_register_preview is a DRY RUN. It never POSTs add/drop/register. Registration
  is currently ineligible for every term; report Banner's eligibility failure text
  verbatim instead of implying registration can proceed.

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
    """Check the UNW session: authenticated flag, state-file age, cookie count and
    earliest cookie expiry (values are never exposed), plus the exact command that
    repairs an expired session. Call this before any other tool."""
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
    pick-N progress, course/attribute/wildcard options, status and priority class.
    This is the authoritative "what is left" list."""
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
) -> dict:
    """Search one term's sections (normalised rows: CRN, course, title, credits,
    seats, days/times, faculty, attributes, availability). Banner ignores the
    attribute/instructor/open-only query parameters, so filtering happens
    client-side over a cached term snapshot. Paginate with limit/offset."""
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
    )


@mcp.tool(structured_output=False)
def unw_course(term: str, crn: str | int, refresh: bool = False) -> dict:
    """Full detail for one CRN: meetings, faculty with RMP evidence, attributes,
    catalog description, prerequisite/corequisite/restriction text plus the parsed
    prerequisite codes, linked lecture/lab CRNs, enrollment text and availability."""
    return _safe(course_payload, term, crn, refresh)


@mcp.tool(structured_output=False)
def unw_candidates(
    term: str, requirement: str | None = None, refresh: bool = False
) -> dict:
    """Resolve unmet requirements to sections offered in ``term`` (wraps the
    catalog resolver). ``requirement`` matches an id, title, parent group or
    target; omit it for every unmet requirement. Every empty result carries the
    reason (never offered / all closed / already completed)."""
    return _safe(candidates_payload, term, requirement, refresh)


@mcp.tool(structured_output=False)
def unw_ratings(names: Sequence[str] | str) -> dict:
    """Batch Rate My Professors lookup. Accepts "Last, First" or "First Last".
    Unrated professors return rating=null (never 0.0); weak_evidence flags n<3."""
    return _safe(ratings_payload, names)


@mcp.tool(structured_output=False)
def unw_conflicts(
    term: str, crns: Sequence[str | int] | str | int, refresh: bool = False
) -> dict:
    """Pairwise time conflicts for a CRN set, plus the ASCII week grid and the list
    of unscheduled (TBA/asynchronous) sections. Half-open intervals; linked
    lecture/lab pairs are exempt."""
    return _safe(conflicts_payload, term, crns, refresh)


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
) -> dict:
    """Draft a conflict-free term plan from the audit, prioritizing major-required
    courses, then major pools, then gen-ed by RMP rating. Returns per-pick
    rationale, credits, average RMP, ASCII grid, verification problems and every
    unplaced requirement with its reason. include_summer=True also drafts the
    following summer for requirements that did not fit."""
    return _safe(
        draft_payload,
        term,
        max_credits,
        profile,
        avoid_days,
        earliest,
        latest,
        include_summer,
        refresh,
    )


@mcp.tool(structured_output=False)
def unw_roadmap(
    start_term: str,
    terms: Sequence[str] | str | None = None,
    include_summer: bool = False,
    max_credits: float = 16,
    refresh: bool = False,
) -> dict:
    """Multi-term plan starting at ``start_term`` (default: the next four
    non-summer terms; summer only when include_summer). Carries requirements and
    prerequisites forward, reports terms with no published catalog, and returns
    graduation-risk notes against the audit's anticipated graduation term."""
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
def unw_banner_plan_create(term: str, name: str) -> dict:
    """Create a named Banner Plan Ahead worksheet for ``term`` (writes to Banner).
    Only call after explicit user confirmation; the student may hold at most five
    plans per term. Names cannot be renamed later -- create/delete instead."""
    return _safe(plan_create_payload, term, name)


@mcp.tool(structured_output=False)
def unw_banner_plan_add(
    term: str,
    plan: str | int,
    crn: str | int | None = None,
    subject: str | None = None,
    number: str | int | None = None,
) -> dict:
    """Add a section (crn) or a course (subject+number) to a Banner plan (writes
    to Banner). ``plan`` is a plan id or exact name; ambiguous names are rejected
    with candidates. Confirm the exact change with the user first."""
    return _safe(plan_add_payload, term, plan, crn, subject, number)


@mcp.tool(structured_output=False)
def unw_banner_plan_remove(
    term: str,
    plan: str | int,
    crn: str | int | None = None,
    subject: str | None = None,
    number: str | int | None = None,
) -> dict:
    """Remove one course row from a Banner plan (writes to Banner). Identify the
    row by crn or subject+number; ``plan`` is an id or exact name. Confirm with
    the user first and re-read the plan afterwards."""
    return _safe(plan_remove_payload, term, plan, crn, subject, number)


@mcp.tool(structured_output=False)
def unw_banner_plan_delete(term: str, plan: str | int) -> dict:
    """Delete an entire Banner plan (writes to Banner, irreversible). ``plan`` is
    an id or exact name. Only call after explicit user confirmation; then report
    how many plans remain for the term."""
    return _safe(plan_delete_payload, term, plan)


@mcp.tool(structured_output=False)
def unw_register_preview(
    term: str, crns: Sequence[str | int] | str | int, refresh: bool = False
) -> dict:
    """DRY RUN ONLY: never posts add/drop/register. Returns Banner's real term
    eligibility flags and failure text for ``term``, then local checks for the
    requested CRNs: seats/availability, time conflicts and prerequisites. Since
    registration is currently ineligible for every term, treat the eligibility
    failure text as the answer and do not attempt real registration."""
    return _safe(registration_preview_payload, term, crns, refresh)


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
    except Exception as exc:  # noqa: BLE001 - one bad term must not hide the list
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
    """Run the stdio MCP server (console script ``unw-mcp``)."""
    os.chdir(REPO_ROOT)
    mcp.run(transport="stdio")


if __name__ == "__main__":
    main()
