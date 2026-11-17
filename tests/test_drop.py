"""Offline tests for confirmed drops/withdrawals (no network, no browser)."""

from __future__ import annotations

import json

import pytest

from eagle_flightplan import mcp_server
from eagle_flightplan.drop import (
    DROP_ACTION,
    DropResult,
    DropSafetyError,
    RegisteredCourse,
    authorize_live_drop,
    classify_submit_payload,
    execute_drop,
    execute_withdraw,
    finalize_drop,
)

TERM = "202620"
ENDPOINT = "/StudentRegistrationSsb/ssb/classRegistration/submitRegistration"


def course(crn: str, course_code: str = "CYS 4855", credits: float = 2.0) -> RegisteredCourse:
    return RegisteredCourse(
        crn=crn, course=course_code, title="Senior Capstone", credits=credits, term=TERM
    )


class FakeCollection:
    def __init__(self, items):
        self.items = list(items)

    def count(self):
        return len(self.items)

    def nth(self, index):
        return self.items[index]


class FakeSelect:
    def __init__(self, page, crn: str, term: str = TERM):
        self.page = page
        self.crn = crn
        self.term = term
        self.value = "null"

    def count(self):
        return 1 if self.crn in self.page.rows else 0

    def get_attribute(self, name):
        return {
            "data-term": self.term,
            "data-coursereferencenumber": self.crn,
            "data-property": "selectedAction",
        }.get(name)

    def evaluate(self, _script, arg=None):
        if arg is not None:
            assert arg == DROP_ACTION
            self.value = arg
            self.page.selections.append((self.crn, arg))
            self.page.selected_values[self.crn] = arg
            return None
        return self.page.rows.get(self.crn, {}).get("status", "")

    def input_value(self):
        return self.value


class FakeSave:
    def __init__(self, page):
        self.page = page

    def count(self):
        return 1

    def get_attribute(self, name):
        if name == "data-registration-endpoint":
            return self.page.endpoint
        return None

    def is_disabled(self):
        enabled = any(value == DROP_ACTION for value in self.page.selected_values.values())
        return not (enabled and self.page.allow_enable)

    def click(self):
        self.page.clicks.append("Submit")
        if self.page.response is not None:
            self.page.responses.append(
                {
                    "status": 200,
                    "url": f"{self.page.endpoint}/batch",
                    "json": self.page.response,
                    "body": json.dumps(self.page.response),
                }
            )
        for crn, status in self.page.after.items():
            if status is None:
                self.page.rows.pop(crn, None)
            elif crn in self.page.rows:
                self.page.rows[crn]["status"] = status


class FakePage:
    def __init__(
        self,
        rows,
        *,
        response=None,
        after=None,
        endpoint=ENDPOINT,
        allow_enable=True,
        term=TERM,
    ):
        self.term = term
        self.rows = {crn: {"term": term, "status": status} for crn, status in rows.items()}
        self.response = response
        self.after = after or {}
        self.endpoint = endpoint
        self.allow_enable = allow_enable
        self.responses: list[dict] = []
        self.selections: list[tuple[str, str]] = []
        self.selected_values: dict[str, str] = {}
        self.clicks: list[str] = []
        self.waits = 0

    def locator(self, selector):
        if selector == "select.actionDdl":
            return FakeCollection([FakeSelect(self, crn, self.term) for crn in self.rows])
        if selector.startswith("#action-") and selector.endswith("-ddl"):
            crn = selector[len("#action-") : -len("-ddl")]
            return FakeSelect(self, crn, self.term)
        if selector == "#saveButton":
            return FakeSave(self)
        return FakeCollection([])

    def wait_for_timeout(self, _ms):
        self.waits += 1


def submit_payload(status_indicator="D", *, status_text="Web Dropped", crn="22449"):
    return {
        "data": {
            "create": [],
            "update": [
                {
                    "id": 2256199,
                    "courseReferenceNumber": crn,
                    "term": TERM,
                    "statusIndicator": status_indicator,
                    "statusDescription": status_text,
                    "removeIndicator": None,
                    "messages": [],
                }
            ],
            "destroy": [],
        }
    }


# --------------------------------------------------------------------------- #
# UI execution
# --------------------------------------------------------------------------- #
def test_drop_dry_run_selects_nothing_and_never_submits():
    page = FakePage({"22449": "Registered"})
    result = execute_drop(page, TERM, "22449", dry_run=True)

    assert result.status == "dry_run"
    assert DROP_ACTION in result.reason
    assert page.selections == []
    assert page.clicks == []


def test_drop_refuses_a_crn_that_is_not_registered():
    page = FakePage({"20737": "Registered", "22449": "Pending"})
    result = execute_drop(page, TERM, "22449", responses=[])

    assert result.status == "not_registered"
    assert "not currently Registered" in result.reason
    assert page.selections == []
    assert page.clicks == []

    missing = execute_drop(page, TERM, "99999", responses=[])
    assert missing.status == "not_registered"
    assert page.selections == []
    assert page.clicks == []


