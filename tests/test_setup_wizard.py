"""Offline setup-wizard tests: validation, .env safety, idempotency, redaction."""

from __future__ import annotations

import io
import stat
import sys

import pytest

from eagle_flightplan import setup_cmd
from eagle_flightplan.setup_cmd import (
    Services,
    SetupError,
    SetupOptions,
    WizardIO,
    run_setup,
    validate_password,
    validate_totp_secret,
    validate_user,
    write_env_file,
)

SECRET = "JBSWY3DPEHPK3PXP"
PASSWORD = "correct-horse-battery"


def fake_services(*, run_code=0, chromium=True, login_error=None, mcp=(False, "not available")):
    calls = {"run": [], "login": 0, "mcp": 0}

    def run(command, cwd):
        calls["run"].append(command)
        return run_code, "ok"

    def login(force):
        calls["login"] += 1
        if login_error:
            raise RuntimeError(login_error)
        return setup_cmd.STATE_PATH

    def register():
        calls["mcp"] += 1
        return mcp

    return Services(run=run, check_chromium=lambda: chromium, login=login, register_mcp=register), calls


def make_fake_venv(root):
    bin_dir = root / ".venv" / "bin"
    bin_dir.mkdir(parents=True, exist_ok=True)
    python = bin_dir / "python"
    python.write_text("#!/bin/sh\nexit 0\n")
    python.chmod(0o755)
    return python


def quiet_io(inputs=(), password="", getpass_value=PASSWORD):
    queue = list(inputs)
    output: list[str] = []

    def input_fn(prompt: str) -> str:
        return queue.pop(0) if queue else ""

    def getpass_fn(prompt: str) -> str:
        return getpass_value

    io_obj = WizardIO(input_fn=input_fn, getpass_fn=getpass_fn, print_fn=output.append)
    return io_obj, output


def test_validators():
    assert validate_user(" someone@students.unwsp.edu ") == "someone@students.unwsp.edu"
    with pytest.raises(SetupError, match="does not look like an email"):
        validate_user("not-an-email")
    with pytest.raises(SetupError, match="too short"):
        validate_password("abc")
    assert validate_totp_secret("jbsw y3dp ehpk 3pxp") == SECRET
    with pytest.raises(SetupError, match="email address, not a TOTP secret"):
        validate_totp_secret("someone@students.unwsp.edu")
    with pytest.raises(SetupError, match="not valid base32"):
        validate_totp_secret("!!!!")


def test_env_file_permissions_and_keys(tmp_path):
    path = tmp_path / ".env"
    write_env_file(path, {"UNW_USER": "u@x.edu", "UNW_PASS": PASSWORD, "UNW_TOTP_SECRET": SECRET})
    text = path.read_text()
    assert "UNW_USER=u@x.edu" in text
    assert "UNW_PASS=" in text and "UNW_TOTP_SECRET=" in text
    mode = stat.S_IMODE(path.stat().st_mode)
    assert mode == 0o600


def test_non_interactive_yes_writes_env_without_leaking_secrets(tmp_path, monkeypatch):
    make_fake_venv(tmp_path)
    monkeypatch.setattr(setup_cmd, "STATE_PATH", tmp_path / "state.json")
    (tmp_path / "state.json").write_text("{}")
    services, calls = fake_services()
    io_obj, output = quiet_io()
    monkeypatch.setattr(sys, "stdin", io.StringIO(PASSWORD + "\n"))
    options = SetupOptions(
        root=tmp_path,
        user="student@students.unwsp.edu",
        totp_secret=SECRET,
        password_stdin=True,
        yes=True,
        no_login=True,
    )
    assert run_setup(options, io=io_obj, services=services) == 0
    env = (tmp_path / ".env").read_text()
    assert "student@students.unwsp.edu" in env and SECRET in env and PASSWORD in env
    transcript = "\n".join(output)
    assert PASSWORD not in transcript
    assert SECRET not in transcript
    assert calls["login"] == 0


