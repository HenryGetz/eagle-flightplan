"""Banner planner ("Plan Ahead") API.

Everything here is a *plan-worksheet* write: nothing touches registration,
so no add/drop/register verb is ever sent.

Verified behaviour (live account, 2026-11-11)
---------------------------------------------
* Plans are term-scoped and the plan list is embedded in the ``selectPlan``
  page bootstrap (``plans: [...]``) — there is no JSON list endpoint.
* The editor session has one *selected* plan. ``create_plan`` leaves the new plan
  selected; otherwise selection follows the student's preferred plan.
* ``POST /plan/addPlanItem`` only validates and materialises a *draft* course
  object (``model``), with ``tuid``. It is persisted by the next
  ``POST /plan/submitPlan/batch`` with that object in ``create``.
* Removal is an ``update`` of the same object with
  ``selectedPlanAction="Delete"`` and ``isDeleteAction=true``.
* Duplicates are **not** rejected: adding the same CRN twice yields two rows
  with distinct ids. This client raises :class:`DuplicateCourseError` instead.
* ``destroy`` in the batch payload does not remove plan courses, and updating
  the header's ``description``/``headerDescription`` answers HTTP 500 — the UI
  has no rename that survives; use create/delete instead.
"""

from __future__ import annotations

import json
import re
import secrets
import string
import time
from typing import Any

import httpx
from pydantic import BaseModel, ConfigDict, Field

from ..catalog import Availability, availability_of
from ..models import Section as SnapshotSection
from ..schedule import find_conflicts

from ..auth import MANUAL_LOGIN_COMMAND, SessionExpiredError, ensure
from .ssb import SSBClient, SSBError

JSON_HEADERS = {
    "Accept": "application/json, text/javascript, */*; q=0.01",
    "Content-Type": "application/json",
    "X-Requested-With": "XMLHttpRequest",
}
FORM_HEADERS = {
    "Accept": "application/json, text/javascript, */*; q=0.01",
    "Content-Type": "application/x-www-form-urlencoded; charset=UTF-8",
    "X-Requested-With": "XMLHttpRequest",
}


class PlanError(SSBError):
    """Base class for planner failures."""


class PlanNotFoundError(PlanError):
    pass


class PlanSelectionError(PlanError):
    """The session's selected plan is not the plan the caller asked to modify."""


class PlanAheadUnavailable(PlanError):
    """The term is authenticated but not open for planning (no plan workspace)."""


class PlanWriteBlocked(PlanError):
    """A hard validation blocker (prerequisite, conflict or duplicate) stopped a write."""

    def __init__(self, issues: list["PlanIssue"]) -> None:
        self.issues = issues
        super().__init__("; ".join(issue.message for issue in issues))


class PlanIssue(BaseModel):
    kind: str
    message: str
    hard: bool = False


class DuplicateCourseError(PlanError):
    """The course is already in the plan (Banner would silently duplicate it)."""


