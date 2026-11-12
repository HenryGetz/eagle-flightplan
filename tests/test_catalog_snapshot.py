"""TermSnapshot query surface (offline)."""

from __future__ import annotations

from datetime import datetime, timezone

from unw_planner.banner.search import SnapshotMeta, TermSnapshot
from unw_planner.models import Instructor, Section, SectionAttribute
from unw_planner.schedule import Meeting, Weekday


def build_snapshot() -> TermSnapshot:
    def sec(crn, subject, number, *, open=True, instructor="Smith, James", attributes=(), title=None):
        return Section(
            crn=crn, term="202620", subject=subject, number=number, section="01",
            subject_course=f"{subject}{number}", title=title or f"{subject} {number}",
            credits_low=4.0, capacity=20, enrollment=20 if not open else 0,
            seats_available=0 if not open else 20, open=open,
            faculty=[Instructor(name=instructor)] if instructor else [],
            attributes=[SectionAttribute(code=code, description=code) for code in attributes],
            meetings=[Meeting(day=Weekday.MON, start="1000", end="1050", kind="Class")],
        )

    return TermSnapshot(
        meta=SnapshotMeta(term="202620", fetched_at=datetime.now(timezone.utc)),
        sections=[
            sec("1", "CYS", "2081", instructor="Smith, James"),
            sec("2", "CYS", "2081", instructor="Terry, Justin", open=False),
            sec("3", "BIB", "4360", instructor="Muska, Mark", attributes=("BEXP",)),
            sec("4", "LIT", "2157", instructor="Fox, Rebecca", attributes=("CCUL",), title="Contemporary Fiction"),
        ],
    )


def test_by_course_orders_open_first_and_by_section():
    snapshot = build_snapshot()
    sections = snapshot.by_course("cys", "2081")
    assert [section.crn for section in sections] == ["1", "2"]
    assert sections[1].open is False


def test_by_subject_attribute_instructor_and_open_only():
    snapshot = build_snapshot()
    assert [s.crn for s in snapshot.by_subject("BIB")] == ["3"]
    assert [s.crn for s in snapshot.by_attribute("ccul")] == ["4"]
    assert [s.crn for s in snapshot.by_instructor("muska")] == ["3"]
    assert [s.crn for s in snapshot.open_only().sections] == ["1", "3", "4"]


def test_search_matches_title_code_and_instructor():
    snapshot = build_snapshot()
    assert [s.crn for s in snapshot.search("Fiction")] == ["4"]
    assert [s.crn for s in snapshot.search("cys 2081")] == ["1", "2"]
    assert [s.crn for s in snapshot.search("Terry")] == ["2"]


def test_instructors_and_attributes_aggregates():
    snapshot = build_snapshot()
    assert snapshot.instructors() == ["Fox, Rebecca", "Muska, Mark", "Smith, James", "Terry, Justin"]
    assert snapshot.attributes() == {"BEXP": 1, "CCUL": 1}


def test_snapshot_round_trips_through_json(tmp_path):
    snapshot = build_snapshot()
    path = tmp_path / "202620.json"
    path.write_text(snapshot.model_dump_json())
    restored = TermSnapshot.model_validate_json(path.read_text())
    section = restored.by_crn("1")
    assert section is not None
    assert section.days == [Weekday.MON]
    assert section.time_label == "10:00-10:50"
    assert len(restored) == 4


def test_term_index_tracks_terms_and_courses(tmp_path):
    from unw_planner.banner.search import TermIndex

    snapshot = build_snapshot()
    index = TermIndex(tmp_path / "index.sqlite")
    index.upsert(snapshot)
    assert index.terms() == ["202620"]
    assert index.section_count("202620") == 4
    rows = index.offering_rows("CYS", "2081")
    assert rows == [("202620", 2, 1)]
    index.upsert(snapshot)  # idempotent replace, no duplicate rows
    assert index.section_count("202620") == 4
    index.close()


def test_snapshot_save_is_atomic_and_corrupt_cache_is_missing(tmp_path, monkeypatch):
    from unw_planner.banner import search

    monkeypatch.setattr(search, "TERMS_DIR", tmp_path)
    monkeypatch.setattr(search, "TERM_INDEX_PATH", tmp_path / "index.sqlite")
    snapshot = build_snapshot()
    path = snapshot.save()
    assert path == tmp_path / "202620.json"
    assert path.exists()
    assert not (tmp_path / "202620.json.tmp").exists()
    assert search.load_snapshot("202620") is not None

    path.write_text("{ this is not json", encoding="utf-8")
    assert search.load_snapshot("202620") is None