def test_existing_env_is_kept_without_force(tmp_path, monkeypatch):
    make_fake_venv(tmp_path)
    monkeypatch.setattr(setup_cmd, "STATE_PATH", tmp_path / "state.json")
    (tmp_path / "state.json").write_text("{}")
    env = tmp_path / ".env"
    env.write_text("UNW_USER=old@students.unwsp.edu\nUNW_PASS=oldpass\nUNW_TOTP_SECRET=" + SECRET + "\n")
    services, _ = fake_services()
    io_obj, output = quiet_io()
    options = SetupOptions(root=tmp_path, yes=True, no_login=True)
    assert run_setup(options, io=io_obj, services=services) == 0
    assert "old@students.unwsp.edu" in env.read_text()
    assert "Keeping the existing .env" in "\n".join(output)


def test_existing_env_interactive_decline_keeps_file(tmp_path, monkeypatch):
    make_fake_venv(tmp_path)
    monkeypatch.setattr(setup_cmd, "STATE_PATH", tmp_path / "state.json")
    (tmp_path / "state.json").write_text("{}")
    env = tmp_path / ".env"
    env.write_text("UNW_USER=old@students.unwsp.edu\nUNW_PASS=oldpass\nUNW_TOTP_SECRET=" + SECRET + "\n")
    before = env.read_text()
    services, _ = fake_services()
    io_obj, _ = quiet_io(inputs=["n"])
    assert run_setup(SetupOptions(root=tmp_path, no_login=True), io=io_obj, services=services) == 0
    assert env.read_text() == before


def test_rerun_is_idempotent_and_does_not_duplicate_keys(tmp_path, monkeypatch):
    make_fake_venv(tmp_path)
    monkeypatch.setattr(setup_cmd, "STATE_PATH", tmp_path / "state.json")
    (tmp_path / "state.json").write_text("{}")
    services, _ = fake_services()
    io_obj, _ = quiet_io()
    options = SetupOptions(
        root=tmp_path, user="student@students.unwsp.edu", totp_secret=SECRET,
        password_stdin=True, yes=True, no_login=True, force=True,
    )
    monkeypatch.setattr(sys, "stdin", io.StringIO(PASSWORD + "\n"))
    assert run_setup(options, io=io_obj, services=services) == 0
    first = (tmp_path / ".env").read_text()
    monkeypatch.setattr(sys, "stdin", io.StringIO(PASSWORD + "\n"))
    assert run_setup(options, io=io_obj, services=services) == 0
    second = (tmp_path / ".env").read_text()
    assert first == second
    assert second.count("UNW_USER=") == 1


def test_install_failure_prints_one_next_action(tmp_path, monkeypatch):
    (tmp_path / "pyproject.toml").write_text("")  # force checkout/editable mode
    monkeypatch.setattr(setup_cmd, "STATE_PATH", tmp_path / "state.json")
    (tmp_path / "state.json").write_text("{}")

    def run(command, cwd):
        return 1, "boom"

    services = Services(run=run, check_chromium=lambda: True)
    io_obj, output = quiet_io()
    options = SetupOptions(
        root=tmp_path, user="student@students.unwsp.edu", totp_secret=SECRET,
        password_stdin=True, yes=True, no_login=True,
    )
    monkeypatch.setattr(sys, "stdin", io.StringIO(PASSWORD + "\n"))
    with pytest.raises(SetupError) as excinfo:
        run_setup(options, io=io_obj, services=services)
    assert "Next:" in str(excinfo.value)
    assert "Traceback" not in str(excinfo.value)


def test_login_failure_does_not_raise_and_marks_failure(tmp_path, monkeypatch):
    make_fake_venv(tmp_path)
    monkeypatch.setattr(setup_cmd, "STATE_PATH", tmp_path / "state.json")
    (tmp_path / "state.json").write_text("{}")
    services, calls = fake_services(login_error="MFA changed")
    io_obj, output = quiet_io()
    options = SetupOptions(
        root=tmp_path, user="student@students.unwsp.edu", totp_secret=SECRET,
        password_stdin=True, yes=True,
    )
    monkeypatch.setattr(sys, "stdin", io.StringIO(PASSWORD + "\n"))
    code = run_setup(options, io=io_obj, services=services)
    assert code == 1
    assert calls["login"] == 1
    assert "Login failed" in "\n".join(output)


