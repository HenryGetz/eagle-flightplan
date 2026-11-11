# UNW planner MCP server

`unw_planner.mcp_server` exposes the degree audit, Banner 9 class search, the
planning engine and the term-scoped Plan Ahead worksheets over MCP **stdio**.
It is the same code path as the `unwplanner` CLI: every read command prints the
payload the matching tool returns.

| item | value |
| --- | --- |
| module | `<repo>/src/unw_planner/mcp_server.py` |
| stdio entry point | `<repo>/.venv/bin/python -m unw_planner.mcp_server` |
| console script | `<repo>/.venv/bin/unw-mcp` |
| registration key | `unw-planner` in `~/.omp/agent/mcp.json` |
| transport | stdio only |
| instructions | advertised in the MCP `initialize` result (55 lines) |

The server never writes to Banner registration. The only writes are Plan Ahead
worksheet operations (`*_plan_create/add/remove/delete`), which are explicitly
named and documented as writes.

---

## Tool table

All tools return a JSON object. Success payloads start with `"ok": true`;
expected domain failures return `"ok": false`, `"error"`, `"error_type"` and an
actionable `"hint"` instead of raising.

| tool | arguments | returns / purpose |
| --- | --- | --- |
| `unw_session` | — | `authenticated`, state-file age, cookie count, earliest non-session cookie expiry (never cookie values), and the exact repair command. Call first. |
| `unw_profile` | — | Name, id, classification, degree/school, catalog year(s), majors, minors, programs, college, advisors, credits earned/in progress, GPA, percent complete, anticipated graduation, academic standing. `holds` is `null` because no mapped endpoint exposes it. |
| `unw_audit` | `group?` | Requirement tree: groups → blocks → options with applied/in-progress/transfer flags. `group` filters by a case-insensitive substring of group title/kind or block label. |
| `unw_remaining` | — | Only unmet requirements: id, title, parent group, kind, status, priority, credits remaining, pick-N progress, options (courses/`ATTRIBUTE X`/`SUBJ *`). |
| `unw_terms` | `refresh?=false` | Term codes/descriptions plus `is_next`, `is_current`, `view_only`, `registration_eligible` and Banner's eligibility failure text. |
| `unw_search` | `term`; `subject?`, `number?`, `keyword?`, `attribute?`, `instructor?`, `open_only?`, `limit?=50`, `offset?=0`, `refresh?=false` | Normalised sections from the term snapshot. Filtering is client-side because Banner ignores those query params. |
| `unw_course` | `term`, `crn`, `refresh?=false` | Full section detail: meetings, faculty + RMP each, attributes, description, prereq/coreq/restriction text and parsed prereq codes, linked CRNs, enrollment text, availability. |
| `unw_candidates` | `term`; `requirement?`, `refresh?=false` | Resolves each unmet requirement (or one id/title substring) to offered sections, with the explicit reason when empty. |
| `unw_ratings` | `names` (list or comma string) | Batch RMP lookup. Accepts `"Last, First"` or `"First Last"`. Unrated → `rating: null`, never `0.0`; `weak_evidence` flags n<3. |
| `unw_conflicts` | `term`, `crns`, `refresh?=false` | Pairwise conflicts, ASCII week grid, total credits, unscheduled (TBA) sections, missing CRNs. |
| `unw_plan_draft` | `term`; `max_credits?=16`, `profile?="max_rated"`, `avoid_days?`, `earliest?`, `latest?`, `include_summer?=false`, `refresh?=false` | Conflict-free draft: picks with rationale, credits, average RMP, grid, verification problems, unplaced requirements + reasons. `include_summer` also drafts the following summer for requirements that did not fit. |
| `unw_roadmap` | `start_term`; `terms?`, `include_summer?=false`, `max_credits?=16`, `refresh?=false` | Multi-term plan (default: the start term + next three non-summer terms), carried-forward requirements/prereqs, `final_remaining` and graduation-risk notes against the audit's `GRADTERMUG`. |
| `unw_offering_matrix` | `courses?` (list or comma string), `refresh?=false` | Offering pattern per `CYS 4245` / `ATTRIBUTE NATS` / `DAL *`: `every-term`, `fall-only`, `spring-only`, `summer-only`, `fall+summer`, `spring+summer`, `unknown`, with section counts/open counts/instructors per term. Omit `courses` for all unmet targets. |
| `unw_banner_plans` | `term` | Plans for **one term**. TERM-SCOPED: an empty list means no plans for THIS term. `plan_ahead_available: false` means Banner does not offer Plan Ahead for that term at all (e.g. 202710). |
| `unw_banner_plan_create` | `term`, `name` | **Writes.** Creates a named worksheet (max 5/term). Verify availability and user confirmation first. |
| `unw_banner_plan_add` | `term`, `plan`, `crn?` or `subject`+`number` | **Writes.** Adds a section or a course to a plan. `plan` = id or exact name; ambiguous/missing names are rejected with the available candidates. |
| `unw_banner_plan_remove` | `term`, `plan`, `crn?` or `subject`+`number` | **Writes.** Removes one plan row and returns the updated plan. |
| `unw_banner_plan_delete` | `term`, `plan` | **Writes, irreversible.** Deletes a plan (Banner wants its per-term sequence number internally). |
| `unw_register_preview` | `term`, `crns`, `refresh?=false` | **DRY RUN ONLY.** Banner's real eligibility flags/failure text plus local seat, conflict and prerequisite checks. Never POSTs add/drop/register. |

