"""Configuration and on-disk layout for unw-planner.

All secrets live in ``.env`` at the repo root (mode 600, gitignored). Nothing in
this module or its callers hardcodes credentials.
"""

from __future__ import annotations

import os
from functools import lru_cache
from pathlib import Path

from dotenv import load_dotenv
from pydantic import BaseModel, Field

REPO_ROOT = Path(__file__).resolve().parents[2]
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
