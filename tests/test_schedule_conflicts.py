from datetime import time


from eagle_flightplan.schedule import (
    Meeting,
    ScheduleSection,
    Weekday,
    conflict_summary,
    conflicts,
    find_conflicts,
    unscheduled_sections,
)


def section(crn: str, meetings, **kwargs) -> ScheduleSection:
    return ScheduleSection(crn=crn, meetings=tuple(meetings), **kwargs)


def mwf(start="1000", end="1050", kind="Lecture", location="Riley 100"):
    return [
        Meeting(day=day, start=start, end=end, location=location, kind=kind)
        for day in (Weekday.MON, Weekday.WED, Weekday.FRI)
    ]


def test_touching_endpoints_do_not_conflict():
    a = section("1", [Meeting(day="Mon", start="1000", end="1100", kind="Lecture")])
    b = section("2", [Meeting(day="Mon", start="1100", end="1200", kind="Lecture")])
    assert conflicts(a, b) == []


def test_overlap_is_half_open_and_reports_span():
    a = section("1", [Meeting(day="Mon", start="1000", end="1100", kind="Lecture")])
    b = section("2", [Meeting(day="Mon", start="1030", end="1200", kind="Lecture")])
    found = conflicts(a, b)
    assert len(found) == 1
    conflict = found[0]
    assert conflict.day is Weekday.MON
    assert conflict.start == time(10, 30)
    assert conflict.end == time(11, 0)
    assert conflict.minutes == 30
    assert "CRN 1" in conflict.summary and "CRN 2" in conflict.summary


def test_different_weekdays_do_not_conflict():
    a = section("1", [Meeting(day="Mon", start="1000", end="1100")])
    b = section("2", [Meeting(day="Tue", start="1000", end="1100")])
    assert conflicts(a, b) == []


def test_identical_crn_is_never_a_self_conflict():
    a = section("777", mwf())
    assert conflicts(a, a) == []
    assert find_conflicts([a, a]) == []


def test_linked_sections_are_exempt():
    lecture = section("101", mwf(kind="Lecture"), link_identifier="LNK42")
    lab = section("102", mwf(kind="Lab"), link_identifier="LNK42")
    assert conflicts(lecture, lab) == []
    assert find_conflicts([lecture, lab]) == []


def test_lecture_lab_pairing_of_same_course_is_exempt():
    lecture = section(
        "201", mwf(kind="Lecture"), subject="CYS", number="4465", term="202620"
    )
    lab = section(
        "202", mwf(kind="Lab"), subject="CYS", number="4465", term="202620"
    )
    assert conflicts(lecture, lab) == []


def test_real_banner_labels_class_plus_lab_are_exempt():
    lecture = section("211", mwf(kind="Class"), subject="CYS", number="2081", term="202620")
    lab = section(
        "212", mwf(kind="Lab"), subject="CYS", number="2081", term="202620"
    )
    assert conflicts(lecture, lab) == []


def test_two_lectures_of_same_course_still_conflict():
    first = section("301", mwf(), subject="CYS", number="2081", term="202620")
    second = section("302", mwf(), subject="CYS", number="2081", term="202620")
    assert len(conflicts(first, second)) == 3


def test_multi_meeting_course_flags_only_the_overlapping_day():
    a = section("401", mwf())
    b = section("402", [Meeting(day="Wed", start="1000", end="1100", kind="Lab")])
    found = conflicts(a, b)
    assert [conflict.day for conflict in found] == [Weekday.WED]


def test_tba_sections_are_ignored_by_default():
    async_section = section("501", [Meeting(day="Mon", start=None, end=None, kind="Online")])
    scheduled = section("502", mwf())
    assert async_section.unscheduled is True
    assert conflicts(async_section, scheduled) == []
    assert find_conflicts([async_section, scheduled]) == []
    assert [s.crn for s in unscheduled_sections([async_section, scheduled])] == ["501"]


def test_strict_mode_flags_tba_pairs():
    async_section = section("601", [Meeting(day="Mon", start=None, end=None)])
    scheduled = section("602", mwf())
    found = find_conflicts([async_section, scheduled], strictness="strict")
    assert len(found) == 1
    assert found[0].kind == "tba"
    assert "TBA" in found[0].summary


def test_find_conflicts_is_deduplicated_and_ordered():
    a = section("10", [Meeting(day="Mon", start="1000", end="1100")])
    b = section("11", [Meeting(day="Mon", start="1030", end="1130")])
    c = section("12", [Meeting(day="Mon", start="0900", end="1015")])
    found = find_conflicts([a, b, c, a])
    assert [(conflict.crn_a, conflict.crn_b) for conflict in found] == [
        ("10", "12"),
        ("10", "11"),
    ]
    assert conflict_summary([]) == "No conflicts."


def test_banner_time_parsing_variants():
    from eagle_flightplan.schedule import parse_banner_time

    assert parse_banner_time("1125") == time(11, 25)
    assert parse_banner_time("08:00") == time(8, 0)
    assert parse_banner_time("TBA") is None
    assert parse_banner_time("") is None
    assert parse_banner_time(None) is None
    assert parse_banner_time("2400") == time(0, 0)
    assert parse_banner_time("9999") is None


def test_non_overlapping_part_of_term_dates_do_not_conflict():
    q3 = section(
        "301",
        [
            Meeting(
                day="Mon",
                start="1000",
                end="1200",
                start_date="01/11/2027",
                end_date="03/03/2027",
                kind="Lecture",
            )
        ],
        part_of_term="Q3",
    )
    q4 = section(
        "302",
        [
            Meeting(
                day="Mon",
                start="1000",
                end="1200",
                start_date="03/15/2027",
                end_date="05/07/2027",
                kind="Lecture",
            )
        ],
        part_of_term="Q4",
    )
    assert conflicts(q3, q4) == []
    assert find_conflicts([q3, q4]) == []


def test_overlapping_part_of_term_dates_still_conflict():
    q3 = section(
        "311",
        [
            Meeting(
                day="Mon",
                start="1000",
                end="1200",
                start_date="01/11/2027",
                end_date="03/15/2027",
                kind="Lecture",
            )
        ],
        part_of_term="Q3",
    )
    full = section(
        "312",
        [
            Meeting(
                day="Mon",
                start="1000",
                end="1200",
                start_date="01/11/2027",
                end_date="05/07/2027",
                kind="Lecture",
            )
        ],
        part_of_term="1",
    )
    found = conflicts(q3, full)
    assert len(found) == 1
    assert "part Q3 vs 1" in found[0].summary


def test_missing_date_range_is_treated_as_overlapping():
    dated = section(
        "321",
        [
            Meeting(
                day="Mon",
                start="1000",
                end="1200",
                start_date="01/11/2027",
                end_date="03/03/2027",
            )
        ],
    )
    undated = section("322", [Meeting(day="Mon", start="1000", end="1200")])
    assert len(conflicts(dated, undated)) == 1
