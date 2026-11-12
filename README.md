# Eagle Flightplan — course planning for UNW Eagles

A course-planning toolkit for the University of Northwestern – St. Paul. It signs in to UNW's Banner 9
Student Self-Service and Ellucian Degree Works audit, reads the degree audit and catalog, resolves
remaining requirements against real sections, checks time conflicts, and builds term or multi-term plans
using Rate My Professors ratings as a soft preference. The same domain layer is exposed as an
`eagle-flightplan` command-line interface and a stdio MCP server for agents.

This is an independent, unofficial project. It is not affiliated with, endorsed by, or supported by the
University of Northwestern, Ellucian, or Rate My Professors. Course data and ratings can be wrong or
stale; verify decisions with an advisor and the official UNW systems.

## What it does

- Parses the Degree Works audit into requirement groups, blocks, course options, attributes, wildcards,
  and credit pools.
- Shows only the requirements that are still unmet, including pick-N progress, remaining credits, and
  `shared_title_with` warnings for possible double-counting.
- Searches a whole Banner term and filters sections by subject, number, title, instructor, attribute, or
  availability.
- Checks pairwise time conflicts with a half-open interval model, an ASCII week grid, and a
  `grid_legend` that maps grid labels and overlap slugs to CRNs.
- Looks up Rate My Professors ratings, keeping unrated instructors as `null` (unknown), never `0.0`.
- Drafts a conflict-free term with pins, exclusions, minimum load, variable-credit hours, instructor
  avoidance, and a roadmap across terms.
- Reads Banner Plan Ahead worksheets and can create, add to, remove from, or delete them with enforced
  confirmation.
- Reads registration time-ticket windows and runs a dry-run registration preview. It does not register
  from the MCP server.

## Install

### Requirements

- Python 3.11 or newer (3.11 and 3.12 are supported).
- A UNW account with Banner 9 Self-Service and Degree Works access. Use your full address, for example
  `you@students.unwsp.edu`.
- TOTP two-factor authentication with a shared secret. Push-only or number-matching-only accounts cannot
  be automated by the login driver.
- Network access to `registration-ss.oci.unw.edu`, `degreeaudit.oci.unw.edu`,
  `login.microsoftonline.com`, and `www.ratemyprofessors.com`.

### One-command install

macOS / Linux:

```bash
curl -fsSL https://raw.githubusercontent.com/HenryGetz/eagle-flightplan/main/bootstrap.sh | sh
```

Windows PowerShell:

```powershell
irm https://raw.githubusercontent.com/HenryGetz/eagle-flightplan/main/bootstrap.ps1 | iex
```

The bootstrap clones the project to `${EAGLE_FLIGHTPLAN_HOME:-$HOME/.eagle-flightplan}` (or the Windows
equivalent), then runs the installer. It attaches `/dev/tty` to the setup prompts when a terminal is
available. In a context with no terminal it stops with instructions instead of reading EOF forever.

The installer writes `.env` with mode 600, installs the Python package, and downloads Playwright
Chromium on first use (about 130 MB). Windows support is provided, but the Windows installer was not
executed in this environment.

### Manual checkout

```bash
git clone https://github.com/HenryGetz/eagle-flightplan.git
cd eagle-flightplan
./install.sh          # or: sh install.sh
```

On Windows:

```powershell
powershell -ExecutionPolicy Bypass -File install.ps1
```

### Non-interactive setup

`--no-login` still writes `.env`, so credentials are still required. Pipe the password rather than
relying on `getpass`:

```bash
printf '%s\n' "$UNW_PASS" | .venv/bin/eagle-flightplan setup --yes --no-login \
  --password-stdin --user "$UNW_USER" --totp-secret "$UNW_TOTP_SECRET"
```

The first Chromium download happens during setup. If setup is skipped, install it with:

```bash
.venv/bin/python -m playwright install chromium
```

## Configure the MCP server

The server is stdio-only and exposes 20 tools. Use the absolute path to the console script:

```json
{
  "mcpServers": {
    "eagle-flightplan": {
      "command": "/ABSOLUTE/PATH/TO/eagle-flightplan/.venv/bin/eagle-flightplan-mcp",
      "args": [],
      "env": {"PYTHONUNBUFFERED": "1"}
    }
  }
}
```

On Windows the command is `C:\ABSOLUTE\PATH\TO\eagle-flightplan\.venv\Scripts\eagle-flightplan-mcp.exe`.

The same block works for omp (`~/.omp/agent/mcp.json`), Claude Desktop (`claude_desktop_config.json`),
and Cursor (`~/.cursor/mcp.json`). `eagle-flightplan setup --register-mcp` merges the entry into the clients
it finds, preserves other servers, backs up before writing, and is idempotent.

