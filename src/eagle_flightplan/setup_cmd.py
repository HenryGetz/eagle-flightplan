"""Interactive setup wizard for non-coders (cross-platform, no bash required).

`eagle-flightplan setup` explains, installs, stores credentials in `.env` (mode 600),
runs doctor checks, and optionally logs in. It is idempotent: re-running keeps an
existing `.env` unless `--force`, and skips steps that are already done.
"""

from __future__ import annotations

import base64
import getpass
import importlib.util
import os
import re
import shlex
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Iterable
from urllib.parse import parse_qs, urlparse

import pyotp
from pydantic import BaseModel

from .config import REPO_ROOT, STATE_PATH, login_command

MIN_PYTHON = (3, 11)
MIN_PASSWORD_LENGTH = 6
ENV_KEYS = ("UNW_USER", "UNW_PASS", "UNW_TOTP_SECRET")
ENV_OPTIONAL = {
    "UNW_AUDIT_URL": "https://degreeaudit.oci.unw.edu/worksheets/WEB31",
    "UNW_SSB_BASE": "https://registration-ss.oci.unw.edu/StudentRegistrationSsb/ssb",
}


class SetupError(RuntimeError):
    """A setup step failed; the message always contains the next action."""


class SetupOptions(BaseModel):
    root: Path = REPO_ROOT
    user: str | None = None
    totp_secret: str | None = None
    password_stdin: bool = False
    yes: bool = False
    force: bool = False
    no_login: bool = False
    register_mcp: bool = False

    model_config = {"arbitrary_types_allowed": True}


class Check(BaseModel):
    name: str
    ok: bool
    detail: str = ""


@dataclass
class Services:
    """Injectable side effects so tests never launch a browser or hit the network."""

    run: Callable[[list[str], Path], tuple[int, str]] = field(default=None)  # type: ignore[assignment]
    check_chromium: Callable[[], bool] = field(default=None)  # type: ignore[assignment]
    login: Callable[[bool], Path] = field(default=None)  # type: ignore[assignment]
    register_mcp: Callable[[], tuple[bool, str]] = field(default=None)  # type: ignore[assignment]

    def __post_init__(self) -> None:
        if self.run is None:
            self.run = _run_streaming
        if self.check_chromium is None:
            self.check_chromium = chromium_installed
        if self.login is None:
            self.login = _default_login
        if self.register_mcp is None:
            self.register_mcp = _default_register_mcp


class WizardIO:
    def __init__(
        self,
        *,
        input_fn: Callable[[str], str] = input,
        getpass_fn: Callable[[str], str] = getpass.getpass,
        print_fn: Callable[[str], None] = print,
    ) -> None:
        self.input = input_fn
        self.getpass = getpass_fn
        self.print = print_fn

    def ask(self, prompt: str, default: str | None = None) -> str:
        suffix = f" [{default}]" if default else ""
        answer = self.input(f"{prompt}{suffix}: ").strip()
        return answer or (default or "")

    def confirm(self, prompt: str, default: bool = True) -> bool:
        suffix = " [Y/n]" if default else " [y/N]"
        answer = self.input(f"{prompt}{suffix}: ").strip().lower()
        if not answer:
            return default
        return answer in {"y", "yes"}


# --------------------------------------------------------------------------- #
# validation
# --------------------------------------------------------------------------- #
def validate_user(value: str) -> str:
    text = value.strip()
    if not text:
        raise SetupError("Enter your UNW email, for example you@students.unwsp.edu.")
    if " " in text or "@" not in text or "." not in text.split("@")[-1]:
        raise SetupError(
            f"{text!r} does not look like an email. Use your full UNW address, "
            "for example you@students.unwsp.edu."
        )
    return text


def extract_totp_secret(value: str) -> str:
    """Accept a bare base32 secret or an otpauth:// URI pasted from a QR decode."""
    text = value.strip()
    if text.lower().startswith("otpauth://"):
        query = parse_qs(urlparse(text).query)
        secret = (query.get("secret") or [""])[0]
        if not secret:
            raise SetupError("That otpauth link has no secret; paste the base32 key instead.")
        text = secret
    return re.sub(r"\s+", "", text).upper().rstrip("=")


