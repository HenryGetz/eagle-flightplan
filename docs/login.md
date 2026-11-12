# UNW login: Entra ID SAML2 + TOTP

Both UNW hosts sit behind the same Microsoft Entra ID tenant
(`713d3c09-b1b4-46a6-93dc-cd6b86269ddd`), so one browser login yields:

* `registration-ss.oci.unw.edu` — Banner 9 Student Self-Service (SSB 9.41.1)
* `degreeaudit.oci.unw.edu` — Ellucian Degree Works dashboard

## The one thing that matters

`https://registration-ss.oci.unw.edu/StudentRegistrationSsb/ssb/registration`
is **public** and returns HTTP 200 to anonymous visitors (a guest shell with
`<meta name="fullName" content="">`). It never triggers a login. The SAML
handshake is started by:

```
GET /StudentRegistrationSsb/ssb/registration/registerPostSignIn?mode=registration
```

That URL 302s to
`https://login.microsoftonline.com/713d3c09-.../saml2?SAMLRequest=...&RelayState=...`.
Any "is the session alive?" check must use this endpoint (or `/ssb/registration`)
and inspect `<meta name="fullName">`, never the status code alone.

The degree audit URL behaves normally: `GET /worksheets/WEB31` 302s to the same
Entra tenant when unauthenticated, and loads the Degree Works dashboard when the
browser already holds an Entra session. (WEB31 is the Ellucian "web31" screen
route, not a per-student worksheet id — see `docs/audit.md`.)

## Flow as driven by `eagle_flightplan.auth.login()`

1. **Entry** — `GET .../registration/registerPostSignIn?mode=registration`.
2. **Username** (`login.microsoftonline.com/.../saml2`) — `input[name='loginfmt']`
   (also `#i0116`, `input[type=email]`), submit `#idSIButton9`.
   The full UPN `student@students.unwsp.edu` is accepted as-is.
3. **Password** — `input[name='passwd']` (`#i0118`), submit `#idSIButton9`.
4. **MFA** — the tenant lands directly on the code page (`.../login`), titled
   "Approve sign in request" with the text "Enter the code displayed in the
   authenticator app". Input `#idTxtBx_SAOTCC_OTC`, submit
   `#idSubmit_SAOTCC_Continue`, value `pyotp.TOTP(secret).now()`.
   The "I can't use my Microsoft Authenticator app right now" → "Use a
   verification code" fallback walk is implemented but is **not** needed for this
   tenant; it only runs when the OTC box is absent (number-matching tenants).
5. **Stay signed in?** — answered **No** (`#idBtn_Back`). Recorded cookie
   evidence shows this does not prevent silent re-auth: the jar keeps
   `ESTSAUTH` / `ESTSAUTHPERSISTENT`, and a later login run with the saved state
   went straight to `term/termSelection?mode=registration` with no password and
   no TOTP prompt (observed 24 min after the interactive login).
6. **Back at the SP** — Lands on
   `/StudentRegistrationSsb/ssb/term/termSelection?mode=registration`.
   `meta fullName` = `Doe, Jane`; `maxInactiveInterval` = `7200`.
7. **Audit SSO** — `GET https://degreeaudit.oci.unw.edu/worksheets/WEB31` loads the
   dashboard with no extra prompt; the audit app sets `REFRESH_TOKEN`, `NAME`,
   `X-AUTH-TOKEN`, `SignInStateCookie`, `CCState`.

`context.storage_state()` is written to `.state/unw_state.json` (mode 600). That
file is the entire replayable session: 24 cookies across Banner, Entra and the
audit host.

## Session lifetime (observed)

| signal | value |
| --- | --- |
| `meta maxInactiveInterval` (guest) | 1800 s |
| `meta maxInactiveInterval` (authenticated) | 7200 s |
| `JSESSIONID` cookie expiry | session cookie (`expires: -1`) |
| observed invalidation | ~26 min after login while actively polling |
| silent re-SAML from saved state | yes (Entra cookies survive) |

Two operational consequences:

1. Banner invalidates the SSB session aggressively. Any call that returns
   `text/html` where JSON was expected means "logged out"; `auth.ensure()` and
   `scripts/probe.py` re-login automatically.
2. **Do not run a browser and the httpx client against the same `JSESSIONID`
   concurrently.** Several captures in this repo showed one side invalidating the
   other. The httpx client is the primary path; use `scripts/capture_web.py`
   only while no httpx work is in flight, then re-login.

## Logging

Every request/response for `*.unw.edu`, `*.unwsp.edu`, `login.microsoftonline.com`,
`msftauth`, `live.com` is appended to `captures/login.jsonl`:

```json
{"type":"request","method":"POST","url":"...","req_headers_subset":{...},"req_body":"..."}
{"type":"response","method":"POST","url":"...","status":200,
 "resp_content_type":"...","resp_body_first_2kb":"...","location":null}
```

Password field values, TOTP codes, `Cookie`/`Authorization` headers, and
`password`/`otc`/`code` keys inside bodies are replaced with `[REDACTED]`.
`captures/` and `.state/` are gitignored.

## Files

| path | role |
| --- | --- |
| `.env` (mode 600, gitignored) | `UNW_USER`, `UNW_PASS`, `UNW_TOTP_SECRET` |
| `.state/unw_state.json` | Playwright `storage_state` (cookies) |
| `captures/login.jsonl` | redacted SAML/MFA transcript |
| `captures/ssb-registration.html` | authenticated SSB shell |
| `captures/audit-landing.html` | Degree Works dashboard shell |

## API

```python
from eagle_flightplan import auth

auth.login(force=True)          # -> Path (state file); silent re-SAML when possible
auth.load_state()               # -> dict (Playwright storage_state)
auth.session_valid()            # -> bool (fullName meta, not status code)
auth.httpx_client()             # -> httpx.Client with Banner cookies + XHR headers
auth.ensure()                   # -> httpx.Client, logging in only when needed
auth.clear()                    # delete the cached state
auth.state_age_seconds()
```

Exceptions: `AuthError`, `MFARequiredError` (MFA page reached but no code box —
carries the selector/URL that failed), `SessionExpiredError` (no state file or
the server answered with a login page).

CLI: `<venv python> -m eagle_flightplan.cli login --force`.

## Automatic re-authentication (self-healing)

Every Banner/audit request that goes through the typed clients is wrapped by
`auth.request_with_refresh()`. When a response is the logged-out shell (empty
`fullName` meta, 401/419, or a redirect to Entra), it:

1. takes a process-wide login lock,
2. re-checks `session_valid()` (a second thread that already healed wins),
3. runs `login(force=True)` **once**, refreshes the client cookies and retries the
   request **once**.

A repeated failure raises `SessionExpiredError` with the manual command
(the packaged `eagle-flightplan login --force` command); it never loops. Non-idempotent plan writes
are never replayed, even after a successful re-login.

Controls: `UNW_AUTO_LOGIN=0` disables healing (`eagle-flightplan advise --no-auto-login`
also sets it), and a login failure surfaces the original error plus the manual
command. Concurrent tool calls cannot start two browsers because the re-login
holds a lock and re-checks validity inside it.

Measured cost (headless Chromium, cold state, 2026-11-11): **~20.5 s** for a full
password + TOTP + KMSI(NO) login, including the audit-host SSO check. Warm
re-login with a still-valid Entra cookie is typically faster (no credential
prompts).
