# unw-planner

[![CI](https://github.com/HenryGetz/unw-planner/actions/workflows/ci.yml/badge.svg)](https://github.com/HenryGetz/unw-planner/actions/workflows/ci.yml)

A command-line and MCP client for planning courses at the University of
Northwestern – St. Paul. It signs in to UNW's Banner 9 Student Self-Service and
Ellucian Degree Works audit with an Entra ID + TOTP login, reads the degree
audit and course catalog, resolves remaining requirements against real sections,
checks time conflicts, and builds term or multi-term plans using Rate My
Professors ratings as a soft preference. The same domain layer is exposed as a
`unwplanner` CLI and a stdio MCP server for agent use.

This is an independent project. It is not affiliated with, endorsed by, or
supported by the University of Northwestern, Ellucian, or Rate My Professors.

## Requirements

- **Python 3.11+** (CI covers 3.11 and 3.12).
- **A UNW account** with access to Banner 9 Student Self-Service and the Degree
  Works dashboard.
- **TOTP two-factor authentication.** The login driver generates the current
  code from a shared secret. Push-notification-only accounts, or tenants that
  require number matching without a "use a verification code" fallback, cannot
  be automated.
- Network access to `registration-ss.oci.unw.edu`,
  `degreeaudit.oci.unw.edu`, `login.microsoftonline.com` and
  `www.ratemyprofessors.com`.

## One-command install

macOS / Linux:

```bash
curl -fsSL https://raw.githubusercontent.com/HenryGetz/unw-planner/main/bootstrap.sh | sh
```

Windows PowerShell:

```powershell
irm https://raw.githubusercontent.com/HenryGetz/unw-planner/main/bootstrap.ps1 | iex
```

The bootstrap clones to `${UNW_PLANNER_HOME:-$HOME/.unw-planner}` (or the
Windows equivalent), then runs the interactive installer. It attaches
`/dev/tty` to the setup prompts when available. In CI or another context with no
terminal, it refuses to start the interactive prompts and prints the checkout
alternative; use the non-interactive command below instead. It installs Python
dependencies and Playwright Chromium, writes `.env`, and optionally logs in.

If you prefer a checkout:

```bash
git clone https://github.com/HenryGetz/unw-planner.git
cd unw-planner
./install.sh          # or: sh install.sh
```

After setup (from the project folder; Windows uses `.venv\\Scripts\\unwplanner.exe`):

```bash
.venv/bin/unwplanner advise --term 202620
```

`advise` prints a shareable advising brief: remaining requirements, candidate
sections, a conflict-free draft and (with `--roadmap`) the multi-term plan. It
is name-free by default; `--no-pii` is accepted for scripts that want to be
explicit about it.

Non-interactive install (never logs in): pipe the password and provide the
other credentials; `--no-login` still writes `.env`:

```bash
printf '%s\n' "$UNW_PASS" | .venv/bin/unwplanner setup --yes --no-login \
  --password-stdin --user "$UNW_USER" --totp-secret "$UNW_TOTP_SECRET"
```

## CLI

The current commands are listed below. Read-only planning commands beyond this
set (profile, remaining requirements, candidates, ratings, conflicts, draft,
roadmap, offering matrix) are available through the MCP server; see
[`docs/mcp.md`](docs/mcp.md). Every command reads the session written by
`login`; set `UNW_AUTO_LOGIN=0` to guarantee no command starts a browser login.

| command | what it does |
| --- | --- |
| `login [--force]` | Run the Entra ID + TOTP browser login and save the session state. |
| `terms` | List Banner term codes and descriptions. |
| `search [--subject S] [--course N] [--term CODE] [--limit N]` | Search one term's sections. |
| `plans [--term CODE]` | List Banner Plan Ahead worksheets for one term (term-scoped). |
| `audit [--unmet] [--json]` | Print the degree-audit groups, or all outstanding groups. |
| `advise [--term CODE] [--out FILE] [--roadmap] [--max-credits N] [--json] [--no-pii] [--no-auto-login]` | Build the one-shot advising brief from live data. |
| `setup [--yes] [--user EMAIL] [--totp-secret SECRET] [--password-stdin] [--no-login] [--register-mcp] [--force] [--root PATH]` | First-run wizard: install dependencies, write `.env` (mode 600), run doctor checks, optionally log in and register MCP. |
| `windows [--term CODE] [--json]` | Show registration time-ticket windows parsed from Banner's eligibility text (read-only). |
| `watch --term CODE [--interval N] [--json] [--until-open]` | Poll one term's eligibility and print changes (read-only). |
| `snipe --term CODE [--plan NAME] [--crn CRN ...] [--mode window\|seats] [--offset N] [--interval N] [--live] [--dry-run/--no-dry-run] [--i-understand] [--allow-waitlist] [--probe] [--max-attempts N] [--timeout N]` | Targeted registration run. Dry-run by default; `--live` additionally requires `--i-understand` and typing the exact term; it never drops/withdraws. Banner is two-phase: it stages each CRN with Add, then clicks Save once; it never reports registered without the Save. **Unverified against a live window.** |
| `plan-create NAME [--term CODE] [--yes]` | Create a plan worksheet. **Writes to Banner.** |
| `plan-add PLAN_ID --crn CRN [--crn CRN ...] [--term CODE] [--yes] [--force]` | Add one or more sections to a plan. **Writes to Banner.** |
| `plan-delete PLAN_ID [--term CODE] [--yes] [--force]` | Delete a plan; without `--force` a TTY asks for the exact name. **Writes to Banner.** |
| `plans-json [--term CODE]` | Legacy raw plan dump; prefer `plans` or the MCP plan tools. |

## MCP server

`unw-mcp` runs a stdio MCP server with 20 tools: session health, profile, audit,
remaining requirements, terms, search, course detail, candidates, RMP ratings,
conflicts, draft (with pins, minimum load and variable-credit hours), roadmap,
offering matrix, term-scoped plan operations, a read-only registration preview
and a read-only registration time-ticket window.

Plan writes are protected by enforced confirmation, not prose. The four write
tools require `confirm: true`; delete additionally requires `confirm_name`
equal to the plan's exact name. A call without them returns
`refused: true`, the intended effect and the current plan state, and performs no
write. Successful writes return a before/after diff read back from Banner.

Register it as a stdio server with an absolute path to `<repo>/.venv/bin/unw-mcp`
(`<repo>\\.venv\\Scripts\\unw-mcp.exe` on Windows). The shape is the same for
omp, Claude Desktop and Cursor:

```json
{
  "mcpServers": {
    "unw-planner": {
      "command": "/ABSOLUTE/PATH/TO/unw-planner/.venv/bin/unw-mcp",
      "args": [],
      "env": {"PYTHONUNBUFFERED": "1"}
    }
  }
}
```

Config locations: `~/.omp/agent/mcp.json` (omp), the Claude Desktop
`claude_desktop_config.json`, and `~/.cursor/mcp.json` (Cursor). `unwplanner
setup --register-mcp` merges this entry into the clients it finds; it preserves
other servers and backs up before writing. Restart the client after registering.

See [`docs/mcp.md`](docs/mcp.md) for the tool table, real request/response
examples, error contract and session-recovery procedure.

## What is verified vs unverified

| area | status |
| --- | --- |
| Entra ID + TOTP login, cookie replay, re-login | Verified live |
| Term list, class search, section detail, term snapshots | Verified live (seven terms) |
| Degree Works audit retrieval/parsing, profile, unmet requirements | Verified live |
| Rate My Professors search and name matching (school 706) | Verified live; coverage varies by professor |
| Conflict checking, draft, roadmap, offering matrix | Offline tests plus live snapshots |
| Banner plan list/read and plan lifecycle | Verified live earlier on a throwaway plan |
| MCP plan-write confirmation/refusal and add diff | Verified with a mocked Banner transport; not re-run against live plans |
| Registration add/drop/register/withdraw | Unverified; deliberately not implemented |

## Safety model

- **Credentials stay local.** `UNW_USER`, `UNW_PASS` and `UNW_TOTP_SECRET` live
  only in `.env` (mode 600, gitignored). No code hardcodes them and no command
  prints them.
- **The session state is a secret.** `.state/unw_state.json` is a replayable
  cookie jar for Banner, Entra ID and Degree Works. It is gitignored and must not
  be copied into tickets, chat or commits. Overwrite it with
  `unwplanner login --force` if exposed.
- **No registration writes by default.** The MCP server never registers; only
  the explicit `unwplanner snipe` command can submit, and only with `--live`
  plus `--i-understand` plus typing the exact term code on stdin. It is
  browser-driven, dry-run by default, never drops/withdraws, and has not been
  validated against a live registration window. `windows`/`watch` and the MCP
  registration preview are read-only.
- **Plan writes are explicit.** CLI plan writes require `--yes` (and an exact
  typed plan name for deletes); MCP writes require `confirm=true` and, for
  delete, the exact `confirm_name`. They never run during read commands and
  never touch anything except Plan Ahead worksheets.
- **Captures are never committed.** `.state/`, `captures/`, `.venv/` and caches
  are gitignored. Capture files may contain course data, screenshots or redacted
  request logs; keep them local.
- **Be polite.** The client reuses one session and caches term snapshots for 15
  minutes. Do not run a second browser against the same session while an HTTP
  call is in flight; Banner invalidates the old session.

## Known limitations

- **Registration is browser-driven and unverified.** The direct add/drop
  endpoints were never observed, so `snipe` drives the UI. It defaults to a dry
  run, requires explicit live authorization, and has not been executed against
  an eligible term. The MCP server has no registration write tool.
- **Filtering is client-side.** Banner ignores `txt_attribute`,
  `txt_instructor`, `chk_open_only` and similar parameters on this deployment, so
  the server fetches a whole-term snapshot (cached for 15 minutes) and filters
  locally. Seat counts are a snapshot, not live guarantees.
- **Plans are term-scoped and bounded.** Plan Ahead only works for selectable
  terms; terms beyond Summer 2027 are visible in the catalog but cannot be
  planned. An empty plan list means "no plans for this term".
- **RMP coverage is thin.** Many faculty have no Rate My Professors record or
  very few ratings. Unrated means unknown, not bad; n<3 is weak evidence.
- **Offering patterns are inferred.** "Fall-only" or "every-term" comes from
  seven observed catalog terms, not a registrar guarantee.
- **Prerequisite parsing is heuristic.** Banner's nested `and`/`or` prerequisite
  text and grade/test clauses are parsed approximately; confirm with an advisor.
- **One student per checkout.** Settings and state are process-wide, not
  multi-user.

## Porting

The tenant, Banner host, Degree Works host/screen code, RMP school id, term-code
convention, attribute vocabulary and login selectors are UNW-specific.
[`docs/porting.md`](docs/porting.md) lists every constant/function to change and
the tests to add. Contribution rules, including the fixture-scrubbing procedure,
are in [`CONTRIBUTING.md`](CONTRIBUTING.md).

## Repository layout

| path | contents |
| --- | --- |
| `src/unw_planner/` | Library: `auth`, `audit/`, `banner/`, `catalog`, `planner`, `schedule`, `rmp/`, `report`, `cli`, `mcp_server`. |
| `docs/` | Login, Banner API, audit, planning, RMP, MCP, porting and an index. |
| `scripts/` | Login, probe, draft, advising artifacts, RMP probe and MCP smoke test. |
| `tests/` | Offline pytest suite, hermetic MCP stdio test and scrubbed fixtures. |
| `.github/workflows/ci.yml` | Python 3.11/3.12 lint, tests and the hermetic MCP smoke. |
| `pyproject.toml` | Package metadata, dependencies, Ruff, coverage, pytest and entry points. |

## Disclaimer

This is unofficial software. Use it only with your own account, at your own
risk. Course data, audit results and instructor ratings can be wrong or stale;
verify everything with your advisor and the official UNW systems before making
enrollment decisions. Respect the terms of service and rate limits of UNW,
Ellucian and Rate My Professors. The project is licensed under the MIT License
(see [`LICENSE`](LICENSE)).