Cached data reports provenance in `catalog`: `source` (`network`/`cache`),
`age_seconds`, `age_human`, `sections`, `request_count`. `refresh: true` forces
a live refetch where the tool supports it.

---

## Example calls (real responses, trimmed)

These were produced against the live account on 2026-11-11. `…` marks trimmed
arrays/strings. Live values drift between calls (the audit worksheet gained a
Ministry minor and its percent-complete changed during integration), so treat
the numbers below as shape examples, not fixtures. Absolute paths are shown as `<repo>` and identity fields are redacted.

### `unw_session`

```json
{"authenticated": true,
 "state_age_human": "2.5 h",
 "cookie_count": 24,
 "earliest_cookie_expiry": "2026-11-21T18:37:55.974701+00:00",
 "fix_command": "cd <repo> && <repo>/.venv/bin/python scripts/login.py --force"}
```

### `unw_profile`

```json
{"name": "REDACTED STUDENT", "id": "00000000", "classification": "Junior",
 "catalog_year": "2024-2025 (Fall 2024)",
 "catalog_years": ["202410", "202510"],
 "majors": ["Cybersecurity", "Computer Science"],
 "minors": ["Ministry", "Bible"],
 "programs": ["Computer Science-BS", "Cybersecurity-BS"],
 "advisors": [{"name": "Zderad, Jonathan A", "email": "jazderad@unwsp.edu", "source": "goalArray"},
              {"name": "Megan Brown", "email": null, "source": "reportArray"}],
 "credits_earned": 86.0, "credits_in_progress": 15.0, "gpa": 4.0,
 "percent_complete": 71, "anticipated_graduation": "Spring 2028",
 "academic_standing": "Good Standing 21 or more cr",
 "holds": null,
 "holds_note": "Holds are not exposed by the mapped Degree Works /api/audit payload … null means UNKNOWN, not 'none' …"}
```

### `unw_audit(group="cyber")`

```json
{"student_name": "REDACTED STUDENT", "catalog_year": "2024-2025 (Fall 2024)",
 "filter": "cyber", "group_count": 2,
 "groups": [{"id": "RA000608", "title": "Major: Cybersecurity", "kind": "major",
             "status": "in_progress", "required_credits": 48.0,
             "earned_credits": 34.0, "remaining_credits": 14.0,
             "blocks": [], "sub_groups": [{"title": "CORE REQUIREMENTS - 12 credits",
               "status": "not_met", "required_credits": 12.0, "earned_credits": 8.0,
               "blocks": [{"label": "Python Programming", "status": "not_met",
                           "notes": "choose 4 credits from the following",
                           "options": [{"code": "COS 2005", "credits": null, "status": "option", …}, …]}, …]}, …]}, …]}
```

### `unw_remaining`

```json
{"count": 18,
 "requirements": [{"id": "RA000608:1:0", "title": "Python Programming",
   "group": "CORE REQUIREMENTS - 12 credits", "kind": "major", "status": "not_met",
   "priority": "major_required", "credits_remaining": 4.0, "min_credits": 4.0,
   "applied_credits": 0.0, "pick_n_of": null, "applied_count": 0,
   "remaining_picks": null, "options": ["COS 2005", "COS 2211", "COS 2212"],
   "attributes": [], "subject_wildcards": [], "major_specific": true,
   "optional": false, "requires_arrangement": false,
   "notes": "choose 4 credits from the following"}, …]}
```

