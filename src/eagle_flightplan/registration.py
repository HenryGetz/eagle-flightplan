"""Registration time-ticket detection (read-only).

Two Banner sources are parsed, in order:

1. ``POST /ssb/term/search?mode=registration`` — ``studentEligValid`` plus
   ``studentEligFailures`` prose (e.g. "You can register from 04/20/2026 08:30 AM
   to 10/28/2026 11:59 PM"). This is the eligibility answer.
2. ``GET /ssb/prepareRegistration/prepareRegistration`` — the server-rendered
   *Prepare for Registration* page. For a future term Banner still says "Term not
   eligible for registration", but this page already shows the assigned personal
   ticket ("Please register within these times: ..."). It never makes a term
   eligible; it only supplies the window.

All datetimes are aware America/Chicago. A window is never invented: if neither
source contains one, ``start`` stays ``None`` and the verbatim Banner text is kept.
"""

from __future__ import annotations

import re
from datetime import datetime
from typing import Iterable, Literal
from zoneinfo import ZoneInfo

from bs4 import BeautifulSoup
from pydantic import BaseModel

from .banner.ssb import SSBClient

TIMEZONE = "America/Chicago"
TZ = ZoneInfo(TIMEZONE)

DATE_PAIR = re.compile(
    r"(?P<date>\d{1,2}/\d{1,2}/\d{4})\s+(?P<time>\d{1,2}:\d{2}\s*(?:AM|PM))",
    re.IGNORECASE,
)

#: The Prepare for Registration page renders the window in its own paragraph.
PREPARE_WINDOW = re.compile(
    r"Please register within these times:\s*"
    r"(?P<start>\d{1,2}/\d{1,2}/\d{4}\s+\d{1,2}:\d{2}\s*(?:AM|PM))\s*-\s*"
    r"(?P<end>\d{1,2}/\d{1,2}/\d{4}\s+\d{1,2}:\d{2}\s*(?:AM|PM))",
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
    source: Literal["banner_term", "banner_prepare"] = "banner_term"
    standing: str | None = None
    institution_hours: int | None = None
    transfer_hours: int | None = None
    time_ticket_text: str = ""
    parse_status: Literal["parsed", "no_ticket", "not_eligible", "unknown"] = "unknown"

    @property
    def opens_in_seconds(self) -> float | None:
        if self.start is None:
            return None
        return (self.start - datetime.now(TZ)).total_seconds()


def _parse_stamp(stamp: str) -> datetime:
    normalized = re.sub(r"\s+", " ", stamp).strip().upper()
    return datetime.strptime(normalized, "%m/%d/%Y %I:%M %p").replace(tzinfo=TZ)


def _parse_pair(match: re.Match[str]) -> datetime:
    return _parse_stamp(match.group("date") + " " + match.group("time"))


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


def parse_prepare_html(html: str, term: str = "") -> RegistrationWindow:
    """Parse the *Prepare for Registration* HTML for the personal ticket.

    The HTML is never stored; only the ticket text, window, standing and hours are
    returned. A page without a ticket is not an error: ``start`` stays ``None``
    and ``parse_status`` is ``no_ticket``/``unknown``.
    """
    soup = BeautifulSoup(html or "", "lxml")
    for tag in soup(["script", "style"]):
        tag.decompose()
    text = " ".join(soup.get_text(" ", strip=True).split())

    window_match = PREPARE_WINDOW.search(text)
    standing_match = re.search(
        r"class standing for registration purposes is ([A-Za-z]+)", text, re.IGNORECASE
    )
    hours_match = re.search(
        r"Institution Hours:\s*(\d+),\s*Transfer Hours:\s*(\d+)", text, re.IGNORECASE
    )
    ticket_match = re.search(r"Time tickets?[^.]*\.", text, re.IGNORECASE)

    start = _parse_stamp(window_match.group("start")) if window_match else None
    end = _parse_stamp(window_match.group("end")) if window_match else None
    time_ticket_text = ticket_match.group(0).strip() if ticket_match else ""
    if window_match:
        status: Literal["parsed", "no_ticket", "unknown"] = "parsed"
        raw_text = window_match.group(0)
        reason = window_match.group(0)
    elif time_ticket_text:
        status = "no_ticket"
        raw_text = time_ticket_text
        reason = time_ticket_text
    else:
        status = "unknown"
        raw_text = ""
        reason = "Prepare for Registration page had no time-ticket text"

    return RegistrationWindow(
        term=term,
        eligible=False,
        start=start,
        end=end,
        reason=reason,
        raw_text=raw_text,
        source="banner_prepare",
        standing=standing_match.group(1) if standing_match else None,
        institution_hours=int(hours_match.group(1)) if hours_match else None,
        transfer_hours=int(hours_match.group(2)) if hours_match else None,
        time_ticket_text=time_ticket_text,
        parse_status=status,
    )


def window(term: str, *, ssb: SSBClient | None = None) -> RegistrationWindow:
    """Bind a term for registration and resolve its personal window.

    ``term/search`` supplies eligibility. When it has no window (the normal case
    for a future term), the Prepare for Registration page supplies the assigned
    ticket without changing ``eligible``.
    """
    client = ssb or SSBClient()
    payload = client.set_term(term, mode="registration")
    failures = payload.get("studentEligFailures") or []
    eligible = bool(payload.get("studentEligValid"))
    result = parse_failures(term, failures, eligible)
    if result.start is not None:
        return result

    try:
        html = client.prepare_registration(term)
    except Exception as exc:
        return result.model_copy(
            update={"reason": f"{result.reason} | prepare page unavailable: {type(exc).__name__}"}
        )

    prepared = parse_prepare_html(html, term)
    update: dict[str, object] = {
        "standing": prepared.standing,
        "institution_hours": prepared.institution_hours,
        "transfer_hours": prepared.transfer_hours,
        "time_ticket_text": prepared.time_ticket_text,
    }
    if prepared.start is not None:
        update.update(
            {
                "start": prepared.start,
                "end": prepared.end,
                "parse_status": "parsed",
                "source": "banner_prepare",
                "raw_text": " | ".join(
                    part for part in (result.raw_text, prepared.raw_text) if part
                ),
                "reason": f"{result.reason}; ticket from prepareRegistration",
            }
        )
    return result.model_copy(update=update)


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
