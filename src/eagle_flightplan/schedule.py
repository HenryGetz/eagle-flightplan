"""Pure schedule algebra: meetings, overlaps, free time and an ASCII week grid.

No I/O, no clock, no network. Everything here operates on wall-clock times in the
campus timezone (America/Chicago): Banner reports meeting times as local strings
such as ``"1125"`` meaning 11:25 AM, and this module never reinterprets them.

Core rules
----------
* Meetings are half-open intervals ``[start, end)``; 10:00-11:00 and 11:00-12:00
  do not conflict.
* Two sections never conflict with themselves, nor with sections that Banner has
  hard-linked to them (shared ``linkIdentifier`` — cross-listed lecture/lab
  pairs), nor with the lecture/lab pairing of the same course.
* TBA / blank / asynchronous meetings have no interval and therefore never
  produce an overlap. They are flagged ``unscheduled`` instead, and can be
  surfaced separately (``include_tba``) or treated as occupying the whole week
  (``strictness="strict"``).
"""

from __future__ import annotations

from datetime import time
from enum import Enum
from typing import Iterable, Literal, Sequence

from pydantic import BaseModel, ConfigDict, Field, field_validator

Strictness = Literal["lenient", "strict"]

DAY_ORDER: tuple[str, ...] = ("Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun")


class Weekday(str, Enum):
    MON = "Mon"
    TUE = "Tue"
    WED = "Wed"
    THU = "Thu"
    FRI = "Fri"
    SAT = "Sat"
    SUN = "Sun"


def parse_banner_time(raw: str | int | None) -> time | None:
    """Parse Banner's ``"1125"`` / ``"08:00"`` / ``"TBA"`` into a ``time``.

    Returns ``None`` for TBA, blank, or asynchronous markers.
    """
    if raw is None:
        return None
    text = str(raw).strip()
    if not text or text.upper() in {"TBA", "TBD", "ARR", "N/A", "ONLINE", "-"}:
        return None
    if ":" in text:
        parts = text.split(":")
        try:
            return time(int(parts[0]), int(parts[1]))
        except (ValueError, IndexError):
            return None
    digits = "".join(ch for ch in text if ch.isdigit())
    if not digits:
        return None
    digits = digits.zfill(4)
    try:
        hours = int(digits[:-2])
        minutes = int(digits[-2:])
    except ValueError:
        return None
    if hours == 24 and minutes == 0:
        return time(0, 0)
    if not (0 <= hours <= 23 and 0 <= minutes <= 59):
        return None
    return time(hours, minutes)


class Meeting(BaseModel):
    """One weekly meeting of a section, in campus-local wall-clock time."""

    model_config = ConfigDict(frozen=True)

    day: Weekday
    start: time | None = None
    end: time | None = None
    location: str = ""
    kind: str = ""

    @field_validator("start", "end", mode="before")
    @classmethod
    def _coerce_time(cls, value):  # noqa: ANN001 - pydantic hook
        if isinstance(value, str):
            return parse_banner_time(value)
        return value

    @property
    def scheduled(self) -> bool:
        return self.start is not None and self.end is not None

    @property
    def label(self) -> str:
        return self.kind or "Meeting"

    def span_minutes(self) -> int:
        if not self.scheduled:
            return 0
        start_min = self.start.hour * 60 + self.start.minute
        end_min = self.end.hour * 60 + self.end.minute
        return max(0, end_min - start_min)


class ScheduleSection(BaseModel):
    """Minimal section shape the conflict engine needs.

    Built either by hand (tests) or by :mod:`eagle_flightplan.banner.models` from a
    Banner search/section-detail payload.
    """

    model_config = ConfigDict(frozen=True)

    crn: str
    subject: str = ""
    number: str = ""
    title: str = ""
    term: str = ""
    link_identifier: str | None = None
    instructional_method: str = ""
    meetings: tuple[Meeting, ...] = Field(default_factory=tuple)
    notes: tuple[str, ...] = Field(default_factory=tuple)

    @property
    def course_label(self) -> str:
        head = " ".join(part for part in (self.subject, self.number) if part).strip()
        return head or self.title or self.crn

    @property
    def full_label(self) -> str:
        label = self.course_label
        return f"{label} ({self.title})" if self.title and label != self.title else label

    @property
    def unscheduled(self) -> bool:
        """True when the section has no usable meeting interval (TBA/async)."""
        return not any(meeting.scheduled for meeting in self.meetings)

    @property
    def tba_meetings(self) -> list[Meeting]:
        return [meeting for meeting in self.meetings if not meeting.scheduled]

    @property
    def scheduled_meetings(self) -> list[Meeting]:
        return [meeting for meeting in self.meetings if meeting.scheduled]


