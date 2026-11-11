"""Banner 9 Student Self-Service client.

All calls are plain HTTP against ``/StudentRegistrationSsb/ssb`` using the cookies
captured by :mod:`unw_planner.auth`. No browser is required after login.

Endpoint map (see ``docs/banner-api.md`` for the full table):

===============================  ====  ===========================================
path                             verb  purpose
===============================  ====  ===========================================
``/classSearch/getTerms``        GET   selectable terms
``/term/search?mode=<mode>``     POST  set the session term; returns eligibility
``/term/saveTerm?mode=<mode>``   POST  persist the term for the session
``/searchResults/searchResults`` GET   class search (see note on filters)
``/searchResults/getFacultyMeetingTimes``  GET  meeting times (JSON)
``/searchResults/getSectionAttributes``    GET  attributes (HTML fragment)
``/searchResults/getSectionPrerequisites`` GET  prerequisites (HTML fragment)
``/searchResults/getCorequisites``         GET  corequisites (HTML fragment)
``/searchResults/getRestrictions``         GET  restrictions (HTML fragment)
``/searchResults/getCourseDescription``    GET  catalog text (HTML fragment)
``/searchResults/getEnrollmentInfo``       GET  seat/waitlist counts (HTML)
``/searchResults/fetchLinkedSections``     GET  linked lecture/lab CRNs (JSON)
===============================  ====  ===========================================
"""

from __future__ import annotations

import re
from typing import Any, Iterable, Literal

import httpx
from bs4 import BeautifulSoup

from ..auth import AuthError, SessionExpiredError, ensure, httpx_client
from .models import (
    Faculty,
    LinkedSection,
    MeetingTime,
    SearchPage,
    Section,
    SectionAttribute,
    SectionDetail,
    Term,
)

SSBError = AuthError

MODE = Literal["search", "registration", "plan", "preReg"]


class TermNotSelectedError(SSBError):
    """A call that requires a session term was made before ``set_term``."""


class NotEligibleError(SSBError):
    """The student is not eligible to register for the requested term."""


class TermEligibility(dict):
    """Raw ``POST /term/search`` payload with attribute access for the flags."""

    @property
    def reg_allowed(self) -> bool:
        return bool(self.get("regAllowed"))

    @property
    def student_elig_valid(self) -> bool:
        return bool(self.get("studentEligValid"))

    @property
    def failures(self) -> list[str]:
        return list(self.get("studentEligFailures") or [])

    @property
    def fwd_url(self) -> str:
        return self.get("fwdURL") or ""


def _html_to_text(html: str) -> str:
    soup = BeautifulSoup(html, "lxml")
    for tag in soup(["script", "style"]):
        tag.decompose()
    text = soup.get_text("\n")
    lines = [re.sub(r"\s+", " ", line).strip() for line in text.splitlines()]
    return "\n".join(line for line in lines if line)


