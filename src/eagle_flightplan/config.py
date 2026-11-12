"""Configuration and on-disk layout for Eagle Flightplan.

All secrets live in ``.env`` at the repo root (mode 600, gitignored). Nothing in
this module or its callers hardcodes credentials.
"""

from __future__ import annotations

import os
import shlex
import sys
from functools import lru_cache
from pathlib import Path

from dotenv import load_dotenv
from pydantic import BaseModel, Field


def resolve_project_root(
    *,
    file_path: Path | None = None,
    environ: dict[str, str] | None = None,
    cwd: Path | None = None,
) -> Path:
    """Resolve the writable data root for both editable and wheel installs.

    Editable installs keep ``config.py`` under ``<repo>/src`` and are detected by
    the adjacent ``pyproject.toml``. A wheel lives under ``site-packages``; there
    we prefer ``EAGLE_FLIGHTPLAN_HOME`` and fall back to the current directory, never to the
    interpreter's library directory.
    """
    path = Path(file_path) if file_path is not None else Path(__file__).resolve()
    package_root = path.parents[2] if len(path.parents) >= 3 else path.parent
    if (package_root / "pyproject.toml").is_file():
        return package_root
    env = environ if environ is not None else os.environ
    override = env.get("EAGLE_FLIGHTPLAN_HOME", "").strip()
    if override:
        return Path(override).expanduser().resolve()
    current = Path(cwd) if cwd is not None else Path.cwd()
    return current.resolve()


def login_command() -> str:
    """The packaged CLI command that repairs a session (works in wheels too)."""
    return f"{shlex.quote(sys.executable)} -m eagle_flightplan.cli login --force"


REPO_ROOT = resolve_project_root()
ENV_PATH = REPO_ROOT / ".env"
STATE_DIR = REPO_ROOT / ".state"
CAPTURES_DIR = REPO_ROOT / "captures"
FIXTURES_DIR = REPO_ROOT / "tests" / "fixtures"

STATE_PATH = STATE_DIR / "unw_state.json"
TERMS_DIR = STATE_DIR / "terms"
TERM_INDEX_PATH = TERMS_DIR / "index.sqlite"
TERM_CACHE_TTL_SECONDS = 15 * 60
LOGIN_LOG_PATH = CAPTURES_DIR / "login.jsonl"
SSB_LOG_PATH = CAPTURES_DIR / "ssb.jsonl"

DEFAULT_AUDIT_URL = "https://degreeaudit.oci.unw.edu/worksheets/WEB31"
DEFAULT_SSB_BASE = "https://registration-ss.oci.unw.edu/StudentRegistrationSsb/ssb"
DEFAULT_SSB_ORIGIN = "https://registration-ss.oci.unw.edu"

load_dotenv(ENV_PATH)


class Settings(BaseModel):
    """Runtime settings resolved from the environment (and ``.env``)."""

    user: str = Field(default_factory=lambda: os.environ.get("UNW_USER", ""))
    password: str = Field(default_factory=lambda: os.environ.get("UNW_PASS", ""))
    totp_secret: str = Field(default_factory=lambda: os.environ.get("UNW_TOTP_SECRET", ""))
    audit_url: str = Field(
        default_factory=lambda: os.environ.get("UNW_AUDIT_URL", DEFAULT_AUDIT_URL)
    )
    ssb_base: str = Field(default_factory=lambda: os.environ.get("UNW_SSB_BASE", DEFAULT_SSB_BASE))

    @property
    def ssb_origin(self) -> str:
        """Scheme+host of the SSB instance, e.g. ``https://registration-ss.oci.unw.edu``."""
        return "/".join(self.ssb_base.split("/")[:3])

    def require_credentials(self) -> None:
        missing = [
            name
            for name, value in (
                ("UNW_USER", self.user),
                ("UNW_PASS", self.password),
                ("UNW_TOTP_SECRET", self.totp_secret),
            )
            if not value
        ]
        if missing:
            raise RuntimeError(
                f"Missing credentials in {ENV_PATH}: {', '.join(missing)}. "
                "Copy .env.example to .env and fill it in."
            )


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()


def ensure_dirs() -> None:
    for path in (STATE_DIR, TERMS_DIR, CAPTURES_DIR, FIXTURES_DIR):
        path.mkdir(parents=True, exist_ok=True)
