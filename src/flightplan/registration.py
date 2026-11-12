"""Registration time-ticket detection (read-only).

Banner exposes the window only through the registration eligibility payload:
``POST /ssb/term/search?mode=registration`` returns ``studentEligValid`` and
``studentEligFailures``. The failures carry the window in prose (e.g. "You can
register from 04/20/2026 08:30 AM to 10/28/2026 11:59 PM"). We parse that text
into aware America/Chicago datetimes and never invent a window that is not there.
"""

from __future__ import annotations

import re
from datetime import datetime
from typing import Iterable, Literal
from zoneinfo import ZoneInfo

from pydantic import BaseModel

from .banner.ssb import SSBClient

TIMEZONE = "America/Chicago"
TZ = ZoneInfo(TIMEZONE)

DATE_PAIR = re.compile(
    r"(?P<date>\d{1,2}/\d{1,2}/\d{4})\s+(?P<time>\d{1,2}:\d{2}\s*(?:AM|PM))",
    re.IGNORECASE,
)

TIME_TICKET_MARKERS = (
    "no registration time ticket",
    "term not eligible for registration",
    "registration is closed",
    "may not register at this time",
    "not eligible to register",
)
OPEN_MARKERS = ("you can register from", "you may register from", "registration opens", "registration begins")


class RegistrationWindow(BaseModel):
    term: str
    eligible: bool
    start: datetime | None = None
    end: datetime | None = None
    reason: str = ""
    raw_text: str = ""
    timezone: str = TIMEZONE
    source: str = "POST /ssb/term/search?mode=registration"
    parse_status: Literal["parsed", "no_ticket", "not_eligible", "unknown"] = "unknown"

    @property
    def opens_in_seconds(self) -> float | None:
        if self.start is None:
            return None
        return (self.start - datetime.now(TZ)).total_seconds()


def _parse_pair(match: re.Match[str]) -> datetime:
    time_text = re.sub(r"\s+", " ", match.group("time")).upper()
    stamp = match.group("date") + " " + time_text
    return datetime.strptime(stamp, "%m/%d/%Y %I:%M %p").replace(tzinfo=TZ)


def parse_failures(term: str, failures: Iterable[str], eligible: bool) -> RegistrationWindow:
    """Parse Banner's eligibility prose into a window; unknown text never raises."""
    messages = [str(message).strip() for message in failures if str(message).strip()]
    raw = "; ".join(messages)
    lowered = raw.lower()
    pairs = [_parse_pair(match) for match in DATE_PAIR.finditer(raw)]
    start = pairs[0] if len(pairs) >= 1 else None
    end = pairs[1] if len(pairs) >= 2 else None

    if eligible:
        return RegistrationWindow(
            term=term,
            eligible=True,
            start=start,
            end=end,
            reason=raw or "eligible",
            raw_text=raw,
            parse_status="parsed" if start else "no_ticket",
        )
    if pairs:
        return RegistrationWindow(
            term=term,
            eligible=False,
            start=start,
            end=end,
            reason=raw,
            raw_text=raw,
            parse_status="parsed",
        )
    if any(marker in lowered for marker in TIME_TICKET_MARKERS):
        status: Literal["no_ticket", "not_eligible"] = (
            "no_ticket" if "time ticket" in lowered else "not_eligible"
        )
        return RegistrationWindow(
            term=term, eligible=False, reason=raw or "not eligible", raw_text=raw, parse_status=status
        )
    return RegistrationWindow(
        term=term,
        eligible=False,
        reason=raw or "Banner returned no eligibility text",
        raw_text=raw,
        parse_status="unknown",
    )


def window(term: str, *, ssb: SSBClient | None = None) -> RegistrationWindow:
    """Bind a term for registration and parse its eligibility window."""
    client = ssb or SSBClient()
    payload = client.set_term(term, mode="registration")
    failures = payload.get("studentEligFailures") or []
    eligible = bool(payload.get("studentEligValid"))
    return parse_failures(term, failures, eligible)


def windows(terms: Iterable[str] | None = None, *, ssb: SSBClient | None = None) -> list[RegistrationWindow]:
    client = ssb or SSBClient()
    if terms is None:
        terms = [term.code for term in client.get_terms() if "view only" not in term.description.lower()]
    return [window(term, ssb=client) for term in terms]


def next_window(terms: Iterable[str] | None = None, *, ssb: SSBClient | None = None) -> RegistrationWindow | None:
    """The currently-open window if any, else the soonest upcoming start."""
    found = windows(terms, ssb=ssb)
    open_now = [item for item in found if item.eligible]
    if open_now:
        return open_now[0]
    upcoming = [item for item in found if item.start is not None and item.start >= datetime.now(TZ)]
    return min(upcoming, key=lambda item: item.start) if upcoming else None


def countdown(seconds: float | None) -> str:
    if seconds is None:
        return "unknown"
    if seconds < 1:
        return "now"
    total = int(seconds)
    days, rest = divmod(total, 86400)
    hours, rest = divmod(rest, 3600)
    minutes, secs = divmod(rest, 60)
    parts = []
    if days:
        parts.append(f"{days}d")
    if hours or days:
        parts.append(f"{hours}h")
    parts.append(f"{minutes}m")
    if not days and not hours:
        parts.append(f"{secs}s")
    return " ".join(parts)