def test_drop_touches_only_the_exact_crn_and_submits_once():
    page = FakePage(
        {"20737": "Registered", "22449": "Registered", "21500": "Registered"},
        response=submit_payload(),
        after={"22449": None},
    )
    result = execute_drop(page, TERM, "22449", responses=page.responses)

    assert page.selections == [("22449", DROP_ACTION)]
    assert page.selected_values.get("20737") is None
    assert page.selected_values.get("21500") is None
    assert page.clicks == ["Submit"]
    assert result.status == "submitted"
    assert result.response_status == "dropped"
    assert result.writes_performed == ["drop_course"]
    assert result.submit_url == f"{ENDPOINT}/batch"


def test_drop_does_not_submit_without_the_canonical_endpoint():
    page = FakePage({"22449": "Registered"}, endpoint="/somewhere/else")
    result = execute_drop(page, TERM, "22449", responses=page.responses)

    assert result.status == "registration_ui_unavailable"
    assert "endpoint" in result.reason
    assert page.selections == []
    assert page.clicks == []


def test_drop_does_not_submit_when_submit_never_enables():
    page = FakePage({"22449": "Registered"}, allow_enable=False)
    result = execute_drop(page, TERM, "22449", responses=page.responses, commit_timeout=0)

    assert result.status == "drop_not_submitted"
    assert page.clicks == []
    assert result.writes_performed == []


# --------------------------------------------------------------------------- #
# result classification / read-back
# --------------------------------------------------------------------------- #
def test_submit_confirm_prompt_is_not_accepted_and_not_retried():
    page = FakePage(
        {"22449": "Registered"},
        response={
            "message": {
                "type": "confirm",
                "message": "Dropping this course will reduce your hours. Continue?",
            }
        },
        after={"22449": None},
    )
    execution = execute_drop(page, TERM, "22449", responses=page.responses)
    assert execution.response_status == "confirm"
    assert page.clicks == ["Submit"]

    result = finalize_drop("drop", TERM, "22449", execution, [course("22449")], [])
    assert result.status == "drop_confirm_required"
    assert page.clicks == ["Submit"]


def test_banner_rejection_keeps_the_exact_message():
    message = "You may not drop this course because a hold is active."
    page = FakePage(
        {"22449": "Registered"},
        response={"message": {"type": "exception", "message": message}},
        after={"22449": "Registered"},
    )
    execution = execute_drop(page, TERM, "22449", responses=page.responses)
    result = finalize_drop(
        "drop", TERM, "22449", execution, [course("22449")], [course("22449")]
    )

    assert result.status == "drop_rejected"
    assert result.banner_message == message
    assert page.clicks == ["Submit"]


def test_unknown_outcome_is_not_promoted_and_not_retried():
    page = FakePage({"22449": "Registered"}, response=None, after={})
    execution = execute_drop(page, TERM, "22449", responses=page.responses)
    assert execution.response_status == "unknown"
    result = finalize_drop(
        "drop", TERM, "22449", execution, [course("22449")], [course("22449")]
    )

    assert result.status == "drop_unknown"
    assert "do not retry" in result.reason.lower()
    assert page.clicks == ["Submit"]


def test_successful_drop_uses_the_read_back_diff():
    execution = DropResult(
        status="submitted",
        action="drop",
        term=TERM,
        crn="22449",
        response_status="dropped",
        page_status="dropped",
        writes_performed=["drop_course"],
    )
    before = [course("20737", "PHI 2016", 4.0), course("22449", "CYS 4855", 2.0)]
    result = finalize_drop("drop", TERM, "22449", execution, before, [before[0]])

    assert result.status == "dropped"
    assert [row.crn for row in result.removed] == ["22449"]
    assert [row.crn for row in result.remaining] == ["20737"]
    assert result.after_read_error == ""


def test_response_success_but_read_back_still_registered_is_unconfirmed():
    execution = DropResult(
        status="submitted",
        term=TERM,
        crn="22449",
        response_status="dropped",
        page_status="dropped",
        writes_performed=["drop_course"],
    )
    result = finalize_drop(
        "drop", TERM, "22449", execution, [course("22449")], [course("22449")]
    )

    assert result.status == "drop_unconfirmed"
    assert "do not resubmit" in result.reason.lower()
    assert result.writes_performed == ["drop_course"]