Restart the MCP client after installing or upgrading. A stale client process keeps the old code and
cache format in memory.

### Key tools

- `unw_session` — live authentication check, state-file age, cookie counts (never values), and repair
  hints.
- `unw_profile`, `unw_audit`, `unw_remaining` — identity, full requirement tree, and unmet requirements.
- `unw_search`, `unw_course` — term sections and one-CRN detail.
- `unw_candidates` — requirement-to-section candidates. `open_only=true`, `compact=true`, and `limit=N`
  keep broad queries manageable.
- `unw_ratings` — batch RMP lookup with 7-day cache and no negative caching.
- `unw_conflicts` — pairwise conflicts, ASCII grid, and `grid_legend`.
- `unw_plan_draft` — conflict-free draft with `max_credits`, `min_credits`,
  `required_crns`/`required_courses`, `exclude_crns`/`exclude_courses`, `avoid_instructors`,
  `exclude_arrangement`, and `hours` (CRN-to-credit-hours). Pins hard-fail with `ok: false` and
  `pin_errors`; the partial draft is still returned.
- `unw_roadmap` — multi-term plan. Pins are draft-only and are not accepted here.
- `unw_offering_matrix` — observed fall/spring/summer offering patterns.
- `unw_registration_window` — read-only time-ticket window(s), parsed from Banner's eligibility prose.
- `unw_register_preview` — DRY RUN ONLY: eligibility, seats, conflicts, and prerequisite checks; never
  POSTs add/drop/register.
- `unw_banner_plans`, `unw_banner_plan_create`, `unw_banner_plan_add`, `unw_banner_plan_remove`,
  `unw_banner_plan_delete` — term-scoped Plan Ahead worksheets. The four write tools require
  `confirm=true`; delete also requires the exact plan name.

Every tool returns JSON beginning with `"ok": true` or `"ok": false` plus an `error`/`hint`. Plan-write
success responses include a before/after diff read back from Banner.

## Agent workflow

Start with session health, not with the audit:

```text
unw_session()
```

- `authenticated: true` — proceed.
- `authenticated: false` and `degraded: true` — cached state exists but the live check failed. Try one
  read tool (`unw_profile` or `unw_terms`) before asking for a login; read-only tools may still work.
- `authenticated: false` and `degraded: false` — no state; use the returned `fix_command` before Banner
  work.
- Stop on `AuthError`/`SessionExpiredError`, or before any Banner plan write.

Then gather context and plan:

```text
unw_profile()
unw_remaining()
unw_plan_draft(term="202620", max_credits=16, min_credits=12,
               required_crns=["CRN1"], hours={"CRN2": 2})
unw_conflicts(term="202620", crns=["CRN1", "CRN2"])
unw_ratings(names=["Last, First"])
unw_banner_plans(term="202620")
```

Report the ASCII grid, a one-line rationale per pick, `unverified` prerequisites, closed/not-yet-open
sections, and any discrepancy between the audit and the plan. Never claim two sections are conflict-free
without running `unw_conflicts` on the final CRN set. Never claim a student has registered: the MCP
server has no registration write.

## CLI

Run commands from the project folder as `.venv/bin/eagle-flightplan ...` (Windows:
`.venv\Scripts\eagle-flightplan.exe`).

| Command | What it does |
| --- | --- |
| `setup` | First-run wizard: dependencies, Chromium, `.env` (mode 600), doctor checks, optional login and MCP registration. |
| `login [--force]` | Entra ID + TOTP browser login; writes the replayable session state. |
| `advise --term CODE` | One-shot advising brief: remaining requirements, candidates, draft, optional roadmap. |
| `audit [--unmet] [--json]` | Print all audit groups or only outstanding ones. |
| `terms`, `search`, `plans` | Read-only term, section, and Plan Ahead listings. |
| `windows --term CODE [--json]` | Registration time-ticket windows parsed from Banner's eligibility text. |
| `watch --term CODE` | Poll eligibility and print changes; `--until-open` exits when it flips. |
| `snipe --term CODE ...` | Targeted registration run (see below). CLI only. |
| `plan-create NAME` | Create a Plan Ahead worksheet. **Writes to Banner.** |
| `plan-add PLAN_ID --crn CRN` | Add a section or course to a worksheet. **Writes to Banner.** |
| `plan-delete PLAN_ID` | Delete a worksheet; an exact typed name is required on a TTY. **Writes to Banner.** |

### `snipe` and registration

