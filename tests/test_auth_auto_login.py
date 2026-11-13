"""UNW_AUTO_LOGIN must gate every implicit login path."""

from __future__ import annotations

from pathlib import Path

import httpx
import pytest
from typer.testing import CliRunner

from eagle_flightplan import auth
from eagle_flightplan.cli import app
from eagle_flightplan.snipe import SnipeOptions, run_browser

runner = CliRunner()


def _expired_response(request: httpx.Request) -> httpx.Response:
    return httpx.Response(
        200,
        text='<html><head><meta name="fullName" content=""></head></html>',
        headers={"content-type": "text/html"},
    )


def test_ensure_with_auto_login_disabled_raises_without_login(monkeypatch):
    monkeypatch.setenv("UNW_AUTO_LOGIN", "0")
    monkeypatch.setattr(auth, "session_valid", lambda settings=None: False)
    calls: list[str] = []
    monkeypatch.setattr(auth, "login", lambda *a, **k: calls.append("login"))
    monkeypatch.setattr(
        auth, "httpx_client", lambda *a, **k: pytest.fail("client must not be built")
    )

    with pytest.raises(auth.SessionExpiredError) as exc:
        auth.ensure()

    assert calls == []
    assert "UNW_AUTO_LOGIN=0" in str(exc.value)
    assert "login --force" in str(exc.value)


def test_ensure_with_auto_login_enabled_logs_in_once(monkeypatch):
    monkeypatch.delenv("UNW_AUTO_LOGIN", raising=False)
    monkeypatch.setattr(auth, "session_valid", lambda settings=None: False)
    calls: list[str] = []

    def fake_login(force=False, settings=None):
        calls.append("login")
        return Path("/tmp/state.json")

    client = object()
    monkeypatch.setattr(auth, "login", fake_login)
    monkeypatch.setattr(auth, "httpx_client", lambda *a, **k: client)

    assert auth.ensure() is client
    assert calls == ["login"]


def test_explicit_cli_login_works_with_auto_login_disabled(monkeypatch):
    monkeypatch.setenv("UNW_AUTO_LOGIN", "0")
    seen: dict[str, bool] = {}

    def fake_login(force=False):
        seen["force"] = force
        return Path("/tmp/state.json")

    monkeypatch.setattr(auth, "login", fake_login)
    monkeypatch.setattr(auth, "session_valid", lambda settings=None: True)

    result = runner.invoke(app, ["login", "--force"])

    assert result.exit_code == 0
    assert seen["force"] is True


def test_request_refresh_with_auto_login_disabled_does_not_login(monkeypatch):
    monkeypatch.setenv("UNW_AUTO_LOGIN", "0")
    calls: list[str] = []
    monkeypatch.setattr(auth, "login", lambda *a, **k: calls.append("login"))
    client = httpx.Client(transport=httpx.MockTransport(_expired_response))

    with pytest.raises(auth.SessionExpiredError):
        auth.request_with_refresh(client, "GET", "https://example.test/ssb")
    assert calls == []
    client.close()


def test_heal_session_with_auto_login_disabled_does_not_login(monkeypatch):
    monkeypatch.setenv("UNW_AUTO_LOGIN", "0")
    calls: list[str] = []
    monkeypatch.setattr(auth, "login", lambda *a, **k: calls.append("login"))

    with pytest.raises(auth.SessionExpiredError):
        auth.heal_session()
    assert calls == []


def test_snipe_with_auto_login_disabled_returns_session_dead(monkeypatch):
    monkeypatch.setenv("UNW_AUTO_LOGIN", "0")
    monkeypatch.setattr(auth, "session_valid", lambda settings=None: False)
    calls: list[str] = []
    monkeypatch.setattr(auth, "login", lambda *a, **k: calls.append("login"))

    result = run_browser(SnipeOptions(term="202620", dry_run=True), [])

    assert result.status == "session_dead"
    assert "UNW_AUTO_LOGIN=0" in result.reason
    assert "login --force" in result.reason
    assert calls == []
