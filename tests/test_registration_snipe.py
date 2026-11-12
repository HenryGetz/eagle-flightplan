"""Offline tests for snipe target resolution, safety gates and the UI loop."""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from eagle_flightplan.audit.models import (
    Audit,
    CourseRef,
    GroupKind,
    RequirementBlock,
    RequirementGroup,
)
from eagle_flightplan.banner.plan import Plan, PlanCourse
from eagle_flightplan.banner.search import SnapshotMeta, TermSnapshot
from eagle_flightplan.models import Instructor, Section
from eagle_flightplan.schedule import Meeting
from eagle_flightplan.snipe import (
    SafetyError,
    Target,
    authorize_live,
    check_registration_eligible,
    classify_outcome,
    execute_targets,
    probe_page,
    resolve_targets,
)


def section(crn: str, subject: str, number: str) -> Section:
    return Section(
        crn=crn, term="202620", subject=subject, number=number, section="01",
        subject_course=f"{subject}{number}", title=f"{subject} {number}", credits_low=4.0,
        capacity=20, enrollment=0, seats_available=20, open=True,
        faculty=[Instructor(name="Smith, James")],
        meetings=[Meeting(day="Mon", start="10:00", end="11:00")],
    )


def snapshot(*sections: Section) -> TermSnapshot:
    return TermSnapshot(
        meta=SnapshotMeta(term="202620", fetched_at=datetime.now(timezone.utc)),
        sections=list(sections),
    )


def audit_with(requirement_label: str, code: str) -> Audit:
    subject, number = code.split()
    return Audit(
        groups=[
            RequirementGroup(
                id="RA1", title="Major", kind=GroupKind.MAJOR,
                blocks=[RequirementBlock(label=requirement_label, pick_n_of=1, options=[CourseRef(subject=subject, number=number)])],
            )
        ]
    )


def test_authorize_live_requires_flag_and_typed_term():
    with pytest.raises(SafetyError, match="i-understand"):
        authorize_live("202620", i_understand=False, stdin_fn=lambda: "202620\n")
    with pytest.raises(SafetyError, match="Confirmation failed"):
        authorize_live("202620", i_understand=True, stdin_fn=lambda: "202610\n")
    authorize_live("202620", i_understand=True, stdin_fn=lambda: "202620\n")


def test_classify_outcome_phrases():
    assert classify_outcome("Registration Successful") == "registered"
    assert classify_outcome("You are already registered for this section") == "already_registered"
    assert classify_outcome("Section is closed") == "closed"
    assert classify_outcome("Waitlist is available") == "waitlist"
    assert classify_outcome("Prerequisite not met") == "prereq"
    assert classify_outcome("Time conflict with CYS 2081") == "conflict"
    assert classify_outcome("You have a hold") == "hold"
    assert classify_outcome("Please sign in again") == "session_dead"
    assert classify_outcome("something else") == "unknown"


def test_resolve_targets_from_plan_in_priority_order(monkeypatch):
    monkeypatch.setattr("eagle_flightplan.snipe.load_snapshot", lambda term: snapshot(section("1", "CYS", "4245"), section("2", "MTH", "1000")))
    plan = Plan(
        id=1, name="Plan", term="202620",
        courses=[PlanCourse(id=1, subject="MTH", number="1000", crn="2", credits=3.0),
                 PlanCourse(id=2, subject="CYS", number="4245", crn="1", credits=4.0)],
    )

    class FakePlanClient:
        def list_plans(self, term):
            return [plan]

    targets = resolve_targets(
        "202620", plan_name="Plan", plan_client=FakePlanClient(),
        audit=audit_with("Current Practices", "CYS 4245"),
    )
    assert [target.crn for target in targets] == ["1", "2"]
    assert targets[0].priority == "major_required"
    assert targets[1].priority == "unknown"


def test_resolve_explicit_crn_and_unknown(monkeypatch):
    monkeypatch.setattr("eagle_flightplan.snipe.load_snapshot", lambda term: snapshot(section("1", "CYS", "4245")))
    audit = Audit()
    targets = resolve_targets("202620", crns=["1"], audit=audit)
    assert [target.crn for target in targets] == ["1"]
    with pytest.raises(Exception):
        resolve_targets("202620", crns=["999"], audit=audit)


class FakeLocator:
    def __init__(self, page, selector):
        self.page = page
        self.selector = selector

    def count(self):
        return 1 if self.selector in self.page.selectors else 0

    def is_visible(self):
        return self.count() > 0

    @property
    def first(self):
        return self


