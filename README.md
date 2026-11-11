# unw-planner

A command-line and MCP client for planning courses at the University of
Northwestern – St. Paul. It signs in to UNW's Banner 9 Student Self-Service and
Ellucian Degree Works audit with an Entra ID + TOTP login, reads your degree
audit and course catalog, resolves remaining requirements against real sections,
checks time conflicts, and drafts a term or multi-term plan using Rate My
Professors ratings as a soft preference. The same engine is exposed as a
`unwplanner` CLI and a stdio MCP server for agent use.

This is an independent project. It is not affiliated with, endorsed by, or
supported by the University of Northwestern, Ellucian, or Rate My Professors.

## Requirements

- **Python 3.11+** (developed on CPython 3.12).
- **A UNW account** with access to Banner 9 Student Self-Service and the Degree
  Works dashboard.
- **TOTP two-factor authentication.** The login driver generates the current
  code from a shared secret. Push-notification-only accounts, or tenants that
  require number matching without a "use a verification code" fallback, cannot
  be automated.
- Network access to `registration-ss.oci.unw.edu`,
  `degreeaudit.oci.unw.edu`, `login.microsoftonline.com` and
  `www.ratemyprofessors.com`.

## Quickstart

```bash
git clone <repository-url> unw-planner
cd unw-planner

python3 -m venv .venv
.venv/bin/pip install -e ".[dev]"
.venv/bin/playwright install chromium

cp .env.example .env
chmod 600 .env
$EDITOR .env        # set UNW_USER, UNW_PASS, UNW_TOTP_SECRET

.venv/bin/unwplanner doctor
.venv/bin/unwplanner login
.venv/bin/unwplanner profile
.venv/bin/unwplanner remaining
```

`doctor` checks the credential file, session age, live session validity,
Chromium, the term-snapshot cache and RMP reachability, and prints a one-line
fix for any failure. `login` opens Chromium headlessly, completes the Entra ID
password + TOTP flow, and saves a replayable cookie state to
`.state/unw_state.json`. Later commands reuse those cookies over plain HTTP; no
browser is needed unless the session expires.

If you use `uv`, `uv sync` and `uv run unwplanner …` are equivalent after the
same `.env` setup.

## CLI

Every read command accepts `--json` for machine-readable output. `--refresh`
forces a live Banner refetch instead of the cached term snapshot.

| command | what it does |
| --- | --- |
| `login [--force]` | Run the Entra ID + TOTP browser login and save the session state. |
| `doctor [--json]` | Check `.env`, session, Chromium, term cache and RMP, with fixes. |
| `terms [--json]` | List term codes, descriptions and Banner registration eligibility. |
| `profile [--json]` | Print Degree Works identity, programs, credits, GPA and graduation term. |
| `remaining [--json]` | List unmet requirements with credits, options and priority. |
| `audit [--unmet] [--group TEXT] [--json]` | Print or filter the requirement tree. |
| `search [--term CODE] [--subject S] [--course N] [--keyword K] [--attribute A] [--instructor I] [--open-only] [--limit N] [--refresh] [--json]` | Search one term's sections. |
| `candidates [--term CODE] [--requirement TEXT] [--open-only/--include-closed] [--refresh] [--json]` | Resolve unmet requirements to offered sections and explain empty results. |
| `rate NAME... [--json]` | Batch Rate My Professors lookup; unrated is `null`, never `0.0`. |
| `conflicts CRN... [--term CODE] [--refresh] [--json]` | Pairwise conflicts, ASCII week grid and unscheduled sections. |
| `draft [--term CODE] [--max-credits N] [--avoid-day D] [--earliest HH:MM] [--latest HH:MM] [--include-summer] [--refresh] [--json]` | Draft a conflict-free plan with rationale. |
| `roadmap [--start-term CODE] [--term CODE] [--include-summer] [--max-credits N] [--refresh] [--json]` | Build a multi-term plan with graduation-risk notes. |
| `offering-matrix [COURSE...] [--refresh] [--json]` | Infer every-term / fall-only / spring-only / unknown offering patterns. |
| `plans [--term CODE] [--json]` | List Banner Plan Ahead worksheets for one term (term-scoped). |
| `plan list [--term CODE] [--json]` | Same as `plans`, under the `plan` subcommand. |
| `plan create --name TEXT [--term CODE] [--json]` | Create a plan worksheet. **Writes to Banner.** |
| `plan add --plan ID_OR_NAME (--crn CRN \| --subject S --number N) [--term CODE] [--json]` | Add a section or course to a plan. **Writes to Banner.** |
| `plan remove --plan ID_OR_NAME (--crn CRN \| --subject S --number N) [--term CODE] [--json]` | Remove one course row from a plan. **Writes to Banner.** |
| `plan delete --plan ID_OR_NAME [--term CODE] [--json]` | Delete a plan. **Writes to Banner, irreversible.** |
| `plans-json [--term CODE]` | Legacy raw plan dump; prefer `plan list --json`. |
| `mcp` | Run the stdio MCP server (same as the `unw-mcp` console script). |