def test_classify_submit_payload_shapes():
    assert classify_submit_payload(None) == ("unknown", "")
    assert classify_submit_payload(
        {"message": {"type": "ok", "message": "Course 22449 has been dropped."}}
    )[0] == "dropped"
    assert classify_submit_payload({"message": "HTTP 500"})[0] == "rejected"
    assert classify_submit_payload(
        {"data": {"update": [{"id": 1, "statusIndicator": "D"}]}}
    )[0] == "dropped"
    assert classify_submit_payload(
        {"data": {"update": [{"id": 1, "statusIndicator": "R"}]}}
    )[0] == "unknown"


# --------------------------------------------------------------------------- #
# withdraw
# --------------------------------------------------------------------------- #
def test_withdraw_requires_the_page_to_match_the_enumerated_schedule():
    page = FakePage({"22449": "Registered", "20737": "Registered"})
    result = execute_withdraw(
        page, TERM, expected_crns=["22449", "20737", "21500"], responses=page.responses
    )

    assert result.status == "schedule_changed"
    assert "21500" in result.reason
    assert page.selections == []
    assert page.clicks == []


def test_withdraw_selects_every_registered_row_once():
    page = FakePage(
        {"22449": "Registered", "20737": "Registered", "21500": "Registered"},
        response={
            "data": {
                "create": [],
                "update": [
                    {"id": 1, "courseReferenceNumber": crn, "statusIndicator": "D"}
                    for crn in ("22449", "20737", "21500")
                ],
                "destroy": [],
            }
        },
        after={"22449": None, "20737": None, "21500": None},
    )
    result = execute_withdraw(
        page,
        TERM,
        expected_crns=["22449", "20737", "21500"],
        responses=page.responses,
    )

    assert sorted(page.selected_values) == ["20737", "21500", "22449"]
    assert page.clicks == ["Submit"]
    assert result.response_status == "dropped"
    assert result.writes_performed == ["withdraw"]


def test_withdraw_partial_reports_survivors_without_retry():
    execution = DropResult(
        status="submitted",
        action="withdraw",
        term=TERM,
        response_status="dropped",
        writes_performed=["withdraw"],
    )
    before = [course("22449"), course("20737", "PHI 2016", 4.0)]
    result = finalize_drop("withdraw", TERM, None, execution, before, [before[1]])

    assert result.status == "withdraw_partial"
    assert "22449" in result.reason
    assert "20737" in result.reason


# --------------------------------------------------------------------------- #
# MCP guards
# --------------------------------------------------------------------------- #
def test_drop_guard_refuses_without_confirm_and_names_the_effect(monkeypatch):
    rows = [course("22449"), course("20737", "PHI 2016", 4.0)]
    monkeypatch.setattr(mcp_server.drop_ops, "read_schedule", lambda term: list(rows))
    monkeypatch.setattr(
        mcp_server.drop_ops,
        "run_drop",
        lambda *args, **kwargs: pytest.fail("a refused drop must not run"),
    )

    payload = mcp_server.drop_course_guarded(TERM, "22449", False)

    assert payload["refused"] is True
    assert payload["confirmation_required"] is True
    assert payload["writes_performed"] == []
    assert "would DROP" in payload["intended_effect"]
    assert "CRN 22449" in payload["intended_effect"]
    assert payload["required_confirmation"] == {"confirm": True}
    assert payload["current_state"]["count"] == 2


def test_drop_guard_rejects_an_unregistered_crn_even_with_confirm(monkeypatch):
    monkeypatch.setattr(
        mcp_server.drop_ops, "read_schedule", lambda term: [course("22449")]
    )
    monkeypatch.setattr(
        mcp_server.drop_ops,
        "run_drop",
        lambda *args, **kwargs: pytest.fail("a non-registered CRN must not run"),
    )

    with pytest.raises(ValueError, match="not currently registered"):
        mcp_server.drop_course_guarded(TERM, "99999", True)


def test_drop_guard_returns_the_read_back_diff(monkeypatch):
    before = [course("22449"), course("20737", "PHI 2016", 4.0)]
    monkeypatch.setattr(
        mcp_server.drop_ops, "read_schedule", lambda term: list(before)
    )
    monkeypatch.setattr(
        mcp_server.drop_ops,
        "run_drop",
        lambda options, **kwargs: DropResult(
            status="dropped",
            action="drop",
            term=TERM,
            crn="22449",
            banner_message="Course 22449 has been dropped.",
            schedule_before=before,
            schedule_after=[before[1]],
            removed=[before[0]],
            remaining=[before[1]],
            writes_performed=["drop_course"],
        ),
    )

    payload = mcp_server.drop_course_guarded(TERM, "22449", True)

    assert payload["ok"] is True
    assert payload["status"] == "dropped"
    assert payload["writes_performed"] == ["drop_course"]
    assert payload["banner_message"] == "Course 22449 has been dropped."
    assert [row["crn"] for row in payload["diff"]["rows_removed"]] == ["22449"]
    assert payload["diff"]["credits_before"] == 6.0
    assert payload["diff"]["credits_after"] == 4.0
    assert payload["schedule_after"]["count"] == 1


