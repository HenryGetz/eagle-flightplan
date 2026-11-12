"""Authenticated session handling for UNW Banner 9 SSB and the degree audit.

The UNW SSB front door is a Microsoft Entra ID SAML2 service provider. Playwright
drives the browser once; the resulting cookie jar (``storage_state``) is then
replayed from pure httpx for every subsequent call.

Public API
----------
``login(force=False)``    drive the browser and persist state, return state path
``load_state()``          read the persisted storage_state dict
``session_valid()``       cheap authenticated probe against SSB
``httpx_client()``        httpx.Client preloaded with the persisted cookies
``ensure()``              reuse a valid session, otherwise log in first
``clear()``               delete persisted state
"""

from __future__ import annotations

import json
import logging
import os
import re
import threading
import time
from pathlib import Path
from typing import Any, Iterable
from urllib.parse import urlparse

import httpx
import pyotp

from .config import (
    CAPTURES_DIR,
    LOGIN_LOG_PATH,
    STATE_PATH,
    Settings,
    ensure_dirs,
    get_settings,
    login_command,
)

LOGIN_HINTS = ("login.microsoftonline.com", "msftauth", "live.com", "adfs")
UNW_HINTS = (".unw.edu", ".unwsp.edu")
REDACTED = "[REDACTED]"

BROWSER_UA = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/131.0.0.0 Safari/537.36"
)

#: Headers a real Banner SSB XHR always carries. Replayed on every httpx call.
SSB_HEADERS: dict[str, str] = {
    "User-Agent": BROWSER_UA,
    "Accept": "application/json, text/javascript, */*; q=0.01",
    "X-Requested-With": "XMLHttpRequest",
    "Accept-Language": "en-US,en;q=0.9",
    "Origin": "https://registration-ss.oci.unw.edu",
    "Referer": "https://registration-ss.oci.unw.edu/StudentRegistrationSsb/ssb/registration",
}

SENSITIVE_KEYS = ("password", "passwd", "otc", "code", "credential", "cookie", "secret")
MANUAL_LOGIN_COMMAND = login_command()
AUTO_LOGIN_ENV = "UNW_AUTO_LOGIN"

_LOGIN_LOCK = threading.Lock()
_LAST_LOGIN_AT: float | None = None
LOGIN_COOLDOWN_SECONDS = 5.0
logger = logging.getLogger("flightplan.auth")


class AuthError(RuntimeError):
    """Login could not be completed."""


class MFARequiredError(AuthError):
    """A second factor was demanded but could not be satisfied automatically."""


class SessionExpiredError(AuthError):
    """The persisted session no longer authenticates."""


