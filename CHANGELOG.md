# Changelog

All notable changes to this project are documented here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/) and the project uses
[Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Fixed

- Audit grades: `F`, `WF`, `NC`, `U`, `W`, `WP`, `I` and `AU` no longer count as
  completed. They are retained with `failed`/`withdrawn`/`incomplete`/`audit`
  status, never enter `applied_codes`/applied credits/pick counts, and a failed
  course can be retaken.
- Prerequisites are tri-state. Empty/"no prerequisite" text is satisfied, parsed
  text is evaluated, and non-empty unparsable text or a fetch failure returns
  `unknown` (never `True`). Unknown prerequisites are placed with
  `prereq_unverified: true` and a warning; the registration preview reports
  `prereq_ok: null`.
- Prose prerequisite fallback for forms like `Prerequisite: CYS 3065` and
  `PQ: COS 2015 or COS 2112`.
- Roadmap accounting for subject-wildcard pools (`DAL *`): progress/counts are
  updated from the section's subject, so the pool is not replanned every term.
- The minimum-load constraint is now enforced; a plan reports
  `min_credits_met`.
- Variable-credit sections reserve the low end of their range by default and
  report `credit_range`; `hours` overrides the reserved value.
- Subject-wildcard and attribute progress in multi-term roadmaps.
- Term snapshots are written atomically; a corrupt cache is treated as missing
  and refetched.
- The ratings cache stores `fetched_at`, honors the 7-day TTL on every read and
  never persists RMP failures or legacy failure entries.
- Required general-education blocks are no longer demoted to `gen_ed_fill` just
  because their group title contains a keyword (e.g. "LEADERSHIP").
- `unw_banner_plan_add` runs the prerequisite gate with the student's applied
  courses before any Banner write.
- `unw_search` caps `limit` at 200 and returns `limit_capped`/`next_offset`.
- `import eagle_flightplan.catalog` no longer fails with a circular import.
- **`snipe` is now stage-then-commit.** Banner's Add/CRN control only stages a
  pending row; Save commits it. `execute_targets` fills every target in dry-run
  (no clicks), stages each live target, then clicks Save exactly once. A missing
  Save returns `staged_not_committed`; an unclear Save returns `save_unknown`;
  unknown staging returns `stage_unknown` and never clicks Save; authorization/OLR
  popups return `authorization_required` and are not accepted.
- `run_browser` now binds the term in registration mode before opening the UI and
  returns `not_eligible` with Banner's exact `studentEligFailures` text; `seats`
  mode waits for a seat first, then binds registration. `--probe` still skips it.
- The old claim that clicking Add registers a section was wrong and is corrected
  in `docs/registration.md` and `docs/banner-api.md`.

### Changed

- Ratings cache compatibility: `.state/ratings.json` is now flat (all
  `RatingInfo` fields plus `fetched_at`). Readers accept flat and nested
  entries; old processes can read new flat rows without a `ValidationError`.
- `unw_session` reports `degraded`, `read_hint` and `write_hint`; a failed live
  check no longer orders an agent to stop read-only work. Repair hints use
  `python -m eagle_flightplan.cli login --force` (works in editable and wheel
  installs) instead of `scripts/login.py`.
- Setup: `--no-login` accepts a missing cached session as informational; a
  non-interactive run without `--password-stdin` fails with an actionable
  `SetupError`. `--register-mcp` now merges an omp/Claude/Cursor stdio entry with
  backup and idempotency instead of being a no-op.
- Data root: `EAGLE_FLIGHTPLAN_HOME` overrides non-editable installs; editable installs keep
  the checkout. `ENV_PATH`, `.state`, `captures`, term/prereq caches and the MCP
  chdir all derive from the resolved root.
- Release/install: bootstrap scripts, README badge/metadata, `.env.*` ignore,
  wheel-install smoke and concrete MCP config examples.
- Renamed the project to Eagle Flightplan: package `eagle_flightplan`, entry points `eagle-flightplan` and
  `eagle-flightplan-mcp`, MCP registration key `eagle-flightplan`, and data env
  `EAGLE_FLIGHTPLAN_HOME`.
- Agent UX: `unw_candidates` `compact`/`limit`/`open_only`; `grid_legend` on
  `unw_conflicts`/`unw_plan_draft`; `credits_semantics`/`part_of_term_note` on
  sections; `shared_title_with` on `unw_remaining`.

- `unw_plan_draft` accepts `min_credits`, `required_crns`, `required_courses`,
  `exclude_crns`, `exclude_courses`, `avoid_instructors`, `exclude_arrangement`
  and `hours`; failed pins return `ok: false` with `pin_errors` and the partial
  draft. Pins are draft-only (`unw_roadmap` documents this).
- New read-only `unw_registration_window` MCP tool (20 tools total).
- Planner availability uses the audit's `activeTerm` instead of only the
  hard-coded fallback.
- `include_summer` carries spring selections into the summer prerequisite set.

### Unverified

- The new MCP paths are covered by offline tests only; `unw_registration_window`
  and plan-add prerequisite refusal were not exercised against live Banner.
- `snipe` is offline-tested only. The literal stage/Save URLs, Save request body,
  Save response, waitlist commit path and separate validate endpoint are
  server-rendered/not proven; no live registration write was run.

## [0.1.0] - 2026-11-11

### Added

- Entra ID + TOTP browser login with replayable cookie state, session-age and
  session-validity checks, automatic re-login, and a `doctor` command.
- Banner 9 SSB client: terms and eligibility, class search, section detail,
  linked sections, and whole-term snapshots with a 15-minute cache and SQLite
  offering index.
- Ellucian Degree Works client and parser: requirement groups/blocks/options,
  applied and in-progress courses, transfer/AP flags, profile fields and a
  structured requirement tree.
- Catalog and planning engine: requirement resolution, prerequisite parsing,
  conflict-free drafts with `max_credits`/day/time constraints, multi-term
  roadmaps, offering-pattern inference, and verification of every draft.
- Registration-window detection (`windows`/`watch`, read-only) and a
  browser-driven `snipe` command that defaults to dry-run; `--live` requires
  `--i-understand` and the exact term typed on stdin.
- Rate My Professors GraphQL client with nickname-aware name matching, SQLite
  cache, conservative ambiguity handling, and explicit `null` for unrated
  faculty.
- `eagle-flightplan` CLI: setup wizard, login, terms, search, audit, plans, advising
  brief (`advise`), and explicit `plan-create` / `plan-add` / `plan-delete`
  writes.
- 19-tool stdio MCP server (`eagle-flightplan-mcp`) covering session, profile, audit,
  remaining requirements, terms, search, course detail, candidates, ratings,
  conflicts, draft, roadmap, offering matrix, term-scoped plan operations, and a
  read-only registration preview. Plan writes require `confirm=true`; delete
  additionally requires the exact plan name, and every write returns a
  before/after diff read back from Banner.
- Offline test suite with scrubbed fixtures, a hermetic MCP stdio smoke test, a
  curated Ruff configuration, coverage configuration, and Python 3.11/3.12 support.

### Verified live

- Entra ID password + TOTP login, cookie replay, and session recovery.
- Term listing, class search, section detail, and term snapshots for seven terms.
- Degree Works audit retrieval/parsing, profile fields, and unmet requirements.
- Rate My Professors search and name matching for UNW (school id 706).
- Banner plan listing and one end-to-end throwaway plan lifecycle during API
  documentation (create, add a section, read back, remove, delete).
- MCP surface startup and tool listing over stdio; plan-write guards, refusal
  paths, and add-success diff against a mocked Banner transport.

### Unverified / caveats

- Registration add/drop/register/withdraw requests. No registration window and
  no time ticket were available, so those endpoints are deliberately unmapped.
- The `snipe` registration command is browser-driven and has not been executed
  against a live registration window; the MCP server exposes no registration
  write. Its dry-run path and safety gates are unit-tested.
- The MCP plan-write success paths were exercised only against a mocked Banner
  transport in this repository; live plan writes were verified earlier while
  mapping the API, not re-run by the test suite.
- Prerequisite parsing is heuristic; grade and test/placement clauses are not
  fully modelled.
- Offering patterns come from seven observed terms, not a registrar guarantee.
- Rate My Professors coverage is thin for many faculty; ratings are a soft
  preference, not evidence of teaching quality.