### `unw_terms`

```json
{"next_term": "202620", "current_term": "202610", "count": 5,
 "terms": [{"code": "202630", "description": "Summer 2027", "label": "Summer 2027",
            "is_next": false, "is_current": false, "view_only": false,
            "registration_eligible": false, "reg_allowed": false,
            "eligibility_failures": ["Term not eligible for registration"]},
           {"code": "202620", "description": "Spring 2027", "label": "Spring 2027",
            "is_next": true, "is_current": false, "view_only": false,
            "registration_eligible": false, "reg_allowed": false,
            "eligibility_failures": ["Term not eligible for registration"]}, …]}
```

### `unw_search(term="202620", subject="CYS", limit=1)`

```json
{"term": "202620", "total_matched": 6, "count": 1, "offset": 0, "limit": 1,
 "catalog": {"source": "cache", "age_human": "5s", "sections": 845},
 "sections": [{"crn": "22445", "code": "CYS 2081", "section": "01",
   "title": "Data Communications", "credits": 4.0, "capacity": 30, "enrollment": 0,
   "seats_available": 30, "open": true, "availability": "open", "days": "TT",
   "times": "12:45-14:25",
   "instructors": ["Smith, James"],
   "meetings": [{"day": "Tue", "start": "12:45", "end": "14:25",
                 "location": "Riley R1332", "kind": "Class", "scheduled": true}, …],
   "attributes": [{"code": "CDMS", "description": "Computing Data Math Sci Dept"}, …],
   "part_of_term": "1", "instructional_method": "Activity/Experience"}, …],
 "note": "Filtering is client-side: Banner ignores txt_attribute/txt_instructor/chk_open_only …"}
```

### `unw_course(term="202620", crn="22445")`

```json
{"term": "202620", "crn": "22445",
 "course": {"code": "CYS 2081", "title": "Data Communications", "days": "TT",
            "times": "12:45-14:25", "seats_available": 30, "availability": "open", …},
 "detail": {"description": "(PQ: COS2015 or COS2112) This course is a comprehensive overview …",
   "prerequisites": ["Area Prerequisites", "Prerequisites:PQ for CYS2081", "(", "Course or Test: COS: Computer Science 2015", …],
   "prerequisite_codes": ["COS 2015", "COS 2112"],
   "corequisites": ["No corequisite course information available."],
   "restrictions": ["Must be enrolled in one of the following Levels:", "Undergraduate (UG)", …],
   "linked_sections": [],
   "enrollment_text": "Enrollment Actual:\n0\nEnrollment Maximum:\n30\nEnrollment Seats Available:\n30 …",
   "faculty": [{"name": "Smith, James", "email": "jlsmith2@unwsp.edu", "primary": true,
                "rmp": {"matched_name": "James Smith", "rated": true, "rating": 3.7,
                        "difficulty": 2.9, "num_ratings": 14, "confidence": 0.99}}]}}
```

### `unw_candidates(term="202620", requirement="Python")`

```json
{"term": "202620", "requirement_count": 2,
 "requirements": [{"requirement": {"id": "RA000608:1:0", "title": "Python Programming",
                    "priority": "major_required", "credits_remaining": 4.0, …},
                   "found": true, "reason": "", "attribute_hits": {}, "count": 2,
                   "candidates": [{"crn": "22201", "code": "COS 2005",
                                   "instructors": ["Goetzman, Isaac"], "days": "MWF",
                                   "times": "12:45-13:50", "seats_available": 30,
                                   "availability": "open", …}, …]}, …]}
```

An empty result always carries a reason, e.g.
`"none of CYS 4245, MIS 4245 is offered in 202620"`,
`"attribute TPHI have zero sections in 202620; the requirement cannot be filled this term"`,
`"CYS 4465 is already completed or in progress"`.

### `unw_ratings(names=["Zderad, Jonathan"])`

```json
{"count": 1,
 "ratings": [{"name": "Zderad, Jonathan", "query": "Zderad, Jonathan",
   "matched_name": "Jonathan Zderad", "rated": true, "rating": 5.0,
   "difficulty": 2.7, "num_ratings": 7, "confidence": 0.99, "source": "rmp",
   "note": "", "weak_evidence": false}],
 "note": "Unrated is null, not 0.0. n<3 ratings are weak evidence …"}
```

An unrated name returns `"rated": false, "rating": null, "difficulty": null,
"num_ratings": 0, "note": "no professor matched …"`.