`snipe` is CLI-only, dry-run by default, and never drops or withdraws. Live mode requires both
`--i-understand` and the exact term code typed on stdin. Banner's registration UI is two-phase: Add/CRN
**stages** a pending row, and Save commits the batch. The runner therefore stages each target, then
clicks Save exactly once; it never reports `registered` from a staging message. Missing Save controls
return `staged_not_committed`, unclear Save results return `save_unknown`, authorization or OLR popups
return `authorization_required`, and unclear staging returns `stage_unknown` without clicking Save.

Live registration remains unverified. Spring 2027 currently returns `"Term not eligible for
registration"`; every non-probe snipe run checks eligibility first and stops with `not_eligible` before
opening the registration UI. Use `eagle-flightplan windows --term 202620` or `unw_registration_window` to see
the current text.

## What is verified vs not

| Area | Status |
| --- | --- |
| Entra ID + TOTP login, cookie replay, re-login | Verified live |
| Term list, class search, section detail, term snapshots | Verified live |
| Degree Works audit parsing, profile, unmet requirements | Verified live |
| Rate My Professors search and name matching | Verified live; coverage varies |
| Conflict checks, draft, roadmap, offering matrix | Offline tests plus live snapshots |
| Plan Ahead list/read and plan lifecycle | Verified live earlier on a throwaway plan |
| MCP plan-write guards and read-back diff | Tested with a mocked Banner transport |
| Registration add/drop/register/withdraw | **Not live-verified; not implemented in MCP** |
| Waitlist/drop request shapes | **Not verified** |
| Holds | **Unknown/null; no mapped endpoint** |
| Corequisite and linked-lab enforcement | **Not enforced by the planner** |
| Roadmap pins | **Draft-only; `unw_roadmap` does not accept pins** |
| Plan credit-hours writes | **Not supported** |

## Data and safety

- `.env` contains `UNW_USER`, `UNW_PASS`, and `UNW_TOTP_SECRET`. It is mode 600 and gitignored. Never
  commit, paste, or log it.
- `.state/unw_state.json` is a replayable cookie jar for Banner, Entra ID, and Degree Works. Treat it as
  a password; it is gitignored.
- `captures/` can contain course data, screenshots, or request transcripts. It is gitignored; review
  before sharing.
- `EAGLE_FLIGHTPLAN_HOME` controls where the bootstrap clones the project and the writable
  data root for wheel/installed runs, including `.env`, `.state/`, and caches.
- This is one student per checkout. Settings and state are process-wide, not multi-user.
- Be polite: reuse the session, respect rate limits, and keep the 15-minute term snapshot cache and
  7-day RMP cache. Do not run a second browser against the same session while an HTTP call is in flight.
- The MCP server never registers. The only Banner writes are Plan Ahead worksheet operations, guarded by
  `confirm=true` and (for deletion) an exact `confirm_name`.

## Troubleshooting

**Session expired**

```bash
.venv/bin/eagle-flightplan login --force
```

The packaged command also works as `.venv/bin/python -m eagle_flightplan.cli login --force`.

**Piped installer has no terminal**

The bootstrap cannot read credential prompts from a script pipe. Run the installer from a terminal, or
use the non-interactive command with `--yes --no-login --password-stdin` and a password source. On
Windows, run `install.ps1` from PowerShell rather than through a non-interactive host.

**Stale MCP client after upgrade**

Restart the client/server. Old processes can keep old code and the old ratings cache in memory.

**Wheel/installed run cannot find `.env` or state**

Set `EAGLE_FLIGHTPLAN_HOME=/path/to/data` before running. The installed package uses that as its writable root
instead of the interpreter's `site-packages` directory.

**`unw_session` says `degraded: true`**

The state file exists but the live check failed. Try `unw_profile` or `unw_terms` once; if they also
fail with an auth/session error, run the repair command. Do not attempt a plan write until the session
is repaired.

**A course prerequisite is `unverified`**

Banner's prerequisite text could not be fetched or parsed. The section may still be placed, with
`prereq_unverified: true`, but confirm it with an advisor or the department before registering.

## Development

```bash
python -m venv .venv
.venv/bin/pip install -e ".[dev]"
.venv/bin/pytest -q
.venv/bin/ruff check .
```

Tests are offline by default; the network-marked RMP tests opt in with `RMP_RUN_NETWORK=1`.

## Repository and license

- Repository: <https://github.com/HenryGetz/eagle-flightplan>
- License: MIT (see `LICENSE`)

Use this software only with your own UNW account, at reasonable request rates, and in accordance with
UNW, Ellucian, and Rate My Professors terms of service. The project is unofficial, comes with no
warranty, and should not be used as the sole source of enrollment decisions.