# --------------------------------------------------------------------------- #
# state helpers
# --------------------------------------------------------------------------- #
def load_state() -> dict[str, Any]:
    """Return the persisted Playwright ``storage_state`` dict."""
    if not STATE_PATH.exists():
        raise SessionExpiredError(
            f"No cached session at {STATE_PATH}. Run `{MANUAL_LOGIN_COMMAND}` first."
        )
    try:
        return json.loads(STATE_PATH.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:  # pragma: no cover - corrupt file
        raise SessionExpiredError(f"Corrupt session state at {STATE_PATH}: {exc}") from exc


def clear() -> None:
    """Delete the cached session state."""
    STATE_PATH.unlink(missing_ok=True)


def state_age_seconds() -> float:
    """Seconds since the state file was last written."""
    return time.time() - STATE_PATH.stat().st_mtime


# --------------------------------------------------------------------------- #
# httpx client
# --------------------------------------------------------------------------- #
def _cookie_jar(state: dict[str, Any]) -> httpx.Cookies:
    jar = httpx.Cookies()
    for cookie in state.get("cookies", []):
        name = cookie.get("name")
        value = cookie.get("value")
        domain = cookie.get("domain")
        if not name or value is None:
            continue
        jar.set(
            name,
            value,
            domain=domain,
            path=cookie.get("path", "/"),
        )
    return jar


def httpx_client(
    settings: Settings | None = None,
    *,
    base_url: str | None = None,
    follow_redirects: bool = True,
    state: dict[str, Any] | None = None,
) -> httpx.Client:
    """Build an httpx client carrying the persisted Banner cookies + XHR headers."""
    cfg = settings or get_settings()
    state = state if state is not None else load_state()
    headers = dict(SSB_HEADERS)
    headers["Origin"] = cfg.ssb_origin
    headers["Referer"] = f"{cfg.ssb_base}/registration"
    return httpx.Client(
        base_url=base_url or cfg.ssb_base,
        headers=headers,
        cookies=_cookie_jar(state),
        follow_redirects=follow_redirects,
        timeout=httpx.Timeout(30.0, connect=15.0),
    )


# --------------------------------------------------------------------------- #
# validation probe
# --------------------------------------------------------------------------- #
def _looks_like_login(response: httpx.Response) -> bool:
    if any(hint in str(response.url) for hint in LOGIN_HINTS):
        return True
    if response.status_code in (301, 302, 303, 307, 308):
        return True
    if "<title>Sign in" in response.text[:4000]:
        return True
    if "SAMLRequest" in str(response.url):
        return True
    return False


def session_valid(settings: Settings | None = None) -> bool:
    """True when the cached cookies still authenticate against Banner SSB.

    The public (guest) registration shell returns 200 to anonymous callers, so a
    status code alone proves nothing. Banner renders the signed-in student's name
    into ``<meta name="fullName">``; an empty value means "guest" and a redirect
    to the identity provider means "no session".
    """
    cfg = settings or get_settings()
    if not STATE_PATH.exists():
        return False
    try:
        state = load_state()
    except SessionExpiredError:
        return False
    if not state.get("cookies"):
        return False
    try:
        with httpx_client(cfg, follow_redirects=True, state=state) as client:
            resp = client.get(f"{cfg.ssb_base}/registration")
    except httpx.HTTPError:
        return False
    if resp.status_code != 200 or _looks_like_login(resp):
        return False
    match = re.search(r'<meta name="fullName" content="([^"]*)"', resp.text)
    return bool(match and match.group(1).strip())


def auto_login_enabled(flag: bool | None = None) -> bool:
    """Auto re-auth is on unless ``UNW_AUTO_LOGIN=0`` (or an explicit False)."""
    if flag is not None:
        return flag
    return os.environ.get(AUTO_LOGIN_ENV, "1").strip().lower() not in {"0", "false", "no", "off"}


def looks_expired(response: httpx.Response) -> bool:
    """True when a Banner response is the logged-out page rather than data."""
    if response.status_code in (401, 419):
        return True
    if response.is_redirect:
        location = response.headers.get("location", "")
        if any(hint in location for hint in ("login.microsoftonline.com", "msftauth", "live.com")):
            return True
    content_type = response.headers.get("content-type", "")
    if "html" not in content_type:
        return False
    try:
        text = response.text
    except Exception:  # noqa: BLE001 - streamed/closed responses carry no body
        return False
    if re.search(r'<meta name="fullName" content="\s*"', text):
        return True
    # A followed login redirect lands on an IdP page: full HTML, no Banner
    # fullName meta at all. Small HTML fragments (section details) are exempt.
    return len(text) > 20_000 and 'name="fullName"' not in text


def heal_session(settings: Settings | None = None) -> None:
    """Re-login once, serialized so concurrent callers cannot open two browsers."""
    global _LAST_LOGIN_AT
    cfg = settings or get_settings()
    with _LOGIN_LOCK:
        if session_valid(cfg):
            return
        now = time.monotonic()
        if _LAST_LOGIN_AT is not None and now - _LAST_LOGIN_AT < LOGIN_COOLDOWN_SECONDS:
            raise AuthError(
                "A login was just attempted and the session is still invalid; "
                f"not retrying again. Run `{MANUAL_LOGIN_COMMAND}`."
            )
        logger.info("session expired; starting automatic re-login")
        try:
            login(force=True, settings=cfg)
        finally:
            _LAST_LOGIN_AT = time.monotonic()


def _refresh_cookies(client: httpx.Client) -> None:
    client.cookies = _cookie_jar(load_state())


def request_with_refresh(
    client: httpx.Client,
    method: str,
    url: str,
    *,
    auto_login: bool | None = None,
    retry_on_expiry: bool = True,
    **kwargs: Any,
) -> httpx.Response:
    """Perform one request, healing an expired session at most once.

    ``retry_on_expiry=False`` is for non-idempotent writes: we never replay a
    plan write, even after a successful re-login.
    """
    response = client.request(method, url, **kwargs)
    if not looks_expired(response):
        return response
    if not retry_on_expiry:
        raise SessionExpiredError(
            f"{method} {url} hit an expired Banner session; the write was not retried. "
            f"Run `{MANUAL_LOGIN_COMMAND}` and repeat it."
        )
    if not auto_login_enabled(auto_login):
        raise SessionExpiredError(
            f"Banner session expired. Auto-login is disabled; run `{MANUAL_LOGIN_COMMAND}`."
        )
    try:
        heal_session()
    except Exception as exc:  # noqa: BLE001 - surface the original failure plus the fix
        raise SessionExpiredError(
            f"Banner session expired and automatic re-login failed ({exc}). "
            f"Run `{MANUAL_LOGIN_COMMAND}`."
        ) from exc
    _refresh_cookies(client)
    retried = client.request(method, url, **kwargs)
    if looks_expired(retried):
        raise SessionExpiredError(
            f"Banner session is still expired after one automatic re-login. "
            f"Run `{MANUAL_LOGIN_COMMAND}`."
        )
    return retried


def ensure(settings: Settings | None = None) -> httpx.Client:
    """Return an authenticated httpx client, logging in only when needed."""
    cfg = settings or get_settings()
    if not session_valid(cfg):
        login(settings=cfg)
    return httpx_client(cfg)


# --------------------------------------------------------------------------- #
# browser login
# --------------------------------------------------------------------------- #
def _redact(payload: Any) -> Any:
    """Mask credential-ish form values inside a request body string."""
    if not isinstance(payload, str) or not payload:
        return payload
    for key in SENSITIVE_KEYS:
        payload = re.sub(
            rf'("{key}[^"]*"\s*:\s*")([^"]*)(")',
            rf"\1{REDACTED}\3",
            payload,
            flags=re.IGNORECASE,
        )
    # urlencoded bodies: key=value&...
    payload = re.sub(
        rf"(?i)((?:{ '|'.join(SENSITIVE_KEYS) })=)([^&]*)",
        rf"\1{REDACTED}",
        payload,
    )
    return payload


def _interesting(url: str) -> bool:
    host = urlparse(url).netloc.lower()
    if any(hint in host for hint in LOGIN_HINTS):
        return True
    return any(host.endswith(hint) or host == hint.lstrip(".") for hint in UNW_HINTS)


def _request_headers(headers: dict[str, str]) -> dict[str, str]:
    keep = (
        "accept",
        "accept-language",
        "content-type",
        "origin",
        "referer",
        "x-requested-with",
        "sec-fetch-mode",
        "user-agent",
    )
    out: dict[str, str] = {}
    for key, value in headers.items():
        lowered = key.lower()
        if lowered in ("cookie", "authorization"):
            out[key] = REDACTED
        elif lowered in keep:
            out[key] = value
    return out


class _LoginLog:
    """Append-only JSONL capture of every interesting request/response."""

    def __init__(self, path: Path) -> None:
        ensure_dirs()
        self.path = path
        self.path.write_text("", encoding="utf-8")
        self._fh = self.path.open("a", encoding="utf-8")

    def write(self, record: dict[str, Any]) -> None:
        self._fh.write(json.dumps(record, ensure_ascii=False) + "\n")
        self._fh.flush()

    def close(self) -> None:
        self._fh.close()


def _click_first(page: Any, selectors: Iterable[str], *, timeout: int = 4000) -> str | None:
    """Click the first selector that resolves; return the selector used."""
    for selector in selectors:
        locator = page.locator(selector).first
        try:
            if locator.count() and locator.is_visible():
                locator.click(timeout=timeout)
                return selector
        except Exception:  # noqa: BLE001 - selector probing is inherently fuzzy
            continue
    return None


def _fill_first(page: Any, selectors: Iterable[str], value: str, *, timeout: int = 8000) -> str | None:
    for selector in selectors:
        locator = page.locator(selector).first
        try:
            if locator.count() and locator.is_visible():
                locator.fill(value, timeout=timeout)
                return selector
        except Exception:  # noqa: BLE001
            continue
    return None


def _on_idp(page: Any) -> bool:
    url = page.url
    return any(host in url for host in ("login.microsoftonline.com", "login.live.com", "msftauth"))


def _screenshot(page: Any, name: str) -> Path:
    ensure_dirs()
    path = CAPTURES_DIR / f"login-{name}.png"
    try:
        page.screenshot(path=str(path), full_page=True)
    except Exception:  # noqa: BLE001 - diagnostics must never mask the real error
        return path
    return path


def _wait_for_any(page: Any, selectors: Iterable[str], *, timeout: int = 15000) -> str | None:
    deadline = time.time() + timeout / 1000
    while time.time() < deadline:
        for selector in selectors:
            try:
                if page.locator(selector).first.count() and page.locator(selector).first.is_visible():
                    return selector
            except Exception:  # noqa: BLE001
                continue
        page.wait_for_timeout(250)
    return None


def _handle_mfa(page: Any, secret: str, log: _LoginLog) -> None:
    """Satisfy the second factor with a TOTP code.

    UNW's tenant shows the "Enter the code displayed in the authenticator app"
    form directly (``#idTxtBx_SAOTCC_OTC``). Some tenants instead show the
    number-matching "Approve sign-in request" dead end first; in that case walk
    the fallback link chain ("I can't use my Microsoft Authenticator app right
    now" -> "Use a verification code") until the code box appears.
    """
    code_selectors = (
        "#idTxtBx_SAOTCC_OTC",
        "input[name='otc']",
        "input[autocomplete='one-time-code']",
    )
    code_selector = _wait_for_any(page, code_selectors, timeout=3000)

    if not code_selector:
        fallback = (
            "text=I can't use my Microsoft Authenticator app right now",
            "text=I can\u2019t use my Microsoft Authenticator app right now",
            "#idAAMgmtLink",
            "text=Use a different verification option",
            "#idSignInAnotherWay",
            "#moreOptions",
            "text=Having trouble?",
        )
        used = _click_first(page, fallback, timeout=6000)
        if used:
            log.write({"step": "mfa-fallback-link", "selector": used})
            page.wait_for_timeout(1500)

        used = _click_first(
            page,
            (
                "text=Use a verification code",
                "text=Use a verification code instead",
                "#idDiv_SAOTCC_Title",
                "#idBtn_SAOTCC",
                "input[value='Use a verification code']",
                "input[value='Verification code']",
            ),
            timeout=6000,
        )
        if used:
            log.write({"step": "mfa-choose-totp", "selector": used})
            page.wait_for_timeout(1000)

        code_selector = _wait_for_any(
            page,
            code_selectors + ("input[type='tel']", "input[aria-label*='code']"),
            timeout=15000,
        )

    if not code_selector:
        _screenshot(page, "mfa-no-code-box")
        raise MFARequiredError(
            "Reached the MFA step but no verification-code input appeared. "
            f"Page: {page.url}. Screenshot: {CAPTURES_DIR / 'login-mfa-no-code-box.png'}"
        )

    page.locator(code_selector).first.fill(pyotp.TOTP(secret).now())
    log.write({"step": "mfa-code-entered", "selector": code_selector, "code": REDACTED})

    used = _click_first(
        page,
        (
            "#idSubmit_SAOTCC_Continue",
            "input[type='submit']",
            "button:has-text('Verify')",
            "#idSIButton9",
        ),
        timeout=6000,
    )
    if not used:
        raise MFARequiredError(f"MFA code entered but no submit control found. Page: {page.url}")
    log.write({"step": "mfa-code-submitted", "selector": used})
    page.wait_for_timeout(2500)


def login(
    force: bool = False,
    settings: Settings | None = None,
    *,
    headless: bool = True,
    timeout_ms: int = 60_000,
    keep_signed_in: bool = False,
) -> Path:
    """Drive the Entra ID SAML login and persist the resulting browser state."""
    from playwright.sync_api import TimeoutError as PlaywrightTimeoutError
    from playwright.sync_api import sync_playwright

    cfg = settings or get_settings()
    cfg.require_credentials()
    ensure_dirs()

    if not force and STATE_PATH.exists() and session_valid(cfg):
        return STATE_PATH

    log = _LoginLog(LOGIN_LOG_PATH)
    try:
        with sync_playwright() as pw:
            browser = pw.chromium.launch(headless=headless, args=["--disable-blink-features=AutomationControlled"])
            # Seed with the cached jar when present: the Entra session cookie
            # (ESTSAUTH/ESTSAUTHPERSISTENT) often yields a silent SAML re-auth,
            # so a refresh needs no password or TOTP. The credential steps below
            # stay conditional, so a cold start still works.
            seed = str(STATE_PATH) if STATE_PATH.exists() else None
            context = browser.new_context(
                storage_state=seed,
                user_agent=BROWSER_UA,
                viewport={"width": 1440, "height": 900},
                locale="en-US",
                timezone_id="America/Chicago",
            )
            page = context.new_page()

            def on_request(request: Any) -> None:
                if not _interesting(request.url):
                    return
                log.write(
                    {
                        "type": "request",
                        "method": request.method,
                        "url": request.url,
                        "req_headers_subset": _request_headers(request.headers),
                        "req_body": _redact(request.post_data),
                    }
                )

            def on_response(response: Any) -> None:
                if not _interesting(response.url):
                    return
                body = None
                try:
                    body = response.text()[:2048]
                except Exception:  # noqa: BLE001 - some responses carry no body
                    body = None
                log.write(
                    {
                        "type": "response",
                        "method": response.request.method,
                        "url": response.url,
                        "status": response.status,
                        "resp_content_type": response.headers.get("content-type"),
                        "resp_body_first_2kb": _redact(body),
                        "location": response.headers.get("location"),
                    }
                )

            page.on("request", on_request)
            page.on("response", on_response)

            try:
                # Banner's public registration shell stays 200 for anonymous
                # visitors; the SAML handshake is triggered by this endpoint.
                entry = f"{cfg.ssb_base}/registration/registerPostSignIn?mode=registration"
                page.goto(entry, wait_until="domcontentloaded", timeout=timeout_ms)
                log.write({"step": "goto-registration", "url": page.url})

                # --- Entra ID: username -------------------------------------
                user_selector = _wait_for_any(
                    page,
                    ("input[name='loginfmt']", "#i0116", "input[type='email']"),
                    timeout=20000,
                )
                if user_selector and _on_idp(page):
                    page.locator(user_selector).first.fill(cfg.user)
                    log.write({"step": "username-filled", "selector": user_selector, "value": cfg.user})
                    _click_first(page, ("#idSIButton9", "input[type='submit']", "button:has-text('Next')"))
                    page.wait_for_timeout(2500)
                log.write({"step": "after-username", "url": page.url})

                # --- Entra ID: password -------------------------------------
                pw_selector = _wait_for_any(
                    page,
                    ("input[name='passwd']", "#i0118", "input[type='password']"),
                    timeout=20000,
                )
                if pw_selector and _on_idp(page):
                    page.locator(pw_selector).first.fill(cfg.password)
                    log.write({"step": "password-filled", "selector": pw_selector, "value": REDACTED})
                    _click_first(page, ("#idSIButton9", "input[type='submit']", "button:has-text('Sign in')"))
                    page.wait_for_timeout(3000)
                log.write({"step": "after-password", "url": page.url})

                # --- MFA ----------------------------------------------------
                # UNW's tenant lands straight on "Approve sign in request ->
                # Enter the code displayed in the authenticator app", i.e. the
                # OTC box is already present. The fallback-link walk below is
                # only needed if the tenant switches to number matching.
                mfa_markers = (
                    "#idTxtBx_SAOTCC_OTC",
                    "input[name='otc']",
                    "text=Approve sign in request",
                    "text=Microsoft Authenticator",
                    "#idDiv_SAOTCC_Title",
                )
                if _on_idp(page) and _wait_for_any(page, mfa_markers, timeout=12000):
                    log.write({"step": "mfa-detected", "url": page.url})
                    _handle_mfa(page, cfg.totp_secret, log)
                log.write({"step": "after-mfa", "url": page.url})

                # --- KMSI ---------------------------------------------------
                if _wait_for_any(page, ("#idBtn_Back", "text=Stay signed in?"), timeout=8000):
                    choice = "#idSIButton9" if keep_signed_in else "#idBtn_Back"
                    _click_first(page, (choice,))
                    log.write({"step": "kmsi", "keep_signed_in": keep_signed_in, "selector": choice})
                    page.wait_for_timeout(2500)

                # --- back to the SP ----------------------------------------
                for _ in range(12):
                    if "microsoftonline" not in page.url and "live.com" not in page.url:
                        break
                    page.wait_for_timeout(1500)
                page.wait_for_load_state("domcontentloaded", timeout=timeout_ms)
                log.write({"step": "back-at-sp", "url": page.url})

                if any(hint in page.url for hint in ("login.microsoftonline.com", "msftauth")):
                    _screenshot(page, "stuck")
                    raise AuthError(
                        "Still on the identity provider after login. "
                        f"URL: {page.url}. Screenshot: {CAPTURES_DIR / 'login-stuck.png'}"
                    )

                # Confirm the SP session actually took (fullName meta populated).
                page.goto(entry, wait_until="domcontentloaded", timeout=timeout_ms)
                log.write({"step": "ssb-authenticated", "url": page.url})
                (CAPTURES_DIR / "ssb-registration.html").write_text(page.content(), encoding="utf-8")

                # The audit app is a separate SP behind the same Entra tenant.
                page.goto(cfg.audit_url, wait_until="domcontentloaded", timeout=timeout_ms)
                for _ in range(8):
                    if "microsoftonline" not in page.url:
                        break
                    page.wait_for_timeout(1500)
                log.write({"step": "audit-loaded", "url": page.url})
                (CAPTURES_DIR / "audit-landing.html").write_text(page.content(), encoding="utf-8")

                state = context.storage_state()
            except PlaywrightTimeoutError as exc:
                _screenshot(page, "timeout")
                raise AuthError(
                    f"Login timed out at {page.url}: {exc}. "
                    f"See {CAPTURES_DIR / 'login-timeout.png'} and {LOGIN_LOG_PATH}."
                ) from exc
            finally:
                browser.close()
    finally:
        log.close()

    STATE_PATH.write_text(json.dumps(state, indent=2), encoding="utf-8")
    STATE_PATH.chmod(0o600)
    if not session_valid(cfg):
        raise AuthError(
            "Browser login finished but the replayable session does not validate. "
            f"Cookies: {[c['name'] for c in state.get('cookies', [])]}. See {LOGIN_LOG_PATH}."
        )
    return STATE_PATH