### `unw_conflicts(term="202620", crns=[...])`

```json
{"term": "202620", "resolved": ["22201", "23189", "20737", "20226", "23109"],
 "missing_crns": [], "conflict_count": 0, "conflicts": [],
 "conflict_summary": "No conflicts.", "total_credits": 16.0,
 "grid": "      Mon           Tue           Wed           Thu           Fri           Sat           Sun\n07:00 . …",
 "unscheduled": [{"crn": "23189", "code": "BIB 3223", "times": "TBA", …}, …]}
```

Conflicts are reported with both CRNs and the exact overlap, e.g.
`"Tue 10:30-11:00 (30 min): CRN 12345 CYS 2081 (Data Communications) overlaps CRN 67890 …"`.

### `unw_plan_draft(term="202620", max_credits=16)`

```json
{"requested": {"term": "202620", "max_credits": 16.0, "profile": "max_rated",
               "avoid_days": [], "earliest": null, "latest": null,
               "include_summer": false},
 "catalog": {"source": "cache", "age_human": "5s", "sections": 845},
 "draft": {"total_credits": 16.0, "selection_count": 5, "avg_rmp_rating": 4.12,
   "verified": true, "verify_problems": [],
   "selections": [{"crn": "22201", "course": "COS 2005", "instructor": "Goetzman, Isaac",
     "rating": {"rated": false, "rating": null, "note": "no professor matched …"},
     "days": "MWF", "times": "12:45-13:50", "credits": 4.0, "seats": 30,
     "availability": "open", "covers": ["RA000608:1:0", "RA001738:1-0:0"],
     "rationale": "required for the major; no RMP record (ranked below rated instructors); 30 seats open"}, …],
   "unplaced": [{"requirement": "Cybersecurity: Current Practices & Trends",
                 "reason": "none of CYS 4245, MIS 4245 is offered in 202620"}, …],
   "warnings": [], "grid": "…", "score_breakdown": ["5 section(s), 16/16 credits", …]}}
```

### `unw_roadmap(start_term="202620", terms=["202620","202630","202710"])`

```json
{"terms": ["202620", "202630", "202710"], "anticipated_graduation": "Spring 2028",
 "planned": [{"term": "202620", "label": "Spring 2027", "status": "planned",
              "credits": 16.0, "courses": ["COS 2005", "ICS 2045", "ICS 3005", "MIN 3045", "BIB 3223"]},
             {"term": "202630", "label": "Summer 2027", "status": "planned",
              "credits": 16.0, "courses": ["MIN 5510", "MIN 1641", …]}, …],
 "final_remaining": ["Additional credits for double major with Cybersecurity (Major: Computer Science)",
                     "Ministry Foundations (Minor: Ministry)", …],
 "risks": ["Additional credits for double major with Cybersecurity: COS 4841: every-term (…) …",
           "The audit anticipates graduation in Spring 2028; verify the remaining courses are offered before that term …"]}
```

### `unw_offering_matrix(courses=["CYS 4245","CYS 2005","ATTRIBUTE NATS"])`

```json
{"observed_terms": ["202520","202530","202610","202620","202630","202710","202720"],
 "observed_labels": ["Spring 2026","Summer 2026","Fall 2026","Spring 2027","Summer 2027","Fall 2027","Spring 2028"],
 "courses": [{"key": "CYS 4245", "pattern": "fall-only", "offered_terms": ["202710"],
              "summary": "fall-only; offered in Fall 2027",
              "terms": [{"term": "202520", "offered": false, "sections": 0}, …,
                        {"term": "202710", "offered": true, "sections": 1,
                         "open_sections": 1, "instructors": ["Faculty, Unassigned"]}, …]},
             {"key": "CYS 2005", "pattern": "unknown", "offered_terms": [],
              "summary": "never observed in 7 cached terms (…)"}, …]}
```

### `unw_banner_plans(term="202620")`

```json
{"term": "202620", "term_scoped": true, "plan_ahead_available": true, "count": 4,
 "plans": [{"id": 59003, "name": "Plan A - Max Rated 16cr", "sequence_number": 1,
            "preferred": true, "total_planned_credit_hours": 16.0,
            "courses": [{"code": "LIT 2157", "crn": "23109", "credits": 2.0, "status": "Planned"}, …]}, …],
 "note": "Plans are TERM-SCOPED: this list contains only plans whose planHeader.term is 202620. An empty list means no plans for THIS term, not that no plans exist."}
```

