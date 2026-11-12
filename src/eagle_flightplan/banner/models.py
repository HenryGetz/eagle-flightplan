"""Typed models for Banner 9 Student Self-Service (SSB) course data.

Field names mirror the JSON keys Banner returns so a reader can map a model back
to the wire format without guessing. Every parser here tolerates the nulls Banner
emits for optional values.
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field

from ..models import Section as SnapshotSection
from ..schedule import Meeting, ScheduleSection, Weekday

DAY_KEYS: tuple[tuple[str, Weekday], ...] = (
    ("monday", Weekday.MON),
    ("tuesday", Weekday.TUE),
    ("wednesday", Weekday.WED),
    ("thursday", Weekday.THU),
    ("friday", Weekday.FRI),
    ("saturday", Weekday.SAT),
    ("sunday", Weekday.SUN),
)


class Term(BaseModel):
    code: str
    description: str

    @property
    def year(self) -> int:
        return int(self.code[:4])

    @property
    def season(self) -> str:
        return {"10": "Fall", "20": "Spring", "30": "Summer"}.get(self.code[4:], "Unknown")


class Faculty(BaseModel):
    model_config = ConfigDict(extra="ignore")

    banner_id: str | None = None
    display_name: str = ""
    email: str | None = None
    primary: bool = False
    term: str = ""

    @classmethod
    def from_api(cls, payload: dict) -> "Faculty":
        return cls(
            banner_id=payload.get("bannerId"),
            display_name=payload.get("displayName") or "",
            email=payload.get("emailAddress"),
            primary=bool(payload.get("primaryIndicator")),
            term=payload.get("term") or "",
        )


class MeetingTime(BaseModel):
    """One ``meetingTime`` object: campus-local 24h ``HHMM`` strings + day flags."""

    model_config = ConfigDict(extra="ignore")

    begin_time: str | None = None
    end_time: str | None = None
    building: str | None = None
    building_description: str | None = None
    room: str | None = None
    campus: str | None = None
    campus_description: str | None = None
    meeting_type: str = ""
    meeting_type_description: str = ""
    meeting_schedule_type: str = ""
    credit_hour_session: float | None = None
    hours_week: float | None = None
    start_date: str | None = None
    end_date: str | None = None
    term: str = ""
    days: tuple[Weekday, ...] = Field(default_factory=tuple)

    @classmethod
    def from_api(cls, payload: dict) -> "MeetingTime":
        return cls(
            begin_time=payload.get("beginTime"),
            end_time=payload.get("endTime"),
            building=payload.get("building"),
            building_description=payload.get("buildingDescription"),
            room=payload.get("room"),
            campus=payload.get("campus"),
            campus_description=payload.get("campusDescription"),
            meeting_type=payload.get("meetingType") or "",
            meeting_type_description=payload.get("meetingTypeDescription") or "",
            meeting_schedule_type=payload.get("meetingScheduleType") or "",
            credit_hour_session=payload.get("creditHourSession"),
            hours_week=payload.get("hoursWeek"),
            start_date=payload.get("startDate"),
            end_date=payload.get("endDate"),
            term=payload.get("term") or "",
            days=tuple(day for key, day in DAY_KEYS if payload.get(key)),
        )

    @property
    def location(self) -> str:
        building = self.building_description or self.building or ""
        return f"{building} {self.room}".strip()

    def to_meetings(self) -> list[Meeting]:
        """Expand the day flags into one :class:`~eagle_flightplan.schedule.Meeting` per weekday."""
        if not self.days:
            return [
                Meeting(
                    day=Weekday.MON,
                    start=self.begin_time,
                    end=self.end_time,
                    location=self.location,
                    kind=self.meeting_type_description or self.meeting_schedule_type,
                )
            ]
        return [
            Meeting(
                day=day,
                start=self.begin_time,
                end=self.end_time,
                location=self.location,
                kind=self.meeting_type_description or self.meeting_schedule_type,
            )
            for day in self.days
        ]


class SectionAttribute(BaseModel):
    code: str
    description: str
    term: str = ""

    @classmethod
    def from_api(cls, payload: dict) -> "SectionAttribute":
        return cls(
            code=payload.get("code") or "",
            description=payload.get("description") or "",
            term=payload.get("termCode") or "",
        )


class Section(BaseModel):
    """A class-search result row (one CRN)."""

    model_config = ConfigDict(extra="ignore")

    crn: str
    term: str
    term_description: str = ""
    subject: str = ""
    number: str = ""
    section_number: str = ""
    title: str = ""
    subject_description: str = ""
    campus: str = ""
    schedule_type: str = ""
    instructional_method: str = ""
    instructional_method_description: str = ""
    part_of_term: str = ""
    credit_hours: float | None = None
    credit_hour_low: float | None = None
    credit_hour_high: float | None = None
    credit_hour_indicator: str | None = None
    maximum_enrollment: int | None = None
    enrollment: int | None = None
    seats_available: int | None = None
    wait_capacity: int | None = None
    wait_count: int | None = None
    wait_available: int | None = None
    open_section: bool = False
    link_identifier: str | None = None
    is_section_linked: bool = False
    faculty: list[Faculty] = Field(default_factory=list)
    meeting_times: list[MeetingTime] = Field(default_factory=list)
    attributes: list[SectionAttribute] = Field(default_factory=list)

    @classmethod
    def from_api(cls, payload: dict) -> "Section":
        meetings = [
            MeetingTime.from_api(entry["meetingTime"])
            for entry in payload.get("meetingsFaculty") or []
            if entry.get("meetingTime")
        ]
        return cls(
            crn=str(payload.get("courseReferenceNumber") or ""),
            term=payload.get("term") or "",
            term_description=payload.get("termDesc") or "",
            subject=payload.get("subject") or "",
            number=payload.get("courseNumber") or "",
            section_number=payload.get("sequenceNumber") or "",
            title=payload.get("courseTitle") or "",
            subject_description=payload.get("subjectDescription") or "",
            campus=payload.get("campusDescription") or "",
            schedule_type=payload.get("scheduleTypeDescription") or "",
            instructional_method=payload.get("instructionalMethod") or "",
            instructional_method_description=payload.get("instructionalMethodDescription") or "",
            part_of_term=str(payload.get("partOfTerm") or ""),
            credit_hours=payload.get("creditHours"),
            credit_hour_low=payload.get("creditHourLow"),
            credit_hour_high=payload.get("creditHourHigh"),
            credit_hour_indicator=payload.get("creditHourIndicator"),
            maximum_enrollment=payload.get("maximumEnrollment"),
            enrollment=payload.get("enrollment"),
            seats_available=payload.get("seatsAvailable"),
            wait_capacity=payload.get("waitCapacity"),
            wait_count=payload.get("waitCount"),
            wait_available=payload.get("waitAvailable"),
            open_section=bool(payload.get("openSection")),
            link_identifier=payload.get("linkIdentifier"),
            is_section_linked=bool(payload.get("isSectionLinked")),
            faculty=[Faculty.from_api(f) for f in payload.get("faculty") or []],
            meeting_times=meetings,
            attributes=[SectionAttribute.from_api(a) for a in payload.get("sectionAttributes") or []],
        )

    @property
    def code(self) -> str:
        return f"{self.subject} {self.number}-{self.section_number}".strip()

    @property
    def instructors(self) -> list[str]:
        return [f.display_name for f in self.faculty if f.display_name]

    def to_schedule_section(self) -> ScheduleSection:
        meetings: list[Meeting] = []
        for meeting_time in self.meeting_times:
            meetings.extend(meeting_time.to_meetings())
        return ScheduleSection(
            crn=self.crn,
            subject=self.subject,
            number=self.number,
            title=self.title,
            term=self.term,
            link_identifier=self.link_identifier,
            instructional_method=self.instructional_method_description,
            meetings=tuple(meetings),
        )


class SearchPage(BaseModel):
    """Envelope of ``GET /ssb/searchResults/searchResults``."""

    model_config = ConfigDict(extra="ignore")

    success: bool
    total_count: int
    page_offset: int
    page_max_size: int
    sections: list[Section] = Field(default_factory=list)

    @classmethod
    def from_api(cls, payload: dict) -> "SearchPage":
        return cls(
            success=bool(payload.get("success")),
            total_count=int(payload.get("totalCount") or 0),
            page_offset=int(payload.get("pageOffset") or 0),
            page_max_size=int(payload.get("pageMaxSize") or 0),
            sections=[Section.from_api(row) for row in payload.get("data") or []],
        )


class LinkedSection(BaseModel):
    """Entry of ``GET /ssb/searchResults/fetchLinkedSections`` -> ``linkedData``."""

    model_config = ConfigDict(extra="ignore")

    crn: str = ""
    subject: str = ""
    number: str = ""
    title: str = ""
    link_identifier: str | None = None

    @classmethod
    def from_api(cls, payload: dict) -> "LinkedSection":
        return cls(
            crn=str(payload.get("courseReferenceNumber") or ""),
            subject=payload.get("subject") or "",
            number=payload.get("courseNumber") or "",
            title=payload.get("courseTitle") or "",
            link_identifier=payload.get("linkIdentifier"),
        )


class SectionDetail(BaseModel):
    """Section-detail bundle: JSON meeting times + HTML fragments flattened to text."""

    model_config = ConfigDict(extra="ignore")

    crn: str
    term: str
    section: Section | SnapshotSection | None = None
    meeting_times: list[MeetingTime] = Field(default_factory=list)
    faculty: list[Faculty] = Field(default_factory=list)
    prerequisites: list[str] = Field(default_factory=list)
    corequisites: list[str] = Field(default_factory=list)
    restrictions: list[str] = Field(default_factory=list)
    description: str = ""
    attributes: list[SectionAttribute] = Field(default_factory=list)
    linked_sections: list[LinkedSection] = Field(default_factory=list)
    enrollment_text: str = ""

    def to_schedule_section(self) -> ScheduleSection:
        meetings: list[Meeting] = []
        for meeting_time in self.meeting_times:
            meetings.extend(meeting_time.to_meetings())
        section = self.section
        if not meetings and section is not None:
            meetings = list(getattr(section, "meetings", []) or [])
        method = ""
        if section is not None:
            method = (
                getattr(section, "instructional_method_description", "")
                or getattr(section, "instructional_method", "")
            )
        return ScheduleSection(
            crn=self.crn,
            subject=section.subject if section else "",
            number=section.number if section else "",
            title=section.title if section else "",
            term=self.term,
            link_identifier=section.link_identifier if section else None,
            instructional_method=method,
            meetings=tuple(meetings),
        )
