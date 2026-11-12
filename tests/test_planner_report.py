"""Offline test for the advising brief (report.py)."""

from __future__ import annotations

from datetime import datetime, timezone

from flightplan.audit.models import Audit, CourseRef, GroupKind, RequirementBlock, RequirementGroup
from flightplan.banner.search import SnapshotMeta, TermSnapshot
from flightplan.models import Instructor, Section
from flightplan.planner import PrereqProvider, RatingInfo, RatingProvider
from flightplan.report import build_brief
from flightplan.schedule import Meeting, Weekday


def section(crn: str, *, instructor: str, credits: float = 4.0, start: str = "10:00", attributes: tuple[str, ...] = ()) -> Section:
    from flightplan.models import SectionAttribute

    return Section(
        crn=crn, term="202620", subject="CYS", number="4245", section="01",
        subject_course="CYS4245", title="Current Practices", credits_low=credits,
        capacity=20, enrollment=0, seats_available=20, open=True,
        faculty=[Instructor(name=instructor)],
        attributes=[SectionAttribute(code=code, description=code) for code in attributes],
        meetings=[Meeting(day=Weekday.MON, start=start, end="10:50", kind="Class")],
    )


def test_brief_contains_all_sections_and_rejection_reason(tmp_path):
    audit = Audit(
        student_name="Hidden Name",
        majors=["Cybersecurity"],
        minors=["Ministry"],
        catalog_year="2024-2025 (Fall 2024)",
        credits_earned=100.0,
        credits_in_progress=12.0,
        gpa=3.9,
        groups=[
            RequirementGroup(
                id="RA1",
                title="Major: Cybersecurity",
                kind=GroupKind.MAJOR,
                blocks=[
                    RequirementBlock(
                        label="Current Practices",
                        pick_n_of=1,
                        options=[CourseRef(subject="CYS", number="4245", status="option")],
                    )
                ],
            )
        ],
    )
    snapshot = TermSnapshot(
        meta=SnapshotMeta(term="202620", fetched_at=datetime.now(timezone.utc)),
        sections=[
            section("1", instructor="Blocked, Prof", start="10:00"),
            section("2", instructor="Good, Prof", start="13:00"),
        ],
    )
    ratings = RatingProvider(
        path=tmp_path / "ratings.json",
        lookup=lambda name: {
            "Blocked, Prof": RatingInfo(name=name, rating=5.0, num_ratings=12),
            "Good, Prof": RatingInfo(name=name, rating=4.0, num_ratings=10),
        }.get(name, RatingInfo(name=name)),
    )
    prereqs = PrereqProvider(
        directory=tmp_path,
        fetcher=lambda sec: "Prerequisites:PQ ( Course or Test: CYS 9999 )"
        if sec.crn == "1"
        else "No prerequisite information available.",
    )
    brief = build_brief("202620", max_credits=16, audit=audit, snapshot=snapshot, ratings=ratings, prereqs=prereqs)
    markdown = brief.markdown
    for heading in (
        "## 1. Student and program",
        "## 2. Remaining requirements",
        "## 3. Candidates per requirement",
        "## 4. Recommended draft",
        "## 5. Why not the others",
        "## 6. Risks",
    ):
        assert heading in markdown
    assert "Hidden Name" not in markdown  # shared document stays name-free
    assert "| 2 | CYS 4245-01 |" in markdown
    assert "Blocked, Prof" in markdown and "prerequisite" in markdown
    assert "no overlaps" in markdown
    assert "TBA / unscheduled" in markdown or "```" in markdown
    assert brief.data["student_name"] == "Hidden Name"
    assert brief.data["draft"]["total_credits"] == 4.0


def test_brief_empty_requirement_explains_why(tmp_path, monkeypatch):
    def unexpected_load(term):
        raise AssertionError("empty caller snapshot must not be replaced by a cache lookup")

    monkeypatch.setattr("flightplan.report.load_snapshot", unexpected_load)
    audit = Audit(
        groups=[
            RequirementGroup(
                id="RA2",
                title="Major: Cybersecurity",
                kind=GroupKind.MAJOR,
                blocks=[
                    RequirementBlock(
                        label="Digital Forensics",
                        pick_n_of=1,
                        options=[CourseRef(subject="CYS", number="4466", status="option")],
                    )
                ],
            )
        ]
    )
    snapshot = TermSnapshot(
        meta=SnapshotMeta(term="202620", fetched_at=datetime.now(timezone.utc)),
        sections=[],
    )
    brief = build_brief(
        "202620",
        audit=audit,
        snapshot=snapshot,
        ratings=RatingProvider(path=tmp_path / "r.json", lookup=lambda name: RatingInfo(name=name)),
        prereqs=PrereqProvider(directory=tmp_path, fetcher=lambda sec: ""),
    )
    assert "no candidate sections" in brief.markdown
    assert "no offered section can satisfy it this term" in brief.markdown


def test_brief_roadmap_keeps_legitimately_empty_other_term_snapshots(tmp_path, monkeypatch):
    empty = TermSnapshot(
        meta=SnapshotMeta(term="202710", fetched_at=datetime.now(timezone.utc)),
        sections=[],
    )
    monkeypatch.setattr("flightplan.report.load_snapshot", lambda term: empty)
    brief = build_brief(
        "202620",
        audit=Audit(),
        snapshot=TermSnapshot(
            meta=SnapshotMeta(term="202620", fetched_at=datetime.now(timezone.utc)),
            sections=[],
        ),
        include_roadmap=True,
        ratings=RatingProvider(path=tmp_path / "r.json", lookup=lambda name: RatingInfo(name=name)),
        prereqs=PrereqProvider(directory=tmp_path, fetcher=lambda sec: ""),
    )
    assert "## 7. Roadmap" in brief.markdown