`unw_banner_plans(term="202630")` is a real empty result (`count: 0`,
`plan_ahead_available: true`). `unw_banner_plans(term="202710")` returns
`count: 0, plan_ahead_available: false` because Banner does not render Plan
Ahead for that term.

### `unw_register_preview(term="202620", crns=["22201","23189"])`

```json
{"dry_run": true, "registration_attempted": false, "writes_performed": [],
 "can_register": false,
 "banner_message": "Term not eligible for registration",
 "eligibility": {"reg_allowed": false, "student_elig_valid": false,
   "student_elig_failures": ["Term not eligible for registration"],
   "fwd_url": "/StudentRegistrationSsb/ssb/classRegistration/classRegistration"},
 "sections": [{"crn": "22201", "code": "COS 2005", …}, {"crn": "23189", "code": "BIB 3223", …}],
 "missing_crns": [], "conflict_count": 0, "conflicts": [],
 "prerequisites": {"checked": true, "note": "",
   "rows": [{"crn": "22201", "course": "COS 2005", "prereq_ok": true, "reason": ""}, …]},
 "note": "This endpoint NEVER posts add/drop/register …"}
```

### Plan writes

These tools were **not executed successfully** during integration: the
student's plans are load-bearing. The real responses below are the safe
rejection paths; the success shape is the `Plan` model returned by
`banner/plan.py`.

`unw_banner_plan_add(term="202620", plan="No Such Plan", crn="22201")`:

```json
{"ok": false,
 "error": "PlanNotFoundError: No plan named 'No Such Plan' in this term. Available: 59003 'Plan A - Max Rated 16cr'; 59005 'Plan B - Major Forward 18cr'; 59013 'Plan C - Light 12cr'; 59021 'Advisor Plan - Spring 2027'",
 "error_type": "PlanNotFoundError",
 "hint": "Banner rejected the plan operation. Re-read the plan list before retrying; never repeat a write after an HTTP 500 without checking unw_session."}
```

`unw_banner_plan_create(term="202710", name="…")`:

```json
{"ok": false,
 "error": "PlanError: Banner rendered no Plan Ahead list for 202710; the term is not selectable in Plan Ahead (the session itself is alive). No plans can be read or written for this term.",
 "error_type": "PlanError",
 "hint": "Banner rejected the plan operation. Re-read the plan list before retrying; …"}
```

Success shapes (from the frozen `PlanClient` models):

```json
// create
{"ok": true, "term": "202620", "created": {<plan>}, "note": "The new plan is term-scoped; read it back with unw_banner_plans(term)."}
// add
{"ok": true, "term": "202620", "plan": {<plan>}, "added": {"id": 206722, "code": "CYS 2081", "crn": "22445", "credits": 4.0, "status": "Planned", …}}
// remove
{"ok": true, "term": "202620", "plan": {<plan>}}
// delete
{"ok": true, "term": "202620", "deleted": {"id": 59003, "name": "Plan A - Max Rated 16cr"}, "remaining_plans": 3}
```

---

## Error contract and cases

Domain failures are data, not exceptions: the tool returns `ok: false` with
`error` (`"<ExceptionType>: <message>"`), `error_type` and `hint`. MCP input
schema violations (wrong JSON type, missing required argument) are reported by
the SDK as an `isError` tool result instead.

| case | real behaviour | what to do |
| --- | --- | --- |
| expired session | `SessionExpiredError` (Banner returned HTML/guest page) | run `fix_command` from `unw_session`; never retry a write blindly |
| missing/expired credentials | `AuthError` | `unwplanner doctor`, then `scripts/login.py --force` |
| plan name ambiguous | `PlanError` listing each `id name` candidate | use the id or an unambiguous exact name |
| plan name/id missing | `PlanNotFoundError` listing available plans | call `unw_banner_plans(term)`; do not guess |
| Plan Ahead unavailable for term | `PlanError` "term is not selectable in Plan Ahead" | pick 202610/202620/202630, or another term with a plan list |
| unknown CRN | `ValueError` naming the term and section count | `unw_search` first |
| bad profile/day/time | `ValueError` with accepted values | fix the argument |
| RMP blocked/rate-limited | `RMPError` (or an unrated `null` row when only the name is missing) | ratings are optional; unrated ≠ 0.0 |
| already-registered course in plan | `DuplicateCourseError` (client-side guard; Banner itself allows duplicates) | remove the existing row first |

---

## For agents

This mirrors the server `instructions`; the full text is advertised on
`initialize`.