def validate_sections(
    sections: list[SnapshotSection],
    *,
    passed_codes: set[str] | None = None,
    grades: dict[str, str] | None = None,
    current_term: str = "202610",
    prereqs: object | None = None,
) -> list[PlanIssue]:
    """Check a plan's rows for duplicate courses, seats, conflicts and prereqs."""
    issues: list[PlanIssue] = []
    seen: dict[str, str] = {}
    for section in sections:
        if section.code in seen:
            issues.append(PlanIssue(kind="duplicate", message=f"duplicate course {section.code} (CRN {seen[section.code]} and {section.crn})", hard=True))
        else:
            seen[section.code] = section.crn
        state = availability_of(section, current_term)
        if state is Availability.CLOSED:
            issues.append(PlanIssue(kind="availability", message=f"{section.code} CRN {section.crn} is closed (0 seats)"))
        elif state is Availability.NOT_YET_OPEN:
            issues.append(PlanIssue(kind="availability", message=f"{section.code} CRN {section.crn} is not yet open (0 seats); verify at registration"))
        if section.credits_low is not None and section.credits_high is not None and section.credits_low != section.credits_high:
            issues.append(PlanIssue(kind="variable_credits", message=f"{section.code} CRN {section.crn} is variable credit ({section.credits_low:g}-{section.credits_high:g}); set credit hours"))
    for conflict in find_conflicts([section.to_schedule_section() for section in sections]):
        issues.append(PlanIssue(kind="conflict", message=f"time conflict: {conflict.summary}", hard=True))
    if passed_codes is not None:
        provider = prereqs
        if provider is None:
            from ..planner import PrereqProvider as _PrereqProvider

            provider = _PrereqProvider()
        for section in sections:
            ok, why = provider.check(section, passed_codes, grades)
            if ok is False:
                issues.append(
                    PlanIssue(kind="prereq", message=f"{section.code} CRN {section.crn}: {why}", hard=True)
                )
            elif ok is None:
                issues.append(
                    PlanIssue(
                        kind="prereq_unverified",
                        message=f"{section.code} CRN {section.crn}: prerequisite unverified ({why})",
                        hard=False,
                    )
                )
    return issues


class PlanCourse(BaseModel):
    """One row of ``planHeader.planCourses``."""

    model_config = ConfigDict(extra="allow")

    id: int | None = None
    subject: str = ""
    number: str = ""
    title: str = ""
    crn: str | None = None
    term: str = ""
    credits: float | None = None
    status: str = ""
    section: str | None = None

    @classmethod
    def from_api(cls, payload: dict) -> "PlanCourse":
        return cls(
            id=payload.get("id"),
            subject=payload.get("subject") or "",
            number=payload.get("courseNumber") or "",
            title=payload.get("courseTitle") or "",
            crn=payload.get("courseReferenceNumber"),
            term=payload.get("term") or "",
            credits=payload.get("creditHours"),
            status=payload.get("planStatus") or "",
            section=payload.get("section"),
        )

    @property
    def code(self) -> str:
        return f"{self.subject} {self.number}".strip()


class Plan(BaseModel):
    model_config = ConfigDict(extra="allow")

    id: int
    name: str
    term: str
    term_description: str = ""
    sequence_number: int | None = None
    preferred: bool = False
    courses: list[PlanCourse] = Field(default_factory=list)
    total_planned_credit_hours: float = 0.0

    @classmethod
    def from_api(cls, payload: dict) -> "Plan":
        return cls(
            id=int(payload["id"]),
            name=payload.get("description") or "",
            term=payload.get("term") or "",
            term_description=payload.get("termDescription") or "",
            sequence_number=payload.get("sequenceNumber"),
            preferred=bool(payload.get("preferredIndicator")),
            courses=[PlanCourse.from_api(course) for course in payload.get("planCourses") or []],
            total_planned_credit_hours=float(payload.get("totalPlannedCreditHours") or 0),
        )


def _unique_session_id() -> str:
    prefix = "".join(secrets.choice(string.ascii_lowercase + string.digits) for _ in range(5))
    return f"{prefix}{int(time.time() * 1000)}"


def _extract_json_block(html: str, key: str) -> Any:
    """Pull a strict-JSON value out of a ``window.bootstraps = {...}`` literal."""
    marker = f"{key}:"
    start = html.find(marker)
    if start < 0:
        raise PlanError(f"Could not find '{key}:' in the plan page bootstrap.")
    cursor = start + len(marker)
    while cursor < len(html) and html[cursor] not in "[{":
        cursor += 1
    if cursor >= len(html):
        raise PlanError(f"Malformed bootstrap for '{key}'.")
    opener = html[cursor]
    closer = "]" if opener == "[" else "}"
    depth = 0
    in_string = False
    escaped = False
    for index in range(cursor, len(html)):
        char = html[index]
        if in_string:
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == '"':
                in_string = False
            continue
        if char == '"':
            in_string = True
        elif char == opener:
            depth += 1
        elif char == closer:
            depth -= 1
            if depth == 0:
                return json.loads(html[cursor : index + 1])
    raise PlanError(f"Unterminated bootstrap for '{key}'.")


