"""Retry/backoff and session-auto-heal tests (offline, MockTransport)."""

from __future__ import annotations

import httpx
import pytest

from flightplan import auth
from flightplan.banner.ssb import SSBClient, SSBError

BASE = "https://registration-ss.oci.unw.edu/StudentRegistrationSsb/ssb"
TERMS = [{"code": "202620", "description": "Spring 2027"}]
GUEST = '<html><head><meta name="fullName" content=""></head><body>guest</body></html>'
HTML = {"content-type": "text/html;charset=UTF-8"}
JSON = {"content-type": "application/json"}


def make_client(handler, **kwargs):
    http = httpx.Client(transport=httpx.MockTransport(handler), base_url=BASE)
    return SSBClient(http, min_interval=0, sleep=kwargs.pop("sleep", lambda _: None), **kwargs)


def test_retry_then_success(caplog):
    calls = {"n": 0}
    sleeps: list[float] = []

    def handler(request):
        calls["n"] += 1
        if calls["n"] <= 2:
            return httpx.Response(503, text="busy", headers=HTML)
        return httpx.Response(200, json=TERMS, headers=JSON)

    client = make_client(handler, sleep=sleeps.append)
    terms = client.get_terms()
    assert terms[0].code == "202620"
    assert calls["n"] == 3
    assert len(sleeps) == 2


def test_retry_exhausted_returns_error_response():
    calls = {"n": 0}

    def handler(request):
        calls["n"] += 1
        return httpx.Response(502, text="bad gateway", headers=HTML)

    client = make_client(handler)
    with pytest.raises(SSBError):
        client.get_terms()
    assert calls["n"] == 3


def test_non_idempotent_write_is_never_retried():
    calls = {"n": 0}

    def handler(request):
        calls["n"] += 1
        return httpx.Response(503, text="busy", headers=HTML)

    client = make_client(handler)
    response = client._request("POST", "/plan/submitPlan/batch", idempotent=False)
    assert response.status_code == 503
    assert calls["n"] == 1


def test_retry_after_is_honoured():
    calls = {"n": 0}
    sleeps: list[float] = []

    def handler(request):
        calls["n"] += 1
        if calls["n"] == 1:
            return httpx.Response(429, headers={**HTML, "retry-after": "1.5"}, text="slow down")
        return httpx.Response(200, json=TERMS, headers=JSON)

    client = make_client(handler, sleep=sleeps.append)
    client.get_terms()
    assert calls["n"] == 2
    assert sleeps == [1.5]


def test_expired_session_heals_once_and_retries(monkeypatch):
    calls = {"n": 0}
    heals = {"n": 0}

    def handler(request):
        calls["n"] += 1
        if calls["n"] == 1:
            return httpx.Response(200, text=GUEST, headers=HTML)
        return httpx.Response(200, json=TERMS, headers=JSON)

    monkeypatch.setattr(auth, "heal_session", lambda *a, **k: heals.__setitem__("n", heals["n"] + 1))
    monkeypatch.setattr(auth, "_refresh_cookies", lambda client: None)
    client = make_client(handler)
    terms = client.get_terms()
    assert terms[0].code == "202620"
    assert heals["n"] == 1
    assert calls["n"] == 2


def test_auto_login_disabled_raises_manual_fix(monkeypatch):
    calls = {"n": 0}
    heals = {"n": 0}

    def handler(request):
        calls["n"] += 1
        return httpx.Response(200, text=GUEST, headers=HTML)

    monkeypatch.setenv("UNW_AUTO_LOGIN", "0")
    monkeypatch.setattr(auth, "heal_session", lambda *a, **k: heals.__setitem__("n", heals["n"] + 1))
    client = make_client(handler)
    with pytest.raises(auth.SessionExpiredError) as excinfo:
        client.get_terms()
    assert "-m flightplan.cli login --force" in str(excinfo.value)
    assert calls["n"] == 1
    assert heals["n"] == 0


def test_login_failure_surfaces_original_error(monkeypatch):
    calls = {"n": 0}

    def handler(request):
        calls["n"] += 1
        return httpx.Response(200, text=GUEST, headers=HTML)

    def explode(*args, **kwargs):
        raise RuntimeError("MFA page changed")

    monkeypatch.setattr(auth, "heal_session", explode)
    client = make_client(handler)
    with pytest.raises(auth.SessionExpiredError) as excinfo:
        client.get_terms()
    message = str(excinfo.value)
    assert "MFA page changed" in message
    assert "-m flightplan.cli login --force" in message
    assert calls["n"] == 1


def test_followed_login_page_without_fullname_meta_is_expired(monkeypatch):
    calls = {"n": 0}
    heals = {"n": 0}
    idp_page = "<html><body>" + ("login form " * 3000) + "</body></html>"

    def handler(request):
        calls["n"] += 1
        if calls["n"] == 1:
            return httpx.Response(200, text=idp_page, headers=HTML)
        return httpx.Response(200, json=TERMS, headers=JSON)

    monkeypatch.setattr(auth, "heal_session", lambda *a, **k: heals.__setitem__("n", heals["n"] + 1))
    monkeypatch.setattr(auth, "_refresh_cookies", lambda client: None)
    client = make_client(handler)
    assert client.get_terms()[0].code == "202620"
    assert heals["n"] == 1 and calls["n"] == 2