class Conflict(BaseModel):
    """An overlap (or, in strict mode, an unschedulable pairing)."""

    day: Weekday
    start: time
    end: time
    crn_a: str
    crn_b: str
    title_a: str
    title_b: str
    kind: Literal["overlap", "tba"] = "overlap"

    @property
    def minutes(self) -> int:
        return (self.end.hour * 60 + self.end.minute) - (self.start.hour * 60 + self.start.minute)

    @property
    def span(self) -> str:
        return f"{self.start.strftime('%H:%M')}-{self.end.strftime('%H:%M')}"

    @property
    def summary(self) -> str:
        if self.kind == "tba":
            return (
                f"{self.day.value} TBA: CRN {self.crn_a} {self.title_a} has no scheduled time "
                f"and cannot be checked against CRN {self.crn_b} {self.title_b}"
            )
        return (
            f"{self.day.value} {self.span} ({self.minutes} min): "
            f"CRN {self.crn_a} {self.title_a} overlaps CRN {self.crn_b} {self.title_b}"
        )


# --------------------------------------------------------------------------- #
# internal helpers
# --------------------------------------------------------------------------- #
def _minutes(value: time) -> int:
    return value.hour * 60 + value.minute


def _from_minutes(value: int) -> time:
    return time(value // 60, value % 60)


def _link_exempt(a: ScheduleSection, b: ScheduleSection) -> bool:
    """Sections Banner binds together may share a time without conflicting."""
    if a.crn == b.crn:
        return True
    if a.link_identifier and b.link_identifier and a.link_identifier == b.link_identifier:
        return True
    # Same course, one lecture + one lab: Banner permits the pairing.
    if (
        a.subject
        and a.subject == b.subject
        and a.number
        and a.number == b.number
        and a.term == b.term
    ):
        kinds_a = {m.kind.lower() for m in a.meetings if m.kind}
        kinds_b = {m.kind.lower() for m in b.meetings if m.kind}
        lecture = {"lecture", "class", "seminar", "discussion"}
        lab = {"lab", "laboratory", "clinical", "studio"}
        if (kinds_a and kinds_a <= lecture and kinds_b & lab) or (
            kinds_b and kinds_b <= lecture and kinds_a & lab
        ):
            return True
    return False


# --------------------------------------------------------------------------- #
# conflicts
# --------------------------------------------------------------------------- #
def conflicts(
    a: ScheduleSection,
    b: ScheduleSection,
    *,
    strictness: Strictness = "lenient",
) -> list[Conflict]:
    """Pairwise overlap list between two sections.

    ``strictness="strict"`` reports TBA sections as ``kind="tba"`` conflicts
    against every other section, since their placement is unknown. ``lenient``
    (default) never treats TBA as a conflict.
    """
    if a.crn == b.crn or _link_exempt(a, b):
        return []

    found: list[Conflict] = []
    for meeting_a in a.scheduled_meetings:
        for meeting_b in b.scheduled_meetings:
            if meeting_a.day != meeting_b.day:
                continue
            start = max(_minutes(meeting_a.start), _minutes(meeting_b.start))
            end = min(_minutes(meeting_a.end), _minutes(meeting_b.end))
            if start < end:
                found.append(
                    Conflict(
                        day=meeting_a.day,
                        start=_from_minutes(start),
                        end=_from_minutes(end),
                        crn_a=a.crn,
                        crn_b=b.crn,
                        title_a=a.full_label,
                        title_b=b.full_label,
                    )
                )

    if strictness == "strict" and not found:
        a_tba, b_tba = a.unscheduled, b.unscheduled
        if a_tba != b_tba:
            day, start, end = _tba_anchor(a, b)
            found.append(
                Conflict(
                    day=day,
                    start=start,
                    end=end,
                    crn_a=a.crn,
                    crn_b=b.crn,
                    title_a=a.full_label,
                    title_b=b.full_label,
                    kind="tba",
                )
            )
    return found


def _tba_anchor(a: ScheduleSection, b: ScheduleSection) -> tuple[Weekday, time, time]:
    """A stable 00:00-00:00 anchor for TBA conflicts, using a real meeting if any."""
    for section in (a, b):
        for meeting in section.scheduled_meetings:
            return meeting.day, meeting.start, meeting.end
    return Weekday.MON, time(0, 0), time(0, 0)


def find_conflicts(
    sections: Sequence[ScheduleSection],
    *,
    include_tba: bool = False,
    strictness: Strictness = "lenient",
) -> list[Conflict]:
    """All conflicts in a candidate set, deduplicated and stably ordered.

    TBA/async sections carry no interval, so they can only surface in
    ``strictness="strict"`` (as ``kind="tba"`` pairings). ``include_tba`` keeps
    them from being skipped early — useful when the caller wants the strict
    pass without changing modes; with the default ``lenient`` mode it changes
    nothing.
    """
    ordered = sorted({section.crn: section for section in sections}.values(), key=lambda s: s.crn)
    found: list[Conflict] = []
    for index, first in enumerate(ordered):
        for second in ordered[index + 1 :]:
            if not include_tba and strictness != "strict" and (first.unscheduled or second.unscheduled):
                continue
            found.extend(conflicts(first, second, strictness=strictness))

    def sort_key(conflict: Conflict) -> tuple[int, int, str, str]:
        return (
            DAY_ORDER.index(conflict.day.value),
            _minutes(conflict.start),
            conflict.crn_a,
            conflict.crn_b,
        )

    return sorted(found, key=sort_key)


def unscheduled_sections(sections: Iterable[ScheduleSection]) -> list[ScheduleSection]:
    return sorted((s for s in sections if s.unscheduled), key=lambda s: s.crn)


# --------------------------------------------------------------------------- #
# grid / free time
# --------------------------------------------------------------------------- #
class TimeGrid(BaseModel):
    """Weekly occupancy grid for a candidate set of sections."""

    model_config = ConfigDict(arbitrary_types_allowed=True)

    meetings: dict[str, list[tuple[Meeting, ScheduleSection]]] = Field(default_factory=dict)

    @classmethod
    def from_sections(cls, sections: Iterable[ScheduleSection]) -> "TimeGrid":
        grid = cls()
        for section in sections:
            for meeting in section.scheduled_meetings:
                grid.meetings.setdefault(meeting.day.value, []).append((meeting, section))
        for entries in grid.meetings.values():
            entries.sort(key=lambda pair: _minutes(pair[0].start))
        return grid

    def busy(self, day: str | Weekday) -> list[tuple[time, time]]:
        key = day.value if isinstance(day, Weekday) else day
        return [(_from_minutes(_minutes(m.start)), _from_minutes(_minutes(m.end))) for m, _ in self.meetings.get(key, [])]

    def free_blocks(self, day: str | Weekday, start: time, end: time) -> list[tuple[time, time]]:
        """Gaps inside ``[start, end)`` on one day, merged and clamped."""
        window_start, window_end = _minutes(start), _minutes(end)
        occupied = sorted(
            (max(window_start, _minutes(m.start)), min(window_end, _minutes(m.end)))
            for m in (meeting for meeting, _ in self.meetings.get(
                day.value if isinstance(day, Weekday) else day, []
            ))
        )
        gaps: list[tuple[time, time]] = []
        cursor = window_start
        for busy_start, busy_end in occupied:
            if busy_end <= busy_start:
                continue
            if busy_start > cursor:
                gaps.append((_from_minutes(cursor), _from_minutes(busy_start)))
            cursor = max(cursor, busy_end)
        if cursor < window_end:
            gaps.append((_from_minutes(cursor), _from_minutes(window_end)))
        return gaps

    def render(
        self,
        *,
        start: time = time(7, 0),
        end: time = time(22, 0),
        step_minutes: int = 30,
        cell_width: int = 14,
    ) -> str:
        if step_minutes <= 0 or cell_width < 6:
            raise ValueError("step_minutes must be > 0 and cell_width >= 6")
        if _minutes(end) <= _minutes(start):
            raise ValueError("end must be after start")

        header = "      " + "".join(day.ljust(cell_width) for day in DAY_ORDER)
        lines = [header.rstrip()]
        slot = _minutes(start)
        while slot < _minutes(end):
            slot_end = min(slot + step_minutes, _minutes(end))
            row = [f"{_from_minutes(slot).strftime('%H:%M')} "]
            for day in DAY_ORDER:
                entries = self.meetings.get(day, [])
                active = [
                    section
                    for meeting, section in entries
                    if _minutes(meeting.start) < slot_end and _minutes(meeting.end) > slot
                ]
                if not active:
                    cell = "."
                else:
                    labels = sorted({section.course_label for section in active})
                    cell = "/".join(labels)
                row.append(cell[: cell_width - 1].ljust(cell_width))
            lines.append("".join(row).rstrip())
            slot = slot_end
        return "\n".join(lines)


def free_blocks(
    sections: Iterable[ScheduleSection],
    day: str | Weekday,
    start: time,
    end: time,
) -> list[tuple[time, time]]:
    """Free intervals on ``day`` between ``start`` and ``end`` for a section set."""
    return TimeGrid.from_sections(sections).free_blocks(day, start, end)


def render_grid(
    sections: Iterable[ScheduleSection],
    *,
    include_tba: bool = True,
    start: time = time(7, 0),
    end: time = time(22, 0),
    step_minutes: int = 30,
) -> str:
    """ASCII week grid plus a TBA footer for sections without meeting times."""
    sections = list(sections)
    grid = TimeGrid.from_sections(sections)
    body = grid.render(start=start, end=end, step_minutes=step_minutes)
    if not include_tba:
        return body
    tba = unscheduled_sections(sections)
    if not tba:
        return body
    footer = ["", "TBA / unscheduled:"]
    footer.extend(f"  CRN {s.crn} {s.full_label}" for s in tba)
    return body + "\n" + "\n".join(footer)


def grid_legend(
    sections: Iterable[ScheduleSection],
    *,
    start: time = time(7, 0),
    end: time = time(22, 0),
    step_minutes: int = 30,
) -> dict[str, list[str]]:
    """Map every grid label (and overlap slug) to the CRN(s) it represents.

    The grid text is unchanged; this is a side table. A cell label is either one
    course code or ``"A/B"`` for overlapping courses. Multiple sections of the
    same course share the base code entry.
    """
    sections = list(sections)
    found: dict[str, set[str]] = {}
    for section in sections:
        if section.course_label:
            found.setdefault(section.course_label, set()).add(section.crn)
    grid = TimeGrid.from_sections(sections)
    if step_minutes <= 0:
        raise ValueError("step_minutes must be > 0")
    slot = _minutes(start)
    while slot < _minutes(end):
        slot_end = min(slot + step_minutes, _minutes(end))
        for day in DAY_ORDER:
            active = [
                section
                for meeting, section in grid.meetings.get(day, [])
                if _minutes(meeting.start) < slot_end and _minutes(meeting.end) > slot
            ]
            labels = sorted({section.course_label for section in active})
            if len(labels) > 1:
                found.setdefault("/".join(labels), set()).update(
                    section.crn for section in active
                )
        slot = slot_end
    return {label: sorted(crns) for label, crns in sorted(found.items())}


def render_grid_checked(
    sections: Iterable["ScheduleSection"],
    *,
    expected_crns: Iterable[str],
    **kwargs,
) -> str:
    """Render a grid only when it is built from exactly the table's sections.

    Artifacts pair a table with an ASCII grid; a mismatch means the two were
    generated from different section sets. Failing loudly beats shipping a
    wrong timetable.
    """
    sections = list(sections)
    found = {section.crn for section in sections}
    expected = {str(crn) for crn in expected_crns}
    if found != expected:
        raise ValueError(
            "grid/table CRN mismatch: "
            f"grid-only={sorted(found - expected)}, table-only={sorted(expected - found)}"
        )
    return render_grid(sections, **kwargs)


def conflict_summary(conflicts_: Sequence[Conflict]) -> str:
    """Human-readable one-line-per-conflict report naming both CRNs."""
    if not conflicts_:
        return "No conflicts."
    return "\n".join(conflict.summary for conflict in conflicts_)