class FakePage:
    def __init__(self, selectors, body="", bodies=None):
        self.selectors = set(selectors)
        self.bodies = list(bodies) if bodies is not None else [body]
        self.step = 0
        self.fills = []
        self.clicks = []
        self.urls = []
        self.url = "about:blank"
        self.title_text = "Registration"

    def goto(self, url, wait_until=None):
        self.urls.append(url)
        self.url = url

    def wait_for_timeout(self, ms):
        pass

    def locator(self, selector):
        return FakeLocator(self, selector)

    def fill(self, selector, value):
        self.fills.append((selector, value))

    def click(self, selector):
        self.clicks.append(selector)
        self.step += 1

    def inner_text(self, selector):
        return self.bodies[min(self.step, len(self.bodies) - 1)]

    def title(self):
        return self.title_text

    def on(self, event, handler):
        pass


UI = {"#txt_crn", "button:has-text('Add')"}
ADD = "button:has-text('Add')"
SAVE = "#saveButton"


def test_dry_run_fills_every_target_without_clicking():
    page = FakePage(UI, body="Add a course by CRN")
    targets = [Target(crn="22449", course="CYS 4855"), Target(crn="23251", course="COS 4841")]
    result = execute_targets(
        page, targets,
        ssb_base="https://x/ssb", term="202620", dry_run=True, allow_waitlist=False,
        max_attempts=2, timeout_seconds=60,
    )
    assert result.status == "dry_run"
    assert page.fills == [("#txt_crn", "22449"), ("#txt_crn", "23251")]
    assert page.clicks == []
    assert result.attempted == ["22449", "23251"]
    assert "would stage 2 CRN(s)" in result.reason
    assert "click Save" in result.reason


def test_live_stages_then_saves_once_and_reports_registered():
    page = FakePage(UI | {SAVE}, bodies=[
        "",
        "Course added to schedule",
        "Registration Successful",
    ])
    result = execute_targets(
        page, [Target(crn="22449", course="CYS 4855")],
        ssb_base="https://x/ssb", term="202620", dry_run=False, allow_waitlist=False,
        max_attempts=3, timeout_seconds=60, sleep=lambda _: None,
    )
    assert result.status == "registered"
    assert page.clicks == [ADD, SAVE]
    assert "committed 22449 after Save" in result.reason


def test_missing_save_is_staged_not_committed():
    page = FakePage(UI, bodies=["", "Course added to schedule"])
    result = execute_targets(
        page, [Target(crn="22449", course="CYS 4855")],
        ssb_base="https://x/ssb", term="202620", dry_run=False, allow_waitlist=False,
        max_attempts=2, timeout_seconds=60, sleep=lambda _: None,
    )
    assert result.status == "staged_not_committed"
    assert "22449" in result.reason and "Save control not found" in result.reason
    assert page.clicks == [ADD]


def test_save_unknown_when_final_page_is_unclear():
    page = FakePage(UI | {SAVE}, bodies=["", "Course added", "Nothing helpful"])
    result = execute_targets(
        page, [Target(crn="22449", course="CYS 4855")],
        ssb_base="https://x/ssb", term="202620", dry_run=False, allow_waitlist=False,
        max_attempts=2, timeout_seconds=60, sleep=lambda _: None,
    )
    assert result.status == "save_unknown"
    assert "unclear" in result.reason
    assert page.clicks == [ADD, SAVE]


def test_hard_staging_failure_never_clicks_save():
    page = FakePage(UI | {SAVE}, bodies=["", "Section is closed"])
    result = execute_targets(
        page, [Target(crn="22449", course="CYS 4855")],
        ssb_base="https://x/ssb", term="202620", dry_run=False, allow_waitlist=False,
        max_attempts=2, timeout_seconds=60, sleep=lambda _: None,
    )
    assert result.status == "closed"
    assert "22449" in result.reason
    assert page.clicks == [ADD]


def test_unknown_staging_is_a_hard_stop_without_retry():
    page = FakePage(UI | {SAVE}, bodies=["", "Nothing helpful"])
    result = execute_targets(
        page, [Target(crn="22449", course="CYS 4855")],
        ssb_base="https://x/ssb", term="202620", dry_run=False, allow_waitlist=False,
        max_attempts=5, timeout_seconds=60, sleep=lambda _: None,
    )
    assert result.status == "stage_unknown"
    assert page.clicks == [ADD]
    assert "no Save was clicked" in result.reason