def validate_totp_secret(value: str) -> str:
    if "@" in value:
        raise SetupError(
            "That looks like an email address, not a TOTP secret. Paste the base32 key "
            "from the authenticator setup screen (letters and digits 2-7 only)."
        )
    secret = extract_totp_secret(value)
    if not secret:
        raise SetupError("Enter the TOTP secret from your authenticator app.")
    try:
        padded = secret + "=" * ((8 - len(secret) % 8) % 8)
        base64.b32decode(padded, casefold=True)
    except Exception as exc:  # noqa: BLE001 - any decode failure is the same user problem
        raise SetupError(
            "That TOTP secret is not valid base32. Copy it again from the setup screen "
            "(letters A-Z and digits 2-7)."
        ) from exc
    code = pyotp.TOTP(secret).now()
    if not re.fullmatch(r"\d{6}", code):
        raise SetupError("The TOTP secret did not produce a 6-digit code; copy it again.")
    return secret


def validate_password(value: str) -> str:
    if len(value) < MIN_PASSWORD_LENGTH:
        raise SetupError(f"Password looks too short (at least {MIN_PASSWORD_LENGTH} characters).")
    return value


# --------------------------------------------------------------------------- #
# environment file
# --------------------------------------------------------------------------- #
def read_env_file(path: Path) -> dict[str, str]:
    values: dict[str, str] = {}
    if not path.exists():
        return values
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        values[key.strip()] = value.strip()
    return values


def write_env_file(path: Path, values: dict[str, str]) -> Path:
    lines = [
        "# Written by `eagle-flightplan setup`. NEVER commit this file.",
        f"UNW_USER={values['UNW_USER']}",
        f"UNW_PASS={values['UNW_PASS']}",
        f"UNW_TOTP_SECRET={values['UNW_TOTP_SECRET']}",
        "# optional overrides",
    ]
    for key, default in ENV_OPTIONAL.items():
        lines.append(f"{key}={values.get(key, default)}")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    try:
        path.chmod(0o600)
    except OSError:
        pass  # Windows: mode bits are advisory
    return path


# --------------------------------------------------------------------------- #
# system checks
# --------------------------------------------------------------------------- #
def _run_streaming(command: list[str], cwd: Path) -> tuple[int, str]:
    process = subprocess.Popen(
        command,
        cwd=str(cwd),
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        bufsize=1,
    )
    lines: list[str] = []
    assert process.stdout is not None
    for line in process.stdout:
        print(line.rstrip())
        lines.append(line.rstrip())
    return process.wait(), "\n".join(lines)


def chromium_installed() -> bool:
    home = Path(os.environ.get("PLAYWRIGHT_BROWSERS_PATH", Path.home() / ".cache" / "ms-playwright"))
    candidates = [home]
    if sys.platform == "darwin":
        candidates.append(Path.home() / "Library" / "Caches" / "ms-playwright")
    if os.name == "nt":
        candidates.append(Path(os.environ.get("LOCALAPPDATA", "")) / "ms-playwright")
    return any(path.exists() and any(path.glob("chromium*")) for path in candidates)


def _venv_python(root: Path) -> Path:
    if os.name == "nt":
        return root / ".venv" / "Scripts" / "python.exe"
    return root / ".venv" / "bin" / "python"


def _venv_cli(root: Path) -> Path:
    name = "eagle-flightplan.exe" if os.name == "nt" else "eagle-flightplan"
    return _venv_python(root).parent / name


def _installed_mode(root: Path) -> bool:
    """True when setup runs from a wheel/installed package, not a checkout."""
    return (
        not (root / "pyproject.toml").exists()
        and importlib.util.find_spec("eagle_flightplan") is not None
        and sys.prefix != sys.base_prefix
    )


def _cli_command(root: Path, *, installed: bool) -> str:
    if installed:
        return f"{shlex.quote(sys.executable)} -m eagle_flightplan.cli"
    return str(_venv_cli(root))


