# Security policy

## Supported versions

This project is pre-1.0. Security fixes go to the latest `0.1.x` state of the
main branch; older snapshots are not maintained.

## Reporting a vulnerability

Do not open a public issue for a vulnerability, a leaked credential, or a file
containing personal data. Report it privately to the maintainer of the repository
you cloned from (or through the hosting service's private security-advisory
feature) with:

- the affected path or command,
- what is exposed and how to reproduce it,
- whether any live session, password, TOTP secret or student record is involved.

If a live credential or session cookie was committed, rotate it before
investigating the cause: change the UNW password, re-enroll the TOTP secret if
needed, and assume a committed `.state/unw_state.json` is a compromised
session.

## Secrets and personal data

| item | handling |
| --- | --- |
| `.env` | `UNW_USER`, `UNW_PASS`, `UNW_TOTP_SECRET`. Mode 600, gitignored. Never commit, paste or log it. |
| `.state/unw_state.json` | Replayable cookies for Banner, Entra ID and Degree Works. Gitignored; treat as a password. |
| `captures/` | Screenshots and redacted request transcripts. Gitignored. May still contain course data, names or URLs; treat as private. |
| Term snapshots, RMP cache | `.state/`; public catalog and rating data, but still local state. |

`scripts/login.py` writes `.state/` and redacts passwords, TOTP codes, cookies
and authorization headers in `captures/login.jsonl`. Redaction is best-effort:
review any capture before sharing it.

If you fork this repository, keep `.env`, `.state/`, `captures/` and `.venv/`
ignored, and do not add a fixture captured from a live authenticated session
without scrubbing the student's name, id, email and any cookie or token.

The repository's test fixtures are scrubbed (`REDACTED STUDENT`, `00000000`,
`redacted@example.edu`). A fixture that contains a real student identity is a
bug; report it privately.

## Write safety

- The MCP server never posts add/drop/register/withdraw requests. Only the
  explicit `eagle-flightplan snipe` command can submit: it defaults to dry-run, and
  `--live` additionally requires `--i-understand` plus typing the exact term code
  on stdin. It never drops or withdraws and has not been validated against a live
  registration window. `windows`/`watch` and the MCP preview are read-only.
- Banner Plan Ahead writes happen only through the explicit `plan create`,
  `plan add`, `plan remove`, `plan delete` commands and the matching MCP tools.
  They are not triggered by read or planning commands.
- The MCP `unw_register_preview` tool only probes term eligibility; it reports
  Banner's failure text and performs no writes.
- Do not run a browser and the httpx client against the same `JSESSIONID`
  concurrently. The observed result is that one side invalidates the other.

## Scope and safe use

Use the tool only with your own UNW account, at reasonable request rates, and in
accordance with UNW, Ellucian and Rate My Professors terms of service. The
project is unofficial and comes with no warranty; see the README disclaimer.