class SSBClient:
    """Thin, typed wrapper over an authenticated httpx client."""

    def __init__(self, client: httpx.Client | None = None) -> None:
        self.client = client or ensure()
        self._term: str | None = None

    # ------------------------------------------------------------------ plumbing
    def _get_json(self, path: str, params: dict[str, Any] | None = None) -> Any:
        response = self.client.get(
            path,
            params=params,
            headers={"Accept": "application/json, text/javascript, */*; q=0.01"},
        )
        return self._json(response, path)

    def _json(self, response: httpx.Response, path: str) -> Any:
        content_type = response.headers.get("content-type", "")
        if "json" not in content_type:
            snippet = response.text[:200].replace("\n", " ")
            raise SessionExpiredError(
                f"{path} returned {response.status_code} {content_type or 'no content-type'} "
                f"instead of JSON — the Banner session likely expired (run login again). "
                f"Body starts: {snippet!r}"
            )
        if response.status_code >= 400:
            raise SSBError(f"{path} failed with HTTP {response.status_code}: {response.text[:400]}")
        return response.json()

    def _get_html(self, path: str, params: dict[str, Any]) -> str:
        response = self.client.get(path, params=params)
        if response.status_code >= 400 or "html" not in response.headers.get("content-type", ""):
            raise SSBError(
                f"{path} failed with HTTP {response.status_code}: {response.text[:300]}"
            )
        return response.text

    # --------------------------------------------------------------------- terms
    def get_terms(self) -> list[Term]:
        """Every term the student may select, newest first."""
        payload = self._get_json(
            "/classSearch/getTerms",
            {"searchTerm": "", "offset": "1", "max": "50"},
        )
        return [Term(code=row["code"], description=row["description"]) for row in payload]

    def next_term(self) -> Term:
        """The soonest term that is not marked view-only / past."""
        for term in self.get_terms():
            if "view only" not in term.description.lower():
                return term
        raise SSBError("No bookable term in the term list.")

    def set_term(self, term: str, mode: MODE = "search") -> TermEligibility:
        """Bind the term to the session (the usual ``POST /ssb/term/search``)."""
        response = self.client.post(
            f"/term/search?mode={mode}",
            data={
                "term": term,
                "studyPath": "",
                "studyPathText": "",
                "startDatepicker": "",
                "endDatepicker": "",
            },
            headers={"Accept": "application/json, text/javascript, */*; q=0.01"},
        )
        payload = self._json(response, "/term/search")
        self._term = term
        return TermEligibility(payload)

    def require_term(self, term: str | None = None) -> str:
        resolved = term or self._term
        if not resolved:
            raise TermNotSelectedError(
                "No term is set in this session. Call set_term('202620') first."
            )
        if self._term != resolved:
            self.set_term(resolved)
        return resolved

    # -------------------------------------------------------------------- search
    def search(
        self,
        *,
        term: str | None = None,
        subject: str = "",
        course_number: str = "",
        course_title: str = "",
        page_offset: int = 0,
        page_max_size: int = 50,
        sort_column: str = "subjectDescription",
        sort_direction: str = "asc",
        open_only: bool = False,
        extra_params: dict[str, str] | None = None,
    ) -> SearchPage:
        """Class search.

        Banner honours ``txt_subject``, ``txt_courseNumber`` and ``txt_term``.
        The other filter widgets (level, campus, instructor, attributes, open
        only) are sent by the UI but are *not* applied server-side on this
        deployment — verified by identical totals with and without them. This
        method therefore filters ``open_only`` client-side and leaves the rest to
        the caller via the returned fields.
        """
        resolved = self.require_term(term)
        params: dict[str, Any] = {
            "txt_subject": subject,
            "txt_courseNumber": course_number,
            "txt_courseTitle": course_title,
            "txt_term": resolved,
            "startDatepicker": "",
            "endDatepicker": "",
            "pageOffset": str(page_offset),
            "pageMaxSize": str(page_max_size),
            "sortColumn": sort_column,
            "sortDirection": sort_direction,
        }
        if extra_params:
            params.update(extra_params)
        page = SearchPage.from_api(self._get_json("/searchResults/searchResults", params))
        if open_only:
            page.sections = [section for section in page.sections if section.open_section]
        return page

    # ------------------------------------------------------------ section detail
    def meeting_times(self, crn: str, term: str | None = None) -> list[MeetingTime]:
        resolved = self.require_term(term)
        payload = self._get_json(
            "/searchResults/getFacultyMeetingTimes",
            {"term": resolved, "courseReferenceNumber": crn},
        )
        entries = payload.get("fmt") or []
        meetings: list[MeetingTime] = []
        for entry in entries:
            meeting = entry.get("meetingTime")
            if meeting:
                meetings.append(MeetingTime.from_api(meeting))
        return meetings

    def faculty(self, crn: str, term: str | None = None) -> list[Faculty]:
        resolved = self.require_term(term)
        payload = self._get_json(
            "/searchResults/getFacultyMeetingTimes",
            {"term": resolved, "courseReferenceNumber": crn},
        )
        people: list[Faculty] = []
        for entry in payload.get("fmt") or []:
            for person in entry.get("faculty") or []:
                people.append(Faculty.from_api(person))
        return people

    def _fragment_lines(self, path: str, crn: str, term: str) -> list[str]:
        html = self._get_html(path, {"term": term, "courseReferenceNumber": crn})
        return _html_to_text(html).splitlines()

    def prerequisites(self, crn: str, term: str | None = None) -> list[str]:
        resolved = self.require_term(term)
        return self._fragment_lines("/searchResults/getSectionPrerequisites", crn, resolved)

    def corequisites(self, crn: str, term: str | None = None) -> list[str]:
        resolved = self.require_term(term)
        return self._fragment_lines("/searchResults/getCorequisites", crn, resolved)

    def restrictions(self, crn: str, term: str | None = None) -> list[str]:
        resolved = self.require_term(term)
        return self._fragment_lines("/searchResults/getRestrictions", crn, resolved)

    def attributes(self, crn: str, term: str | None = None) -> list[SectionAttribute]:
        resolved = self.require_term(term)
        lines = self._fragment_lines("/searchResults/getSectionAttributes", crn, resolved)
        out: list[SectionAttribute] = []
        for line in lines:
            match = re.match(r"^(?P<desc>.*?)\s+(?P<code>[A-Z0-9]{3,6})$", line)
            if match and len(match.group("desc")) > 2:
                out.append(SectionAttribute(code=match.group("code"), description=match.group("desc")))
        return out

    def description(self, crn: str, term: str | None = None) -> str:
        resolved = self.require_term(term)
        return "\n".join(self._fragment_lines("/searchResults/getCourseDescription", crn, resolved))

    def enrollment_text(self, crn: str, term: str | None = None) -> str:
        resolved = self.require_term(term)
        return "\n".join(self._fragment_lines("/searchResults/getEnrollmentInfo", crn, resolved))

    def linked_sections(self, crn: str, term: str | None = None) -> list[LinkedSection]:
        resolved = self.require_term(term)
        payload = self._get_json(
            "/searchResults/fetchLinkedSections",
            {"term": resolved, "courseReferenceNumber": crn},
        )
        return [LinkedSection.from_api(row) for row in payload.get("linkedData") or []]

    def section_detail(
        self,
        crn: str,
        term: str | None = None,
        *,
        section: Section | object | None = None,
    ) -> SectionDetail:
        """Full detail bundle for one CRN (meeting times + HTML fragments + links).

        ``section`` may be a banner wire row or a normalized snapshot Section; when
        omitted the term snapshot is consulted first (a 100-row page search is not
        enough for a ~900-section term).
        """
        resolved = self.require_term(term)
        if section is None:
            section = self._snapshot_section(crn, resolved)
        if section is None:
            page = self.search(term=resolved, page_max_size=500)
            section = next(
                (candidate for candidate in page.sections if candidate.crn == crn),
                None,
            )
        return SectionDetail(
            crn=crn,
            term=resolved,
            section=section,
            meeting_times=self.meeting_times(crn, resolved),
            faculty=self.faculty(crn, resolved),
            prerequisites=_drop_headings(self.prerequisites(crn, resolved)),
            corequisites=_drop_headings(self.corequisites(crn, resolved)),
            restrictions=_drop_headings(self.restrictions(crn, resolved)),
            description=self.description(crn, resolved),
            attributes=self.attributes(crn, resolved),
            linked_sections=self.linked_sections(crn, resolved),
            enrollment_text=self.enrollment_text(crn, resolved),
        )

    @staticmethod
    def _snapshot_section(crn: str, term: str) -> object | None:
        from .search import load_snapshot

        snapshot = load_snapshot(term)
        if snapshot is None:
            return None
        return snapshot.by_crn(crn)

    # ----------------------------------------------------------------- schedules
    def sections_for_crns(
        self, crns: Iterable[str], term: str | None = None, *, subject: str
    ) -> list[Section]:
        """Resolve CRNs to search rows, searching one subject (Banner needs one)."""
        resolved = self.require_term(term)
        wanted = {str(crn) for crn in crns}
        page = self.search(term=resolved, subject=subject, page_max_size=200)
        return [section for section in page.sections if section.crn in wanted]


def _drop_headings(lines: list[str]) -> list[str]:
    return [line for line in lines if line and line.lower() not in {"catalog prerequisites", "corequisites", "prerequisites"}]


def client_for_session() -> SSBClient:
    """Convenience: reusable client bound to the cached login."""
    return SSBClient(httpx_client())