def _importable(python: Path | None) -> bool:
    if python is not None and python.exists():
        result = subprocess.run(
            [str(python), "-c", "import eagle_flightplan"],
            capture_output=True,
            text=True,
        )
        return result.returncode == 0
    return importlib.util.find_spec("eagle_flightplan") is not None


def preflight(
    root: Path,
    *,
    chromium_ok: bool | None = None,
    require_session: bool = True,
    installed: bool = False,
) -> list[Check]:
    version = sys.version_info
    python = Path(sys.executable) if installed else _venv_python(root)
    checks = [
        Check(
            name="Python version",
            ok=version >= MIN_PYTHON,
            detail=f"{version.major}.{version.minor}.{version.micro}",
        ),
        Check(name="Virtual environment", ok=sys.prefix != sys.base_prefix, detail=sys.prefix),
        Check(
            name="Project .venv",
            ok=True if installed else python.exists(),
            detail=(
                f"using installed interpreter {sys.executable}"
                if installed
                else str(python)
            ),
        ),
        Check(
            name="Package importable",
            ok=_importable(python),
            detail="eagle_flightplan",
        ),
        Check(
            name="Chromium (Playwright)",
            ok=chromium_installed() if chromium_ok is None else chromium_ok,
            detail="headless browser",
        ),
    ]
    env = read_env_file(root / ".env")
    checks.append(
        Check(
            name="Credentials file",
            ok=all(env.get(key) for key in ENV_KEYS),
            detail=str(root / ".env"),
        )
    )
    session_exists = STATE_PATH.exists()
    if require_session:
        checks.append(
            Check(
                name="Cached session",
                ok=session_exists,
                detail=str(STATE_PATH),
            )
        )
    else:
        checks.append(
            Check(
                name="Cached session",
                ok=True,
                detail=(
                    str(STATE_PATH)
                    if session_exists
                    else "not required with --no-login; run `eagle-flightplan login` before live tools"
                ),
            )
        )
    return checks


# --------------------------------------------------------------------------- #
# wizard
# --------------------------------------------------------------------------- #
def _install(root: Path, services: Services, io: WizardIO, *, installed: bool = False) -> None:
    root.mkdir(parents=True, exist_ok=True)
    if installed:
        python = Path(sys.executable)
        io.print(f"Using the installed interpreter {python}.")
    else:
        python = _venv_python(root)
        if not python.exists():
            io.print(f"Creating the virtual environment in {root / '.venv'} …")
            code, output = services.run([sys.executable, "-m", "venv", str(root / ".venv")], root)
            if code != 0 or not python.exists():
                raise SetupError(
                    "Could not create the virtual environment. Next: run "
                    f"`{sys.executable} -m venv .venv` in the project folder and re-run setup."
                )
        if not _importable(python):
            io.print("Installing Eagle Flightplan (this can take a minute) …")
            code, output = services.run(
                [str(python), "-m", "pip", "install", "-e", str(root)], root
            )
            if code != 0:
                raise SetupError(
                    "Package installation failed. Next: run "
                    f"`{python} -m pip install -e .` in {root} and re-run setup."
                )
        else:
            io.print("Package already installed — skipping.")
    if not services.check_chromium():
        io.print("Downloading the headless browser (about 130 MB, one time) …")
        code, output = services.run(
            [str(python), "-m", "playwright", "install", "chromium"], root
        )
        if code != 0:
            raise SetupError(
                "Browser download failed. Next: run "
                f"`{python} -m playwright install chromium` and re-run setup."
            )
    else:
        io.print("Headless browser already installed — skipping.")