def test_register_mcp_unavailable_does_not_fail(tmp_path, monkeypatch):
    make_fake_venv(tmp_path)
    monkeypatch.setattr(setup_cmd, "STATE_PATH", tmp_path / "state.json")
    (tmp_path / "state.json").write_text("{}")
    services, calls = fake_services(mcp=(False, "MCP registration is not available in this build."))
    io_obj, output = quiet_io()
    options = SetupOptions(
        root=tmp_path, user="student@students.unwsp.edu", totp_secret=SECRET,
        password_stdin=True, yes=True, no_login=True, register_mcp=True,
    )
    monkeypatch.setattr(sys, "stdin", io.StringIO(PASSWORD + "\n"))
    assert run_setup(options, io=io_obj, services=services) == 0
    assert calls["mcp"] == 1
    assert "not available" in "\n".join(output)


def test_no_login_without_cached_session_exits_zero(tmp_path, monkeypatch):
    make_fake_venv(tmp_path)
    monkeypatch.setattr(setup_cmd, "STATE_PATH", tmp_path / "missing-state.json")
    (tmp_path / ".env").write_text(
        "UNW_USER=student@students.unwsp.edu\n"
        f"UNW_PASS={PASSWORD}\n"
        f"UNW_TOTP_SECRET={SECRET}\n"
    )
    services, calls = fake_services()
    io_obj, output = quiet_io()
    options = SetupOptions(root=tmp_path, yes=True, no_login=True)
    assert run_setup(options, io=io_obj, services=services) == 0
    transcript = "\n".join(output)
    assert "not required with --no-login" in transcript
    assert calls["login"] == 0


def test_noninteractive_without_password_names_password_stdin(tmp_path, monkeypatch):
    make_fake_venv(tmp_path)
    monkeypatch.setattr(setup_cmd, "STATE_PATH", tmp_path / "missing-state.json")
    services, _ = fake_services()
    io_obj, _ = quiet_io()
    monkeypatch.setattr(sys, "stdin", io.StringIO(""))
    options = SetupOptions(
        root=tmp_path,
        user="student@students.unwsp.edu",
        totp_secret=SECRET,
        password_stdin=False,
        yes=True,
        no_login=True,
    )
    with pytest.raises(SetupError) as excinfo:
        run_setup(options, io=io_obj, services=services)
    message = str(excinfo.value)
    assert "--password-stdin" in message
    assert "interactively" in message
    assert "GetpassWarning" not in message


def test_installed_mode_skips_venv_and_pip(tmp_path, monkeypatch):
    # No pyproject.toml => installed/wheel mode; the running tests already use a venv.
    monkeypatch.setattr(setup_cmd, "STATE_PATH", tmp_path / "missing-state.json")
    services, calls = fake_services()
    io_obj, output = quiet_io()
    options = SetupOptions(
        root=tmp_path,
        user="student@students.unwsp.edu",
        totp_secret=SECRET,
        password_stdin=True,
        yes=True,
        no_login=True,
    )
    monkeypatch.setattr(sys, "stdin", io.StringIO(PASSWORD + "\n"))
    assert run_setup(options, io=io_obj, services=services) == 0
    assert not (tmp_path / ".venv").exists()
    assert calls["run"] == []
    assert (tmp_path / ".env").exists()
    transcript = "\n".join(output)
    assert "Installed mode" in transcript
    assert "using installed interpreter" in transcript
    assert str(tmp_path / ".venv") not in transcript


def test_installed_mode_creates_missing_root(tmp_path, monkeypatch):
    root = tmp_path / "fresh-data"
    monkeypatch.setattr(setup_cmd, "STATE_PATH", root / "missing-state.json")
    services, calls = fake_services()
    io_obj, _ = quiet_io()
    options = SetupOptions(
        root=root,
        user="student@students.unwsp.edu",
        totp_secret=SECRET,
        password_stdin=True,
        yes=True,
        no_login=True,
    )
    monkeypatch.setattr(sys, "stdin", io.StringIO(PASSWORD + "\n"))
    assert run_setup(options, io=io_obj, services=services) == 0
    assert (root / ".env").exists()
    assert calls["run"] == []
