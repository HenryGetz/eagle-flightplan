# Contributing

Thanks for improving `Eagle Flightplan`. This project automates a real student
account, so safety and reproducibility matter more than feature volume.

## Setup

```bash
git clone <repository-url> eagle-flightplan
cd eagle-flightplan

python3 -m venv .venv
.venv/bin/pip install -e ".[dev,cov]"
.venv/bin/playwright install chromium

cp .env.example .env
chmod 600 .env
$EDITOR .env   # UNW_USER, UNW_PASS, UNW_TOTP_SECRET
```

Use `uv` if you prefer: `uv sync --extra dev --extra cov`.

Run the checks before opening a pull request:

```bash
.venv/bin/ruff check .
.venv/bin/python -m pytest -q
.venv/bin/python -m pytest --cov=eagle_flightplan --cov-report=term-missing
```

The default test suite is hermetic: no credentials, no browser, no network.
Live RMP tests are marked `network` and skipped unless `RMP_RUN_NETWORK=1`.
Set `UNW_AUTO_LOGIN=0` when you want to guarantee that no command attempts a
browser login.

## Never commit secrets or personal data

`.env`, `.state/`, `captures/`, `.venv/`, caches and screenshots are gitignored.
That is a safety rail, not a license to leave secrets around:

- Never add `.env`, a cookie jar, a captured request log, a screenshot, or a
  fixture taken from an authenticated page.
- `.state/unw_state.json` is a replayable session. Treat it like a password.
- `captures/` can contain course records, names, URLs and screenshots. Keep it
  local.
- Do not paste passwords, TOTP secrets, cookies or student identifiers into
  issues, pull requests, commit messages or changelog entries.

If a credential or session is exposed, rotate it before debugging: change the
UNW password, re-enroll the TOTP secret if needed, and delete the state file.

## Scrubbing a fixture

Fixtures captured from live services are allowed only after scrubbing. Replace
every real value with a clearly fake one:

| field | placeholder |
| --- | --- |
| student name | `REDACTED STUDENT` |
| student id | `00000000` |
| student email | `redacted@example.edu` |
| login UPN | `student@students.unwsp.edu` |
| local username path | `<repo>` or `~` |

Then re-scan the whole tree before committing:

```bash
# Use the real values locally; never put them in a command that gets committed.
grep -rn "<student-name>" --exclude-dir=.git --exclude-dir=.venv \
  --exclude-dir=.state --exclude-dir=captures .
grep -rn "<student-id>" --exclude-dir=.git --exclude-dir=.venv \
  --exclude-dir=.state --exclude-dir=captures .
git grep --cached -n -E "<student-name>|<student-id>|<student-email>"
```

Also check that no fixture contains `/home/<username>`, a live cookie value, or
an `Authorization` header value. The test at `tests/test_mcp_ci.py` only needs
scrubbed fixtures and must stay network-free.

## Code layout and ownership

- `src/eagle_flightplan/mcp_server.py` owns the MCP surface and the guarded plan
  writes. Plan-write tools must refuse without explicit confirmation and must
  return a before/after diff read back from Banner.
- `src/eagle_flightplan/cli.py` owns command-line UX and its own confirmation flow.
- `src/eagle_flightplan/report.py` builds the advising brief.
- `src/eagle_flightplan/auth.py`, `banner/`, `audit/`, `catalog.py`, `planner.py`,
  `schedule.py` and `rmp/` are the domain layer. Keep interface changes small,
  keep them covered by tests, and update `docs/` when behaviour changes.
- `tests/` is hermetic. Mock HTTP with `httpx.MockTransport`; never call a live
  service from a default test.

## Adding a school or tenant adapter

Read `docs/porting.md` first. In short: change the SSB base, Degree Works host
and screen, RMP school id, term-code convention, audit node/attribute mappings
and login selectors. Add scrubbed fixtures and offline tests for each changed
parser. Keep UNW defaults working: adapters should be configuration or small
subclasses where possible, not copy-pasted forks of the whole package.

## Pull request checklist

- [ ] `ruff check .` passes.
- [ ] `pytest -q` passes without credentials or network.
- [ ] New behaviour has an offline test; a bug fix has a regression test.
- [ ] No secret, cookie, screenshot, capture, real name, student id or email.
- [ ] Docs updated (`README.md`, `docs/`, `CHANGELOG.md` where relevant).
- [ ] Destructive Banner operations stay explicit and confirmed.

By contributing you agree that your contribution is licensed under the MIT
License in `LICENSE`.
