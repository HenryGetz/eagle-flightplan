"""Fetch Degree Works audit JSON with the shared UNW session."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import httpx

from ..auth import MANUAL_LOGIN_COMMAND, AuthError, SessionExpiredError, ensure
from .models import Audit
from .parser import looks_like_html, parse_audit

AUDIT_ORIGIN = "https://degreeaudit.oci.unw.edu"
MYSELF_PATH = "/api/students/myself"
AUDIT_PATH = "/api/audit"


class AuditClient:
    """Degree Works API wrapper (read-only)."""

    def __init__(self, client: httpx.Client | None = None) -> None:
        self.client = client or ensure()
        self._student: dict[str, Any] | None = None

    # ------------------------------------------------------------------ student
    def student(self) -> dict[str, Any]:
        """The signed-in student's audit identity + goals."""
        response = self.client.get(
            AUDIT_ORIGIN + MYSELF_PATH,
            headers={"Accept": "application/json"},
        )
        payload = self._json(response, MYSELF_PATH)
        try:
            student = payload["_embedded"]["students"][0]
        except (KeyError, IndexError, TypeError) as exc:
            raise AuthError(f"Unexpected /api/students/myself payload: {payload!r}") from exc
        goal = (student.get("goals") or [{}])[0]
        self._student = {
            "id": student.get("id"),
            "name": student.get("name"),
            "active_term": student.get("activeTerm"),
            "school": (goal.get("school") or {}).get("key", "UG"),
            "degree": (goal.get("degree") or {}).get("key", "BS"),
            "catalog_year": (goal.get("catalogYear") or {}).get("key", ""),
            "goals": goal.get("details") or [],
        }
        return self._student

    # -------------------------------------------------------------------- audit
    def fetch(
        self,
        *,
        audit_type: str = "AA",
        include_in_progress: bool = True,
        include_preregistered: bool = True,
        audit_id: str = "",
        extra: dict[str, str] | None = None,
    ) -> dict[str, Any]:
        """Raw audit JSON (``/api/audit``)."""
        student = self._student or self.student()
        params = {
            "studentId": student["id"],
            "school": student["school"],
            "degree": student["degree"],
            "is-process-new": "false",
            "audit-type": audit_type,
            "auditId": audit_id,
            "include-inprogress": str(include_in_progress).lower(),
            "include-preregistered": str(include_preregistered).lower(),
            "aid-term": "",
        }
        if extra:
            params.update(extra)
        response = self.client.get(
            AUDIT_ORIGIN + AUDIT_PATH,
            params=params,
            headers={"Accept": "application/vnd.net.hedtech.degree+json, application/json"},
        )
        payload = self._json(response, AUDIT_PATH)
        if not isinstance(payload, dict) or "blockArray" not in payload:
            raise AuthError(
                "Audit endpoint did not return an audit document. "
                f"Keys: {list(payload)[:10] if isinstance(payload, dict) else type(payload).__name__}"
            )
        return payload

    def audit(self, **kwargs: Any) -> Audit:
        return parse_audit(self.fetch(**kwargs))

    # ------------------------------------------------------------------ helpers
    def save_raw(self, path: Path, payload: dict[str, Any] | None = None) -> Path:
        payload = payload or self.fetch()
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(payload, indent=1), encoding="utf-8")
        return path

    def _json(self, response: httpx.Response, path: str) -> Any:
        content_type = response.headers.get("content-type", "")
        if looks_like_html(response.text[:2000]) or "json" not in content_type:
            raise SessionExpiredError(
                f"{path} returned {response.status_code} {content_type or 'no content-type'} "
                "instead of JSON — the Degree Works session expired. "
                f"Run `{MANUAL_LOGIN_COMMAND}`."
            )
        return response.json()


def audit_for_session() -> Audit:
    return AuditClient().audit()
