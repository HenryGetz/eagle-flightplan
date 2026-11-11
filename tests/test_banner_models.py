import json
from pathlib import Path

from unw_planner.banner.models import SearchPage, Section, Term
from unw_planner.schedule import Weekday

FIXTURES = Path(__file__).parent / "fixtures"


def load(name: str) -> dict:
    return json.loads((FIXTURES / name).read_text())


def test_search_page_parses_real_payload():
    page = SearchPage.from_api(load("search_CYS_202620.json"))
    assert page.success is True
    assert page.total_count == len(page.sections) == 2
    section = page.sections[0]
    assert section.crn == "22445"
    assert section.subject == "CYS"
    assert section.number == "2081"
    assert section.term == "202620"
    assert section.term_description == "Spring 2027"
    assert section.title == "Data Communications"
    assert section.credit_hour_low == 4
    assert section.maximum_enrollment == 30
    assert section.open_section is True
    assert section.instructional_method_description == "Activity/Experience"


def test_faculty_and_meeting_times():
    page = SearchPage.from_api(load("search_CYS_202620.json"))
    section = page.sections[0]
    assert section.instructors == ["Smith, James"]
    meeting = section.meeting_times[0]
    assert meeting.begin_time == "1245"
    assert meeting.end_time == "1425"
    assert meeting.building == "RIL"
    assert meeting.room == "R1332"
    assert meeting.days == (Weekday.TUE, Weekday.THU)
    assert meeting.meeting_type == "CLAS"


def test_to_schedule_section_expands_day_flags():
    section = SearchPage.from_api(load("search_CYS_202620.json")).sections[0]
    schedule = section.to_schedule_section()
    assert schedule.crn == "22445"
    assert schedule.course_label == "CYS 2081"
    assert [(m.day.value, m.start.hour, m.start.minute) for m in schedule.scheduled_meetings] == [
        ("Tue", 12, 45),
        ("Thu", 12, 45),
    ]
    assert schedule.unscheduled is False


def test_section_tolerates_nulls():
    section = Section.from_api({"courseReferenceNumber": "1", "term": "202620"})
    assert section.meeting_times == []
    assert section.faculty == []
    assert section.open_section is False
    assert section.to_schedule_section().unscheduled is True


def test_term_season_decoding():
    assert Term(code="202620", description="Spring 2027").season == "Spring"
    assert Term(code="202630", description="Summer 2027").season == "Summer"
    assert Term(code="202610", description="Fall 2026").season == "Fall"
    assert Term(code="202620", description="").year == 2026


def test_section_detail_fixture_shapes():
    detail = load("section_22445_details.json")
    assert detail["meetingTimes"]["status"] == 200
    payload = json.loads(detail["meetingTimes"]["body"])
    assert payload["fmt"][0]["meetingTime"]["beginTime"] == "1245"
    assert "Enrollment Seats Available" in detail["enrollment"]["body"]
    assert "Corequisites" in detail["coreq"]["body"]


def test_section_detail_accepts_snapshot_section_and_uses_snapshot(monkeypatch):
    from datetime import datetime, timezone

    import httpx

    from unw_planner.banner.models import SectionDetail
    from unw_planner.banner.search import SnapshotMeta, TermSnapshot
    from unw_planner.banner.ssb import SSBClient
    from unw_planner.models import Instructor, Section as SnapshotSection
    from unw_planner.schedule import Meeting, Weekday

    normalized = SnapshotSection(
        crn="999", term="202620", subject="CYS", number="4245", section="01",
        subject_course="CYS4245", title="Current Practices", credits_low=4.0,
        capacity=30, enrollment=0, seats_available=30, open=True,
        faculty=[Instructor(name="Smith, James")],
        meetings=[Meeting(day=Weekday.MON, start="10:00", end="10:50", kind="Class")],
    )
    detail = SectionDetail(crn="999", term="202620", section=normalized)
    schedule = detail.to_schedule_section()
    assert schedule.crn == "999"
    assert schedule.subject == "CYS"
    assert schedule.scheduled_meetings

    snapshot = TermSnapshot(
        meta=SnapshotMeta(term="202620", fetched_at=datetime.now(timezone.utc)),
        sections=[normalized],
    )
    monkeypatch.setattr("unw_planner.banner.search.load_snapshot", lambda term: snapshot)

    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path.replace("/StudentRegistrationSsb/ssb", "")
        if path == "/term/search":
            return httpx.Response(200, json={"fwdURL": "/classSearch/classSearch"})
        if path.endswith("getFacultyMeetingTimes"):
            return httpx.Response(200, json={"fmt": []})
        if path.endswith("fetchLinkedSections"):
            return httpx.Response(200, json={"linkedData": []})
        return httpx.Response(200, text="<section>none</section>", headers={"content-type": "text/html"})

    http = httpx.Client(transport=httpx.MockTransport(handler), base_url="https://x/StudentRegistrationSsb/ssb")
    client = SSBClient(http)
    resolved = client.section_detail("999", "202620")
    assert resolved.section is not None
    assert resolved.section.crn == "999"
    assert resolved.to_schedule_section().scheduled_meetings
