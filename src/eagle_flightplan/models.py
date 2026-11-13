"""Shared, normalized course models used by the snapshot, catalog and planner.

These are *cooked* values: local wall-clock meeting times, numeric credits, flat
instructor/attribute lists. The raw wire shapes live in
:mod:`eagle_flightplan.banner.models` and are converted by
:func:`Section.from_wire`.
"""

from __future__ import annotations

from datetime import time

from pydantic import BaseModel, ConfigDict, Field

from .schedule import Meeting, ScheduleSection, Weekday


class Instructor(BaseModel):
    model_config = ConfigDict(frozen=True)

    name: str = ""
    email: str | None = None
    primary: bool = False


class SectionAttribute(BaseModel):
    model_config = ConfigDict(frozen=True)

    code: str
    description: str = ""


class Section(BaseModel):
    """One section (CRN) as returned by a term-wide search snapshot."""

    model_config = ConfigDict(extra="ignore")

    crn: str
    term: str
    subject: str = ""
    number: str = ""
    section: str = ""
    subject_course: str = ""
    title: str = ""
    credits_low: float | None = None
    credits_high: float | None = None
    level: str | None = None
    capacity: int | None = None
    enrollment: int | None = None
    seats_available: int | None = None
    wait_available: int | None = None
    open: bool = False
    link_identifier: str | None = None
    is_linked: bool = False
    part_of_term: str = ""
    campus: str = ""
    instructional_method: str = ""
    schedule_type: str = ""
    faculty: list[Instructor] = Field(default_factory=list)
    attributes: list[SectionAttribute] = Field(default_factory=list)
    meetings: list[Meeting] = Field(default_factory=list)

    # --------------------------------------------------------------- derived
    @property
    def code(self) -> str:
        return f"{self.subject} {self.number}"

    @property
    def section_code(self) -> str:
        return f"{self.code}-{self.section}".strip()

    @property
    def credits(self) -> float | None:
        """Display credit value: the high end of a range when present."""
        return self.credits_high if self.credits_high is not None else self.credits_low

    @property
    def planning_credits(self) -> float | None:
        """Credit value to reserve when planning.

        Variable-credit sections reserve the low end by default; an explicit
        override can raise or lower it. Fixed-credit sections are unaffected.
        """
        return self.credits_low if self.credits_low is not None else self.credits_high

    @property
    def credit_range(self) -> list[float] | None:
        if self.credits_low is None and self.credits_high is None:
            return None
        if self.credits_high is None or self.credits_high == self.credits_low:
            return [self.credits_low if self.credits_low is not None else self.credits_high]
        return [self.credits_low if self.credits_low is not None else self.credits_high, self.credits_high]

    @property
    def instructors(self) -> list[str]:
        names = [person.name for person in self.faculty if person.name]
        return names or ["Staff"]

    @property
    def instructor_emails(self) -> list[str]:
        return [person.email for person in self.faculty if person.email]

    @property
    def attribute_codes(self) -> set[str]:
        return {attribute.code.upper() for attribute in self.attributes}

    @property
    def unscheduled(self) -> bool:
        return not any(meeting.scheduled for meeting in self.meetings)

    @property
    def days(self) -> list[Weekday]:
        return sorted(
            {meeting.day for meeting in self.meetings if meeting.scheduled},
            key=lambda day: list(Weekday).index(day),
        )

    @property
    def time_label(self) -> str:
        spans = sorted(
            {
                f"{meeting.start.strftime('%H:%M')}-{meeting.end.strftime('%H:%M')}"
                for meeting in self.meetings
                if meeting.scheduled
            }
        )
        return ", ".join(spans) or "TBA"

    @property
    def day_label(self) -> str:
        if not self.days:
            return "TBA"
        if self.days and len(self.days) == 5 and set(self.days) == {
            Weekday.MON, Weekday.TUE, Weekday.WED, Weekday.THU, Weekday.FRI
        }:
            return "Daily"
        return "".join(day.value[0] for day in self.days)

    def to_schedule_section(self) -> ScheduleSection:
        return ScheduleSection(
            crn=self.crn,
            subject=self.subject,
            number=self.number,
            title=self.title,
            term=self.term,
            link_identifier=self.link_identifier,
            instructional_method=self.instructional_method,
            part_of_term=self.part_of_term,
            meetings=tuple(self.meetings),
        )

    @classmethod
    def from_wire(cls, row) -> "Section":
        """Convert an :class:`eagle_flightplan.banner.models.Section` wire row."""
        meetings: list[Meeting] = []
        for meeting_time in row.meeting_times:
            meetings.extend(meeting_time.to_meetings())
        return cls(
            crn=row.crn,
            term=row.term,
            subject=row.subject,
            number=row.number,
            section=row.section_number,
            subject_course=f"{row.subject}{row.number}",
            title=row.title,
            credits_low=row.credit_hour_low if row.credit_hour_low is not None else row.credit_hours,
            credits_high=row.credit_hour_high,
            level=None,
            capacity=row.maximum_enrollment,
            enrollment=row.enrollment,
            seats_available=row.seats_available,
            wait_available=row.wait_available,
            open=row.open_section,
            link_identifier=row.link_identifier,
            is_linked=row.is_section_linked,
            part_of_term=row.part_of_term,
            campus=row.campus,
            instructional_method=row.instructional_method_description,
            schedule_type=row.schedule_type,
            faculty=[
                Instructor(name=person.display_name, email=person.email, primary=person.primary)
                for person in row.faculty
            ],
            attributes=[
                SectionAttribute(code=attribute.code, description=attribute.description)
                for attribute in row.attributes
            ],
            meetings=meetings,
        )


def meeting_start(section: Section) -> time:
    """Earliest scheduled start, used as a deterministic tie-break."""
    starts = [meeting.start for meeting in section.meetings if meeting.start]
    return min(starts) if starts else time(0, 0)