def test_ambiguous_loading_stage_retries_to_attempt_cap_then_stops():
    page = FakePage(UI | {SAVE}, bodies=["", "Processing..."])
    result = execute_targets(
        page, [Target(crn="22449", course="CYS 4855")],
        ssb_base="https://x/ssb", term="202620", dry_run=False, allow_waitlist=False,
        max_attempts=3, timeout_seconds=60, sleep=lambda _: None,
    )
    assert result.status == "stage_unknown"
    assert page.clicks == [ADD, ADD, ADD]


def test_authorization_popup_is_detected_and_not_accepted():
    page = FakePage(UI | {SAVE, ".addAuthorizationPopup"}, bodies=["", "Add authorization required"])
    result = execute_targets(
        page, [Target(crn="22449", course="CYS 4855")],
        ssb_base="https://x/ssb", term="202620", dry_run=False, allow_waitlist=False,
        max_attempts=2, timeout_seconds=60, sleep=lambda _: None,
    )
    assert result.status == "authorization_required"
    assert "not accepted" in result.reason
    assert page.clicks == [ADD]


def test_waitlist_not_joined_without_flag():
    page = FakePage(UI, body="Waitlist is available")
    result = execute_targets(
        page, [Target(crn="22449", course="CYS 4855")],
        ssb_base="https://x/ssb", term="202620", dry_run=False, allow_waitlist=False,
        max_attempts=2, timeout_seconds=60, sleep=lambda _: None,
    )
    assert result.status == "waitlist"
    assert page.clicks == [ADD]


def test_waitlist_joined_when_allowed():
    page = FakePage(UI | {"button:has-text('Waitlist')"}, bodies=[
        "",
        "Waitlist is available",
        "Joined waitlist",
    ])
    result = execute_targets(
        page, [Target(crn="22449", course="CYS 4855")],
        ssb_base="https://x/ssb", term="202620", dry_run=False, allow_waitlist=True,
        max_attempts=2, timeout_seconds=60, sleep=lambda _: None,
    )
    assert result.status == "waitlist_joined"
    assert page.clicks == [ADD, "button:has-text('Waitlist')"]


class FakeEligibility:
    def __init__(self, valid: bool, failures=()):
        self.student_elig_valid = valid
        self.failures = list(failures)


class FakeSSB:
    def __init__(self, eligibility: FakeEligibility):
        self.eligibility = eligibility
        self.calls = []

    def set_term(self, term, mode="search"):
        self.calls.append((term, mode))
        return self.eligibility


def test_preflight_not_eligible_uses_banner_failure_text():
    ssb = FakeSSB(FakeEligibility(False, ["Term not eligible for registration"]))
    result = check_registration_eligible("202620", ssb=ssb)
    assert result is not None
    assert result.status == "not_eligible"
    assert result.reason == "Term not eligible for registration"
    assert ssb.calls == [("202620", "registration")]


def test_preflight_not_eligible_without_failures_has_fallback():
    ssb = FakeSSB(FakeEligibility(False, []))
    result = check_registration_eligible("202620", ssb=ssb)
    assert result is not None
    assert result.reason == "term not eligible"


def test_preflight_allows_eligible_term():
    ssb = FakeSSB(FakeEligibility(True))
    assert check_registration_eligible("202620", ssb=ssb) is None
    assert ssb.calls == [("202620", "registration")]


def test_probe_reports_selector_availability():
    ready = probe_page(FakePage(UI, body="Register for classes"), "https://x/ssb")
    assert ready["available"] is True
    assert ready["crn_selector"] == "#txt_crn"
    missing = probe_page(FakePage(set(), body="You must be logged in"), "https://x/ssb")
    assert missing["available"] is False


def test_unrelated_pending_text_is_not_staging_success():
    page = FakePage(UI | {SAVE}, bodies=["", "Pending financial aid review"])
    result = execute_targets(
        page, [Target(crn="22449", course="CYS 4855")],
        ssb_base="https://x/ssb", term="202620", dry_run=False, allow_waitlist=False,
        max_attempts=2, timeout_seconds=60, sleep=lambda _: None,
    )
    assert result.status == "stage_unknown"
    assert page.clicks == [ADD]


def test_crn_pending_schedule_row_counts_as_staged():
    page = FakePage(UI | {SAVE}, bodies=["", "22449 scheduled in your plan", "Registration Successful"])
    result = execute_targets(
        page, [Target(crn="22449", course="CYS 4855")],
        ssb_base="https://x/ssb", term="202620", dry_run=False, allow_waitlist=False,
        max_attempts=2, timeout_seconds=60, sleep=lambda _: None,
    )
    assert result.status == "registered"
    assert page.clicks == [ADD, SAVE]
