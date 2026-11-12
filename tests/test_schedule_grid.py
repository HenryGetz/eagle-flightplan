from datetime import time

from eagle_flightplan.schedule import (
    Meeting,
    ScheduleSection,
    TimeGrid,
    Weekday,
    free_blocks,
    grid_legend,
    render_grid,
)


def build():
    lecture = ScheduleSection(
        crn="22445",
        subject="CYS",
        number="2081",
        title="Data Communications",
        term="202620",
        meetings=(
            Meeting(day=Weekday.TUE, start="1245", end="1425", location="Riley R1332", kind="Lecture"),
            Meeting(day=Weekday.THU, start="1245", end="1425", location="Riley R1332", kind="Lecture"),
        ),
    )
    lab = ScheduleSection(
        crn="22446",
        subject="CYS",
        number="2082",
        title="Networking Lab",
        term="202620",
        meetings=(Meeting(day=Weekday.FRI, start="0800", end="1000", location="Riley R1332", kind="Lab"),),
    )
    tba = ScheduleSection(
        crn="22447", subject="CYS", number="3000", title="Internship", term="202620",
        meetings=(Meeting(day=Weekday.MON, start=None, end=None, kind="Online"),),
    )
    return [lecture, lab, tba]


def test_grid_snapshot():
    grid = render_grid(build(), start=time(8, 0), end=time(15, 0), step_minutes=60)
    assert grid == (
        "      Mon           Tue           Wed           Thu           Fri           Sat           Sun\n"
        "08:00 .             .             .             .             CYS 2082      .             .\n"
        "09:00 .             .             .             .             CYS 2082      .             .\n"
        "10:00 .             .             .             .             .             .             .\n"
        "11:00 .             .             .             .             .             .             .\n"
        "12:00 .             CYS 2081      .             CYS 2081      .             .             .\n"
        "13:00 .             CYS 2081      .             CYS 2081      .             .             .\n"
        "14:00 .             CYS 2081      .             CYS 2081      .             .             .\n"
        "\n"
        "TBA / unscheduled:\n"
        "  CRN 22447 CYS 3000 (Internship)"
    )


def test_grid_without_tba_footer():
    grid = render_grid(build(), start=time(12, 0), end=time(14, 0), include_tba=False)
    assert "TBA" not in grid
    assert "CYS 2081" in grid


def test_free_blocks_inside_window():
    sections = build()
    assert free_blocks(sections, Weekday.TUE, time(8, 0), time(15, 0)) == [
        (time(8, 0), time(12, 45)),
        (time(14, 25), time(15, 0)),
    ]
    assert free_blocks(sections, Weekday.SAT, time(8, 0), time(15, 0)) == [
        (time(8, 0), time(15, 0))
    ]
    assert free_blocks(sections, Weekday.TUE, time(13, 0), time(14, 0)) == []


def test_time_grid_busy_merges_nothing_but_lists_meetings():
    grid = TimeGrid.from_sections(build())
    assert grid.busy(Weekday.TUE) == [(time(12, 45), time(14, 25))]
    assert grid.busy("Fri") == [(time(8, 0), time(10, 0))]


def test_render_grid_checked_rejects_mismatched_crns():
    from eagle_flightplan.schedule import render_grid_checked

    sections = build()[:2]
    assert render_grid_checked(sections, expected_crns={"22445", "22446"})
    try:
        render_grid_checked(sections, expected_crns={"22445", "99999"})
    except ValueError as exc:
        assert "99999" in str(exc)
    else:  # pragma: no cover
        raise AssertionError("mismatched CRNs must raise")


def test_grid_legend_maps_base_codes_and_overlap_slugs():
    a = ScheduleSection(
        crn="1001", subject="CYS", number="4245", title="A", term="202620",
        meetings=(Meeting(day=Weekday.MON, start="1000", end="1100"),),
    )
    b = ScheduleSection(
        crn="1002", subject="BIB", number="4360", title="B", term="202620",
        meetings=(Meeting(day=Weekday.MON, start="1030", end="1130"),),
    )
    legend = grid_legend([a, b])
    assert legend["CYS 4245"] == ["1001"]
    assert legend["BIB 4360"] == ["1002"]
    assert legend["BIB 4360/CYS 4245"] == ["1001", "1002"]