1. **Start with `unw_session`.** If `authenticated` is false, tell the user to
   run its `fix_command`. Never ask for a password, TOTP code or cookie.
2. **Then `unw_profile` and `unw_remaining`.** The audit already knows majors,
   minors, catalog years, advisors and credits — do not ask the student.
   **Do** confirm their declared program: students often say "ministry minor"
   while the audit says "Bible" (and an audit can carry both). Report
   discrepancies; never silently pick one.
3. **Ask before assuming:** credit budget, blocked days/times, earliest start,
   latest end, summer willingness, graduation target.
4. **Priority order:** scarce major-required courses first (check
   `unw_offering_matrix`; never propose a fall-only course for spring), then the
   major credit pool, then gen-ed by highest RMP rating.
5. **Never propose a time conflict.** Run `unw_conflicts` on the final CRN set
   and show the ASCII grid.
6. **Never write to Banner plans without explicit user confirmation**, and
   **never attempt real registration**. `unw_register_preview` is a dry run; the
   eligibility failure text is the answer.
7. **Unrated is unknown, not bad** (`null ≠ 0.0`); n<3 is weak evidence. State
   match confidence as name-matching confidence.
8. **Always show the grid and a per-pick rationale.** Flag `NOT_YET_OPEN` /
   `CLOSED`, single-section courses, unassigned faculty ("Staff"), and
   variable-credit rows. Seats are a snapshot.
9. **Plans are term-scoped.** `unw_banner_plans(term) == []` means no plans for
   *this* term, not "no plans exist".
10. **Report discrepancies** (already-completed course, requirement the plan
    drops, audit vs catalog conflict) instead of resolving them silently.

---

## Session recovery

The session state is `.state/unw_state.json` (Playwright storage state). Banner
invalidates the SSB session aggressively; the observed age can be well under an
hour even while polling.

1. Call `unw_session`. If `authenticated` is false, use the returned
   `fix_command`:

   ```bash
   cd <repo> && <repo>/.venv/bin/python scripts/login.py --force
   ```

   `scripts/login.py` drives Entra ID username → password → TOTP from `.env`
   automatically (no human needed) and rewrites the state file.
2. Re-call `unw_session`; it should report `authenticated: true`.
3. If login still fails, run `unwplanner doctor`. It checks `.env` (mode 600 and
   all three credentials), state age, live session validity, Chromium presence,
   the term-snapshot cache and RMP reachability, each with a one-line fix.
4. **Do not run a browser against the same `JSESSIONID` while an httpx call is
   in flight** — observed to invalidate both sides (`docs/login.md`). The httpx
   replay path is the primary one.
5. HTTP 500 from a plan batch write also drops the SSB session: re-run the login
   script and re-read the plan before retrying anything.

Never print `.env` values, cookie values, TOTP secrets or state-file contents.

---

## Verification

```bash
cd <repo>
uv run --project . unwplanner doctor
uv run --project . unwplanner remaining
uv run --project . unwplanner search --term 202620 --subject CYS --json
.venv/bin/python scripts/smoke_mcp.py
.venv/bin/python -m pytest -q
.venv/bin/ruff check --select F,E9 src/unw_planner/mcp_server.py src/unw_planner/cli.py
```

`scripts/smoke_mcp.py` launches the server over real stdio, asserts the 19-tool
surface and every non-empty description, then calls `unw_session`,
`unw_profile`, `unw_terms`, `unw_search(202620, CYS)`,
`unw_ratings(["Zderad, Jonathan"])` and `unw_banner_plans("202620")`. It exits
non-zero on any error or empty result. It re-executes itself with
`.venv/bin/python` when started from an interpreter without the `mcp` package.

---

## Known integration notes

* `SSBClient.section_detail(..., section=...)` declares the **wire**
  `banner.models.Section`, but every snapshot query returns the normalised
  `models.Section`; passing the latter raises a pydantic `ValidationError`.
  `unw_course` therefore calls the individual detail endpoints
  (`getSectionPrerequisites`, `getCorequisites`, …) and normalises them itself.
* Banner renders no `plans:` bootstrap for non-selectable terms (202710/202720).
  The frozen `PlanClient.list_plans` treats that as a session expiry; the MCP
  wrapper verifies the session and reports `plan_ahead_available: false` instead.
* Term snapshots are shared across tools and protected by a process-wide lock,
  because Banner's session holds a single bound term. Two concurrent tool calls
  cannot read one term's sections under another term's key.