def _collect_credentials(options: SetupOptions, io: WizardIO, env_path: Path) -> dict[str, str]:
    existing = read_env_file(env_path)
    if env_path.exists() and not options.force:
        if options.yes:
            io.print("Keeping the existing .env (use --force to replace it).")
            return existing
        if not io.confirm(f"{env_path} already exists. Replace it?", default=False):
            io.print("Keeping the existing .env.")
            return existing

    user = options.user
    if not user:
        user = io.ask("UNW email", default="you@students.unwsp.edu")
    user = validate_user(user)

    password = None
    if options.password_stdin:
        password = sys.stdin.readline().rstrip("\n")
        if not password:
            raise SetupError("No password arrived on stdin. Next: pipe it with --password-stdin.")
    if password is None:
        if not sys.stdin.isatty():
            raise SetupError(
                "No password available in a non-interactive shell. Next: pipe it with "
                f"`{sys.executable} -m eagle_flightplan.cli setup --yes --password-stdin "
                "--user <email> --totp-secret <base32>` or run setup interactively."
            )
        password = io.getpass("UNW password (typing is hidden): ")
    password = validate_password(password)

    secret = options.totp_secret
    if not secret:
        secret = io.ask("TOTP secret (base32 key from your authenticator app)")
    secret = validate_totp_secret(secret)

    values = {
        "UNW_USER": user,
        "UNW_PASS": password,
        "UNW_TOTP_SECRET": secret,
    }
    write_env_file(env_path, values)
    io.print(f"Saved credentials to {env_path} (permissions restricted).")
    return values


def _default_login(force: bool) -> Path:
    from . import auth

    return auth.login(force=force)


def _default_register_mcp() -> tuple[bool, str]:
    from .register_mcp import register_default_clients

    return register_default_clients(root=REPO_ROOT)


def _doctor_table(checks: Iterable[Check], io: WizardIO) -> bool:
    io.print("Doctor checks:")
    ok_all = True
    for check in checks:
        mark = "✓" if check.ok else "✗"
        io.print(f"  {mark} {check.name}: {check.detail}")
        ok_all = ok_all and check.ok
    return ok_all


def run_setup(
    options: SetupOptions | None = None,
    *,
    io: WizardIO | None = None,
    services: Services | None = None,
) -> int:
    options = options or SetupOptions()
    io = io or WizardIO()
    services = services or Services()
    root = Path(options.root).resolve()
    root.mkdir(parents=True, exist_ok=True)
    installed = _installed_mode(root)
    env_path = root / ".env"
    io.print("Eagle Flightplan setup")
    io.print(f"Project folder: {root}")
    if installed:
        io.print(f"Installed mode: using {sys.executable}")

    version = sys.version_info
    if version < MIN_PYTHON:
        raise SetupError(
            "Python 3.11 or newer is required. Install Python 3.12 from python.org "
            "(tick 'Add Python to PATH') and run setup again."
        )
    if sys.prefix == sys.base_prefix and not installed:
        venv_python = _venv_python(root)
        io.print(
            "You are not inside the project virtual environment.\n"
            f"Next: run `{venv_python} -m eagle_flightplan.cli setup` "
            "or use install.sh / install.ps1."
        )
        if not options.yes:
            raise SetupError("Run setup from the project .venv.")
        io.print("Continuing because --yes was passed; installing into the project .venv.")

    _install(root, services, io, installed=installed)
    _collect_credentials(options, io, env_path)

    checks = preflight(
        root,
        chromium_ok=services.check_chromium(),
        require_session=not options.no_login,
        installed=installed,
    )
    ok_all = _doctor_table(checks, io)

    if not options.no_login:
        should_login = options.yes or io.confirm("Log in now?", default=True)
        if should_login:
            io.print("Starting the browser login (about 20 seconds) …")
            try:
                services.login(True)
                io.print("Login complete.")
            except Exception as exc:  # noqa: BLE001 - explain, do not traceback
                io.print(
                    f"Login failed: {exc}. Next: run `{login_command()}` "
                    "and check your credentials in .env."
                )
                ok_all = False

    if options.register_mcp:
        registered, message = services.register_mcp()
        io.print(("Registered the MCP server. " if registered else "") + message)

    cli = _cli_command(root, installed=installed)
    io.print("What to do next:")
    io.print(f"  1. {cli} advise --term 202620")
    io.print(f"  2. {cli} plans")
    io.print(f"  3. {cli} login   (only if the session expires)")
    return 0 if ok_all else 1