class PlanClient:
    """Plan Ahead operations for the signed-in student."""

    def __init__(self, ssb: SSBClient | None = None) -> None:
        self.ssb = ssb or SSBClient(ensure())
        self.client = self.ssb.client
        self.current_term = "202610"

    # -------------------------------------------------------------------- reading
    def _set_plan_mode(self, term: str) -> None:
        self.ssb.set_term(term, mode="plan")

    def list_plans(self, term: str) -> list[Plan]:
        """All plans for a term (id, name, term, courses)."""
        self._set_plan_mode(term)
        page = self.ssb._request("GET", "/plan/selectPlan")
        if page.status_code != 200 or "html" not in page.headers.get("content-type", ""):
            raise PlanError(f"selectPlan returned HTTP {page.status_code}.")
        try:
            raw = _extract_json_block(page.text, "plans")
        except PlanError:
            guest = re.search(r'<meta name="fullName" content="\s*"', page.text)
            if guest:
                raise SessionExpiredError(
                    f"Banner returned the guest plan page — run `{MANUAL_LOGIN_COMMAND}`."
                ) from None
            raise PlanAheadUnavailable(
                f"Term {term} is not open for planning: Banner served the authenticated "
                "registration shell without a plan workspace (no plans block)."
            ) from None
        return [Plan.from_api(row) for row in raw]

    def get_plan(self, plan_id: int, term: str) -> Plan:
        for plan in self.list_plans(term):
            if plan.id == plan_id:
                return plan
        raise PlanNotFoundError(f"No plan {plan_id} in term {term}.")

    def current_plan(self) -> Plan:
        """The plan the editor session is bound to (``planHeader`` of the batch read)."""
        payload = self._batch()
        header = (payload.get("data") or {}).get("planHeader")
        if not header:
            raise PlanSelectionError("The session has no selected plan.")
        return Plan.from_api(header)

    # --------------------------------------------------------------------- writes
    def _batch(
        self,
        *,
        create: list[dict] | None = None,
        update: list[dict] | None = None,
        destroy: list[dict] | None = None,
    ) -> dict:
        body = {
            "create": create or [],
            "update": update or [],
            "destroy": destroy or [],
            "uniqueSessionId": _unique_session_id(),
        }
        response = self.ssb._request(
            "POST",
            "/plan/submitPlan/batch",
            content=json.dumps(body),
            headers=JSON_HEADERS,
            # Plan writes are never replayed: after an expired session we cannot
            # know whether the batch landed, so the caller must re-read and retry.
            idempotent=False,
            retry_on_expiry=False,
        )
        content_type = response.headers.get("content-type", "")
        if "json" not in content_type:
            raise SessionExpiredError(
                f"submitPlan/batch returned {response.status_code} "
                f"{content_type or 'no content-type'}; the Banner session expired. "
                f"Run `{MANUAL_LOGIN_COMMAND}`."
            )
        payload = response.json()
        if response.status_code >= 500 or payload.get("success") is False:
            raise PlanError(
                f"submitPlan/batch failed: {payload.get('message') or payload}"
            )
        return payload

    def create_plan(self, name: str, term: str) -> Plan:
        """Create a named plan; it becomes the session's selected plan."""
        if not name.strip():
            raise PlanError("Plan name must not be blank.")
        if len(name) > 80:
            raise PlanError("Banner caps plan names at 80 characters.")
        self._set_plan_mode(term)
        # The batch write needs the plan workspace initialised in the session.
        # A bare /plan/plan opens a *draft* planHeader; /plan/plan?select=1 fails
        # (HTTP 500 on the next write) when the student has no plan to select.
        self.ssb._request("GET", "/plan/plan")
        payload = self._batch(create=[{"headerDescription": name, "headerComment": ""}])
        header = (payload.get("data") or {}).get("planHeader") or {}
        if not header.get("id"):
            raise PlanError(f"Plan was not created: {payload}")
        created = Plan.from_api(header)
        # A new plan is *not* auto-selected when the student already has one.
        try:
            return self.select_plan(created.id, term)
        except PlanSelectionError:
            return created

    def select_plan(self, plan_id: int, term: str) -> Plan:
        """Bind an existing plan to the editor session.

        The UI's "Edit" button navigates to ``/plan/plan?select=<sequenceNumber>``,
        where the number is the plan's per-term ``sequenceNumber`` (1-based), not
        its database id. This performs that navigation and verifies the editor
        really holds ``plan_id`` before any write happens.
        """
        plan = self.get_plan(plan_id, term)
        if plan.sequence_number is None:
            raise PlanSelectionError(f"Plan {plan_id} has no sequenceNumber; cannot select it.")
        self._set_plan_mode(term)
        page = self.ssb._request("GET", f"/plan/plan?select={int(plan.sequence_number)}")
        if page.status_code != 200 or "selectedPlan" not in page.text:
            raise PlanSelectionError(
                f"Could not open plan {plan_id} (HTTP {page.status_code})."
            )
        current = self.current_plan()
        if current.id != plan_id:
            raise PlanSelectionError(
                f"Session is bound to plan {current.id} ({current.name!r}), not {plan_id}."
            )
        return current

    def validate(
        self,
        plan_id: int,
        term: str,
        *,
        passed_codes: set[str] | None = None,
        grades: dict[str, str] | None = None,
    ) -> list[PlanIssue]:
        from .search import load_snapshot
        from ..planner import PrereqProvider as _PrereqProvider

        plan = self.get_plan(plan_id, term)
        snapshot = load_snapshot(term)
        if snapshot is None:
            return [PlanIssue(kind="snapshot", message=f"no catalog snapshot for {term}; rows not validated")]
        sections = [snapshot.by_crn(course.crn) for course in plan.courses]
        present = [section for section in sections if section is not None]
        provider = _PrereqProvider() if passed_codes is not None else None
        return validate_sections(
            present,
            passed_codes=passed_codes,
            grades=grades,
            current_term=self.current_term,
            prereqs=provider,
        )

    def add_course(
        self,
        plan_id: int,
        term: str,
        *,
        subject: str | None = None,
        number: str | None = None,
        crn: str | None = None,
        allow_duplicate: bool = False,
        force: bool = False,
        passed_codes: set[str] | None = None,
        grades: dict[str, str] | None = None,
    ) -> PlanCourse:
        """Add a course (subject+number) or a specific section (CRN) to a plan.

        Passing ``crn`` pins the section; the stored row keeps
        ``courseReferenceNumber``. Passing ``subject``/``number`` adds the course
        without pinning a section (``courseReferenceNumber`` stays null).
        """
        if not crn and not (subject and number):
            raise PlanError("Pass crn, or subject and number.")
        current = self.select_plan(plan_id, term)
        wanted_crn = str(crn) if crn else None
        for existing in current.courses:
            same_course = (
                subject
                and existing.subject.upper() == subject.upper()
                and existing.number == str(number)
            )
            same_section = wanted_crn and existing.crn == wanted_crn
            if same_section or (same_course and not wanted_crn):
                if not allow_duplicate:
                    raise DuplicateCourseError(
                        f"{existing.code} (CRN {existing.crn or '-'}) is already in plan "
                        f"{plan_id}; Banner would create a duplicate row."
                    )

        if not force:
            from .search import load_snapshot

            snapshot = load_snapshot(term)
            if snapshot is not None:
                candidate = snapshot.by_crn(wanted_crn) if wanted_crn else next(
                    (s for s in snapshot.by_course(subject or "", str(number or "")) if s.open), None
                )
                if candidate is not None:
                    existing = [snapshot.by_crn(course.crn) for course in current.courses]
                    existing = [section for section in existing if section is not None]
                    provider = None
                    if passed_codes is not None:
                        from ..planner import PrereqProvider as _PrereqProvider

                        provider = _PrereqProvider()
                    issues = validate_sections(
                        existing + [candidate],
                        passed_codes=passed_codes,
                        grades=grades,
                        current_term=self.current_term,
                        prereqs=provider,
                    )
                    hard = [issue for issue in issues if issue.hard]
                    if hard:
                        raise PlanWriteBlocked(hard)

        if wanted_crn:
            form = {"term": term, "courseReferenceNumber": wanted_crn, "course": "section"}
        else:
            form = {"subject": subject, "courseNumber": str(number), "course": "course"}
        response = self.ssb._request(
            "POST", "/plan/addPlanItem", data=form, headers=FORM_HEADERS
        )
        draft = self._json_or_die(response, "/plan/addPlanItem")
        if not draft.get("success"):
            raise PlanError(f"addPlanItem rejected {form}: {draft.get('message')}")
        model = draft["model"]

        payload = self._batch(create=[model])
        header = (payload.get("data") or {}).get("planHeader") or {}
        courses = [PlanCourse.from_api(course) for course in header.get("planCourses") or []]
        added = next(
            (
                course
                for course in reversed(courses)
                if course.id not in {c.id for c in current.courses}
            ),
            None,
        )
        if added is None:
            raise PlanError("Banner accepted the save but no new plan course appeared.")
        return added

    def remove_course(
        self,
        plan_id: int,
        term: str,
        *,
        plan_course_id: int | None = None,
        crn: str | None = None,
        subject: str | None = None,
        number: str | None = None,
    ) -> Plan:
        """Remove one course row and return the updated plan."""
        current = self.select_plan(plan_id, term)
        target: PlanCourse | None = None
        for course in current.courses:
            if plan_course_id is not None and course.id == plan_course_id:
                target = course
                break
            if crn and course.crn == str(crn):
                target = course
                break
            if subject and number and course.subject.upper() == subject.upper() and course.number == str(number):
                target = course
                break
        if target is None:
            raise PlanNotFoundError(
                f"No matching course in plan {plan_id} "
                f"(id={plan_course_id}, crn={crn}, {subject} {number})."
            )
        raw = next(
            course
            for course in self._raw_plan_courses()
            if course.get("id") == target.id
        )
        update = dict(raw)
        update["selectedPlanAction"] = "Delete"
        update["isDeleteAction"] = True
        payload = self._batch(update=[update])
        header = (payload.get("data") or {}).get("planHeader") or {}
        return Plan.from_api(header)

    def delete_plan(self, plan_id: int, term: str) -> None:
        """Delete a plan. Banner wants the plan's ``sequenceNumber``, as an integer."""
        plan = self.get_plan(plan_id, term)
        if plan.sequence_number is None:
            raise PlanError(f"Plan {plan_id} has no sequenceNumber; cannot delete it.")
        self._set_plan_mode(term)
        response = self.ssb._request(
            "POST",
            "/plan/delete",
            data={"delete": int(plan.sequence_number)},
            headers=FORM_HEADERS,
            idempotent=False,
            retry_on_expiry=False,
        )
        payload = self._json_or_die(response, "/plan/delete")
        if not payload.get("success"):
            raise PlanError(f"Banner refused to delete plan {plan_id}: {payload.get('message')}")

    # --------------------------------------------------------------------- helpers
    def _raw_plan_courses(self) -> list[dict]:
        payload = self._batch()
        header = (payload.get("data") or {}).get("planHeader") or {}
        return list(header.get("planCourses") or [])

    def _json_or_die(self, response: httpx.Response, path: str) -> dict:
        content_type = response.headers.get("content-type", "")
        if "json" not in content_type:
            raise SessionExpiredError(
                f"{path} returned {response.status_code} {content_type or 'no content-type'}; "
                f"the Banner session expired. Run `{MANUAL_LOGIN_COMMAND}`."
            )
        return response.json()