## MCP server

`unw-mcp` (or `unwplanner mcp`) runs a stdio MCP server that exposes 19 tools:
session health, profile, audit, remaining requirements, terms, search, course
detail, candidates, RMP ratings, conflicts, draft, roadmap, offering matrix, and
the term-scoped Banner plan operations (create/add/remove/delete), plus a
registration preview that never writes. It advertises its own usage instructions
to the agent on connection.

Register it with your MCP client as a stdio server using an absolute path to
`<repo>/.venv/bin/unw-mcp`; see [`docs/mcp.md`](docs/mcp.md) for the exact local
registration, tool table, real request/response examples, error contract and
session-recovery procedure.

## Safety model

- **Credentials stay local.** `UNW_USER`, `UNW_PASS` and `UNW_TOTP_SECRET` live
  only in `.env` (mode 600, gitignored). No code hardcodes them and no command
  prints them.
- **The session state is a secret.** `.state/unw_state.json` is a replayable
  cookie jar for Banner, Entra ID and Degree Works. It is gitignored and must not
  be copied into tickets, chat or commits. Delete it or overwrite it with
  `unwplanner login --force` if exposed.
- **No registration writes.** No command or tool posts add/drop/register. The
  registration preview is read-only; Banner's own eligibility failure text is
  reported as-is.
- **Plan writes are explicit.** Only `plan create`, `plan add`, `plan remove` and
  `plan delete` (and the matching MCP tools) write to Banner, and only to Plan
  Ahead worksheets. They never run implicitly during a read command.
- **Captures are never committed.** `.state/`, `captures/` and `.venv/` are
  gitignored. Capture files may contain course data, screenshots or redacted
  request logs; they stay on the local machine.
- **Be polite.** The client reuses one session and caches term snapshots for 15
  minutes. Do not run a second browser against the same session while an HTTP
  call is in flight; Banner invalidates the old session. Keep request rates low
  and respect the sites' terms of service.

## Known limitations

- **Registration is not implemented.** The registration write endpoints could
  not be exercised without an open registration time ticket, so they are
  deliberately unmapped. `unwplanner doctor`/`profile` report eligibility, and
  `unw_register_preview` (MCP) reports Banner's eligibility failure text, but no
  add/drop/register request is ever sent.
- **Filtering is client-side.** Banner ignores `txt_attribute`,
  `txt_instructor`, `chk_open_only` and similar parameters on this deployment, so
  the tool fetches a whole-term snapshot (cached for 15 minutes) and filters
  locally. Seat counts are therefore a snapshot, not live seat guarantees.
- **Plans are term-scoped and bounded.** Plan Ahead only works for selectable
  terms; terms beyond Summer 2027 are visible in the catalog but cannot be
  planned. An empty plan list means "no plans for this term", not "no plans
  exist".
- **RMP coverage is thin.** Many faculty have no Rate My Professors record or
  very few ratings. Unrated means unknown, not bad; n<3 is weak evidence.
- **Offering patterns are inferred.** "Fall-only" or "every-term" comes from
  seven observed catalog terms, not a registrar guarantee. Alternate-year
  offerings and unpublished future catalogs can change the answer.
- **Prerequisite parsing is heuristic.** Banner's nested `and`/`or` prerequisite
  text and grade/test clauses are parsed approximately; always confirm with an
  advisor.
- **One student per checkout.** Settings and state are process-wide, not
  multi-user.

## Repository layout

| path | contents |
| --- | --- |
| `src/unw_planner/` | Library: `auth`, `audit/`, `banner/`, `catalog`, `planner`, `schedule`, `rmp/`, `cli`, `mcp_server`. |
| `docs/` | Login flow, Banner API map, audit parser, planning engine, RMP API, MCP server. |
| `scripts/` | Login, probe, draft, advising artifacts, RMP probe and MCP smoke test. |
| `tests/` | Offline pytest suite plus scrubbed fixtures captured from the live services. |
| `pyproject.toml` | Package metadata, dependencies, `unwplanner` and `unw-mcp` entry points. |
| `.env.example` | Credential template; copy to `.env` and fill in. |

## Disclaimer

This is unofficial software. Use it only with your own account, at your own
risk. Course data, audit results and instructor ratings can be wrong or stale;
verify everything with your advisor and the official UNW systems before making
enrollment decisions. Respect the terms of service and rate limits of UNW,
Ellucian and Rate My Professors.