def test_withdraw_guard_requires_confirm_and_the_exact_term(monkeypatch):
    rows = [course("22449"), course("20737", "PHI 2016", 4.0)]
    monkeypatch.setattr(mcp_server.drop_ops, "read_schedule", lambda term: list(rows))
    monkeypatch.setattr(
        mcp_server.drop_ops,
        "run_drop",
        lambda *args, **kwargs: pytest.fail("a refused withdraw must not run"),
    )

    no_confirm = mcp_server.withdraw_guarded(TERM, False, None)
    assert no_confirm["refused"] is True
    assert no_confirm["writes_performed"] == []
    assert "WITHDRAW ALL 2" in no_confirm["intended_effect"]
    assert no_confirm["required_confirmation"] == {"confirm": True, "confirm_term": TERM}

    wrong_term = mcp_server.withdraw_guarded(TERM, True, "202610")
    assert wrong_term["refused"] is True
    assert "confirm_term" in wrong_term["error"]


def test_withdraw_guard_runs_only_with_exact_confirmation(monkeypatch):
    before = [course("22449"), course("20737", "PHI 2016", 4.0)]
    monkeypatch.setattr(mcp_server.drop_ops, "read_schedule", lambda term: list(before))
    calls = []

    def fake_run(options, **kwargs):
        calls.append(options)
        return DropResult(
            status="withdrawn",
            action="withdraw",
            term=TERM,
            schedule_before=before,
            schedule_after=[],
            removed=before,
            remaining=[],
            writes_performed=["withdraw"],
        )

    monkeypatch.setattr(mcp_server.drop_ops, "run_drop", fake_run)

    payload = mcp_server.withdraw_guarded(TERM, True, TERM)

    assert payload["ok"] is True
    assert payload["status"] == "withdrawn"
    assert payload["writes_performed"] == ["withdraw"]
    assert payload["diff"]["course_count_after"] == 0
    assert len(payload["diff"]["rows_removed"]) == 2
    assert calls and calls[0].action == "withdraw"


# --------------------------------------------------------------------------- #
# CLI consent
# --------------------------------------------------------------------------- #
def test_authorize_live_drop_requires_consent_and_the_typed_term():
    with pytest.raises(DropSafetyError, match="i-understand"):
        authorize_live_drop(TERM, i_understand=False, confirm_term=TERM, isatty=False)

    with pytest.raises(DropSafetyError, match="Confirmation failed"):
        authorize_live_drop(
            TERM,
            i_understand=True,
            confirm_term=TERM,
            stdin_fn=lambda: "202610\n",
            isatty=True,
        )

    authorize_live_drop(
        TERM,
        i_understand=True,
        confirm_term=None,
        stdin_fn=lambda: f"{TERM}\n",
        isatty=True,
    )

    with pytest.raises(DropSafetyError, match="--confirm-term"):
        authorize_live_drop(TERM, i_understand=True, confirm_term="202610", isatty=False)

    authorize_live_drop(TERM, i_understand=True, confirm_term=TERM, isatty=False)


def test_run_drop_withdraw_aborts_when_the_confirmed_set_changed(monkeypatch):
    from eagle_flightplan import drop as drop_ops
    from eagle_flightplan.drop import DropOptions, run_drop

    monkeypatch.setattr("eagle_flightplan.auth.session_valid", lambda settings=None: True)
    monkeypatch.setattr("eagle_flightplan.auth.ensure", lambda settings=None: object())
    monkeypatch.setattr(
        drop_ops, "read_schedule", lambda term, ssb=None: [course("22449")]
    )

    result = run_drop(
        DropOptions(
            term=TERM,
            action="withdraw",
            expected_crns=["22449", "20737"],
        )
    )

    assert result.status == "schedule_changed"
    assert "20737" in result.reason
    assert result.writes_performed == []


def test_read_back_failure_never_claims_success():
    execution = DropResult(
        status="submitted",
        term=TERM,
        crn="22449",
        response_status="dropped",
        writes_performed=["drop_course"],
    )
    result = finalize_drop(
        "drop",
        TERM,
        "22449",
        execution,
        [course("22449")],
        None,
        after_read_error="SessionExpiredError: boom",
    )

    assert result.status == "drop_unconfirmed"
    assert result.after_read_error
    assert "do not resubmit" in result.reason.lower()


def test_pre_submit_failure_is_not_reinterpreted_as_an_unknown_drop():
    execution = DropResult(
        status="run_failed",
        term=TERM,
        crn="22449",
        reason="TimeoutError: browser exploded",
        writes_performed=[],
    )
    result = finalize_drop(
        "drop", TERM, "22449", execution, [course("22449")], [course("22449")]
    )

    assert result.status == "run_failed"
    assert result.writes_performed == []
    assert "browser exploded" in result.reason
