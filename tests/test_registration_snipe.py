"""Offline tests for snipe target resolution, safety gates and the UI loop."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

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
    SnipeOptions,
    Target,
    _seats_ready,
    authorize_live,
    check_registration_eligible,
    classify_outcome,
    execute_targets,
    exit_code_for,
    probe_page,
    resolve_targets,
    run_browser,
    select_term,
    should_capture,
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
    assert classify_outcome("You have holds which do not prevent registration") == "unknown"
    assert classify_outcome("full") == "unknown"
    assert classify_outcome("Section is full") == "closed"
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
        "Course added 22449 to schedule",
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
    page = FakePage(UI, bodies=["", "Course added 22449 to schedule"])
    result = execute_targets(
        page, [Target(crn="22449", course="CYS 4855")],
        ssb_base="https://x/ssb", term="202620", dry_run=False, allow_waitlist=False,
        max_attempts=2, timeout_seconds=60, sleep=lambda _: None,
    )
    assert result.status == "staged_not_committed"
    assert "22449" in result.reason and "Save control not found" in result.reason
    assert page.clicks == [ADD]


def test_save_unknown_when_final_page_is_unclear():
    page = FakePage(UI | {SAVE}, bodies=["", "Course added 22449", "Nothing helpful"])
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
    page = FakePage(UI, bodies=["", "Waitlist is available"])
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


def test_select_term_flow_reaches_crn_input():
    page = FakePage(
        {"#s2id_txt_term", "li.select2-results__option:has-text('Spring 2027')", "#term-go", "#txt_crn"},
        body="Select a Term",
    )
    result = select_term(page, "https://x/ssb", "202620", "Spring 2027", sleep=lambda _: None)
    assert result is None
    assert page.urls[0].endswith("/term/termSelection?mode=registration")
    assert "#s2id_txt_term" in page.clicks
    assert "li.select2-results__option:has-text('Spring 2027')" in page.clicks
    assert "#term-go" in page.clicks


def test_select_term_stops_on_alternate_pin():
    page = FakePage({"#input_alt_pin"}, body="Alternate PIN")
    result = select_term(page, "https://x/ssb", "202620", "Spring 2027", sleep=lambda _: None)
    assert result is not None
    assert result.status == "alternate_pin_required"
    assert page.clicks == []


def test_select_term_reports_missing_continue_control():
    page = FakePage(
        {"#s2id_txt_term", "li.select2-results__option:has-text('Spring 2027')"},
        body="Select a Term",
    )
    result = select_term(page, "https://x/ssb", "202620", "Spring 2027", sleep=lambda _: None)
    assert result is not None
    assert result.status == "term_selection_failed"
    assert page.clicks == ["#s2id_txt_term", "li.select2-results__option:has-text('Spring 2027')"]


def test_select_term_reports_not_eligible_before_window():
    page = FakePage(
        {"#s2id_txt_term"},
        body="Term not eligible for registration",
    )
    result = select_term(page, "https://x/ssb", "202620", "Spring 2027", sleep=lambda _: None)
    assert result is not None
    assert result.status == "not_eligible"
    assert "not eligible" in result.reason.lower()


def test_prebrowser_dry_run_does_not_claim_capture(monkeypatch, tmp_path):
    from zoneinfo import ZoneInfo

    import eagle_flightplan.snipe as snipe_mod
    from eagle_flightplan.registration import RegistrationWindow

    tz = ZoneInfo("America/Chicago")
    future = RegistrationWindow(
        term="202620",
        eligible=False,
        start=datetime(2026, 11, 16, 8, 30, tzinfo=tz),
        end=datetime(2027, 3, 22, 23, 59, tzinfo=tz),
        reason="Term not eligible for registration",
        source="banner_prepare",
        parse_status="parsed",
        standing="Senior",
    )
    capture = tmp_path / "never-created.jsonl"
    monkeypatch.setattr(snipe_mod, "window", lambda term, ssb=None: future)
    monkeypatch.setattr(snipe_mod, "_capture_path", lambda term, label: capture)
    monkeypatch.setattr("eagle_flightplan.auth.session_valid", lambda settings=None: True)
    monkeypatch.setattr("eagle_flightplan.auth.ensure", lambda settings=None: object())

    result = run_browser(
        SnipeOptions(term="202620", mode="window", dry_run=True),
        [Target(crn="20010", course="ACC 2101")],
    )

    assert result.status == "dry_run"
    assert result.capture_path is None
    assert not capture.exists()


def test_staging_requires_new_text_for_the_just_submitted_crn():
    page = FakePage(UI | {SAVE}, bodies=[
        "",
        "Course added 11111 to schedule",
        "Course added 11111 to schedule",
    ])
    result = execute_targets(
        page,
        [Target(crn="11111", course="CYS 1000"), Target(crn="22222", course="CYS 2000")],
        ssb_base="https://x/ssb",
        term="202620",
        dry_run=False,
        allow_waitlist=False,
        max_attempts=2,
        timeout_seconds=60,
        sleep=lambda _: None,
    )
    assert result.status == "stage_unknown"
    assert "22222" in result.reason
    assert page.clicks.count(ADD) == 3  # one success + two retries for the stale CRN
    assert SAVE not in page.clicks


def test_outcome_taxonomy_exit_codes():
    for status in (
        "registered",
        "already_registered",
        "waitlist_joined",
        "dry_run",
        "probe_ok",
    ):
        assert exit_code_for(status) == 0, status
    for status in (
        "probe_unavailable",
        "term_option_not_found",
        "term_selection_failed",
        "registration_ui_unavailable",
        "no_seats",
        "alternate_pin_required",
        "run_failed",
        "not_eligible",
        "session_dead",
        "save_unknown",
        "waitlist",
    ):
        assert exit_code_for(status) == 1, status


def test_capture_filter_skips_assets_and_external_hosts():
    assert should_capture(
        "https://registration-ss.oci.unw.edu/StudentRegistrationSsb/ssb/term/search",
        resource_type="xhr",
        content_type="application/json",
    )
    assert should_capture(
        "https://my.unw.edu/api/v1/users/me/",
        resource_type="xhr",
        content_type="application/json",
    )
    assert not should_capture(
        "https://registration-ss.oci.unw.edu/StudentRegistrationSsb/assets/app.js",
        resource_type="script",
        content_type="application/javascript",
    )
    assert not should_capture(
        "https://example.com/api/thing",
        resource_type="xhr",
        content_type="application/json",
    )


class RaisingFillPage(FakePage):
    def fill(self, selector, value):
        raise RuntimeError("fill exploded")


def test_stage_io_failure_returns_run_failed_without_save():
    page = RaisingFillPage(UI | {SAVE}, bodies=[""])
    result = execute_targets(
        page,
        [Target(crn="22449", course="CYS 4855")],
        ssb_base="https://x/ssb",
        term="202620",
        dry_run=True,
        allow_waitlist=False,
        max_attempts=1,
        timeout_seconds=30,
    )
    assert result.status == "run_failed"
    assert "fill exploded" in result.reason
    assert SAVE not in page.clicks


def test_seats_ready_ignores_single_token_course_codes():
    class FakeSSB:
        def search(self, **kwargs):
            raise AssertionError("single-token course must be skipped")

    assert _seats_ready("202620", [Target(crn="1", course="CYS")], FakeSSB()) == []


def test_live_consent_is_enforced_inside_run_browser():
    with pytest.raises(SafetyError, match=r"live and options\.i_understand"):
        run_browser(SnipeOptions(term="202620", dry_run=False, live=False), [])
    with pytest.raises(SafetyError, match=r"live and options\.i_understand"):
        run_browser(
            SnipeOptions(term="202620", dry_run=False, live=True, i_understand=False),
            [],
        )


class FakeCapture:
    def close(self):
        pass


class FakePageForRun:
    def __init__(self):
        self.listeners = []

    def goto(self, url, wait_until=None):
        pass

    def wait_for_timeout(self, ms):
        pass

    def on(self, event, handler):
        self.listeners.append(handler)

    def remove_listener(self, event, handler):
        pass


class FakeContext:
    def __init__(self):
        self.page = FakePageForRun()

    def new_page(self):
        return self.page

    def close(self):
        pass


class FakeBrowser:
    def new_context(self, **kwargs):
        return FakeContext()

    def close(self):
        pass


class FakePlaywright:
    class chromium:
        @staticmethod
        def launch(headless=True):
            return FakeBrowser()

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False


def test_run_browser_refreshes_session_after_prewarm(monkeypatch, tmp_path):
    import eagle_flightplan.snipe as snipe_mod
    from eagle_flightplan.auth import SessionExpiredError
    from eagle_flightplan.registration import TZ, RegistrationWindow

    now = datetime.now(TZ)
    future = RegistrationWindow(
        term="202620",
        eligible=False,
        start=now + timedelta(seconds=5),
        end=now + timedelta(hours=1),
        reason="Term not eligible for registration",
        source="banner_prepare",
        parse_status="parsed",
        standing="Senior",
    )
    calls = {"session_valid": 0, "login": 0, "window": 0, "sleep": []}

    def fake_session_valid(settings=None):
        calls["session_valid"] += 1
        return calls["session_valid"] == 1

    def fake_login(force=False, settings=None, **kwargs):
        calls["login"] += 1
        return tmp_path / "state.json"

    def fake_window(term, ssb=None):
        calls["window"] += 1
        if calls["window"] == 1:
            return future
        raise SessionExpiredError("cached session expired")

    capture = tmp_path / "capture.jsonl"
    monkeypatch.setattr("eagle_flightplan.auth.session_valid", fake_session_valid)
    monkeypatch.setattr("eagle_flightplan.auth.login", fake_login)
    monkeypatch.setattr("eagle_flightplan.auth.ensure", lambda settings=None: object())
    monkeypatch.setattr(snipe_mod, "window", fake_window)
    monkeypatch.setattr(snipe_mod, "_capture_path", lambda term, label: capture)
    monkeypatch.setattr(
        snipe_mod, "capture_traffic", lambda page, path: FakeCapture()
    )
    monkeypatch.setattr(snipe_mod.time, "sleep", lambda seconds: calls["sleep"].append(seconds))
    monkeypatch.setattr("playwright.sync_api.sync_playwright", lambda: FakePlaywright())

    result = run_browser(
        SnipeOptions(
            term="202620",
            dry_run=False,
            live=True,
            i_understand=True,
            prewarm_seconds=0,
            timeout_seconds=5,
        ),
        [],
    )

    assert result.status == "session_dead"
    assert calls["login"] == 1
    assert calls["session_valid"] >= 2
    assert calls["window"] == 2
    assert calls["sleep"]
    assert result.capture_path == str(capture)
