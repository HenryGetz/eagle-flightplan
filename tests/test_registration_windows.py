"""Offline tests for registration time-ticket parsing and window helpers."""

from __future__ import annotations

from datetime import datetime

from unw_planner.registration import (
    TIMEZONE,
    countdown,
    next_window,
    parse_failures,
    window,
)

REAL_202610 = [
    "You have no Registration Time Ticket for the current time.",
    "You can register from 04/20/2026 08:30 AM to 10/28/2026 11:59 PM",
]
REAL_202620 = ["Term not eligible for registration"]


def test_real_202610_window():
    result = parse_failures("202610", REAL_202610, False)
    assert result.parse_status == "parsed"
    assert result.eligible is False
    assert result.start is not None and result.end is not None
    assert result.start.isoformat() == "2026-04-20T08:30:00-05:00"
    assert result.end.isoformat() == "2026-10-28T23:59:00-05:00"
    assert result.timezone == TIMEZONE
    assert "no Registration Time Ticket" in result.reason


def test_real_202620_not_eligible():
    result = parse_failures("202620", REAL_202620, False)
    assert result.eligible is False
    assert result.start is None and result.end is None
    assert result.parse_status == "not_eligible"
    assert result.reason == "Term not eligible for registration"


def test_may_register_wording_with_two_pairs():
    result = parse_failures(
        "202710",
        ["You may register from 11/02/2026 07:00 AM to 11/06/2026 11:59 PM and again 01/04/2027 08:00 AM to 01/08/2027 05:00 PM"],
        False,
    )
    assert result.parse_status == "parsed"
    assert result.start.strftime("%Y-%m-%d %H:%M") == "2026-11-02 07:00"
    assert result.end.strftime("%Y-%m-%d %H:%M") == "2026-11-06 23:59"


def test_closed_and_generic_phrasings():
    closed = parse_failures("202620", ["Registration is closed"], False)
    assert closed.parse_status == "not_eligible" and closed.start is None
    generic = parse_failures("202620", ["You may not register at this time."], False)
    assert generic.parse_status == "not_eligible"
    unknown = parse_failures("202620", ["Banner said something new"], False)
    assert unknown.parse_status == "unknown"
    assert unknown.eligible is False
    assert unknown.reason == "Banner said something new"


def test_no_failures_and_eligible():
    ready = parse_failures("202620", [], True)
    assert ready.eligible is True and ready.parse_status == "no_ticket"


def test_window_uses_injected_client():
    class FakeSSB:
        def __init__(self):
            self.calls = []

        def set_term(self, term, mode):
            self.calls.append((term, mode))
            return {"studentEligValid": False, "studentEligFailures": REAL_202610}

    ssb = FakeSSB()
    result = window("202610", ssb=ssb)
    assert ssb.calls == [("202610", "registration")]
    assert result.start is not None


def test_next_window_picks_open_then_soonest():
    class FakeSSB:
        def __init__(self, payloads, terms):
            self.payloads = payloads
            self.terms = terms

        def get_terms(self):
            class T:
                def __init__(self, code):
                    self.code = code
                    self.description = "Term"

            return [T(code) for code in self.terms]

        def set_term(self, term, mode):
            return self.payloads[term]

    future = {"studentEligValid": False, "studentEligFailures": ["You can register from 12/01/2027 08:00 AM to 12/03/2027 11:59 PM"]}
    later = {"studentEligValid": False, "studentEligFailures": ["You can register from 01/01/2028 08:00 AM to 01/03/2028 11:59 PM"]}
    open_term = {"studentEligValid": True, "studentEligFailures": []}
    ssb = FakeSSB({"202720": future, "202810": later, "202620": open_term}, ["202620", "202720", "202810"])
    assert next_window(ssb=ssb).term == "202620"

    ssb2 = FakeSSB({"202720": future, "202810": later}, ["202720", "202810"])
    assert next_window(ssb=ssb2).term == "202720"


def test_countdown_formatting():
    assert countdown(None) == "unknown"
    assert countdown(-5) == "now"
    assert countdown(90) == "1m 30s"
    assert countdown(3 * 86400 + 4 * 3600 + 5 * 60) == "3d 4h 5m"
    assert countdown(0.5) == "now"
