# Eagle Flightplan MCP server

`eagle_flightplan.mcp_server` exposes the degree audit, Banner 9 class search, the
planning engine and the term-scoped Plan Ahead worksheets over MCP **stdio**.
The `eagle-flightplan` CLI and the MCP server share the same domain layer (`auth`,
`banner`, `audit`, `catalog`, `planner`, `report`); the MCP surface is the
broader read-only API.

| item | value |
| --- | --- |
| module | `<repo>/src/eagle_flightplan/mcp_server.py` |
| stdio entry point | `<repo>/.venv/bin/python -m eagle_flightplan.mcp_server` |
| console script | `<repo>/.venv/bin/eagle-flightplan-mcp` |
| registration key | `eagle-flightplan` in `~/.omp/agent/mcp.json` (also Claude Desktop / Cursor) |
| transport | stdio only |
| instructions | advertised in the MCP `initialize` result (60 lines) |

The server never writes to Banner registration. The only writes are Plan Ahead
worksheet operations (`*_plan_create/add/remove/delete`), which are explicitly
named and documented as writes.

---

## Client configuration

`eagle-flightplan setup --register-mcp` merges the entry below into omp
(`~/.omp/agent/mcp.json`), Claude Desktop (`claude_desktop_config.json`) and
Cursor (`~/.cursor/mcp.json`) when they are present. It preserves other
servers, backs up before writing, and is idempotent. Use the absolute path for
your checkout/install (`...\.venv\Scripts\eagle-flightplan-mcp.exe` on Windows):

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

The same block works for omp, Claude Desktop and Cursor. Restart the client
after adding or upgrading the server. A stale client process keeps the old code
and the old cache in memory; in particular, `.state/ratings.json` is now flat
(all `RatingInfo` fields plus `fetched_at`) but old processes may still read it
until they restart.

---

## Tool table

All tools return a JSON object. Success payloads start with `"ok": true`;
expected domain failures return `"ok": false`, `"error"`, `"error_type"` and an
actionable `"hint"` instead of raising.

| tool | arguments | returns / purpose |
| --- | --- | --- |
| `unw_session` | — | `authenticated` (live check), `degraded` (state exists but live check failed), state-file age, cookie count, earliest non-session cookie expiry (never cookie values), `read_hint`/`write_hint`, and the exact repair command. Call first. |
| `unw_profile` | — | Name, id, classification, degree/school, catalog year(s), majors, minors, programs, college, advisors, credits earned/in progress, GPA, percent complete, anticipated graduation, academic standing. `holds` is `null` because no mapped endpoint exposes it. |
| `unw_audit` | `group?` | Requirement tree: groups → blocks → options with applied/in-progress/transfer flags. `group` filters by a case-insensitive substring of group title/kind or block label. |
| `unw_remaining` | — | Only unmet requirements: id, title, parent group, kind, status, priority, credits remaining, pick-N progress, options (courses/`ATTRIBUTE X`/`SUBJ *`), and `shared_title_with` (other requirement ids with the same title/options: double-counting risk). |
| `unw_terms` | `refresh?=false` | Term codes/descriptions plus `is_next`, `is_current`, `view_only`, `registration_eligible` and Banner's eligibility failure text. |
| `unw_search` | `term`; `subject?`, `number?`, `keyword?`, `attribute?`, `instructor?`, `open_only?`, `limit?=50`, `offset?=0`, `refresh?=false` | Normalised sections from the term snapshot. Filtering is client-side because Banner ignores those query params. `limit` is capped at 200; `limit_capped` and `next_offset` drive paging. |
| `unw_course` | `term`, `crn`, `refresh?=false` | Full section detail: meetings, faculty + RMP each, attributes, description, prereq/coreq/restriction text and parsed prereq codes, linked CRNs, enrollment text, availability. |
| `unw_candidates` | `term`; `requirement?`, `open_only?=true`, `compact?=false`, `limit?=0` (0=all), `refresh?=false` | Resolves each unmet requirement (or one id/title substring) to offered sections, with the explicit reason when empty. `compact` omits duplicated section detail; `limit` caps candidates per requirement and reports `returned`/`truncated`. One requirement can be large. |
| `unw_ratings` | `names` (list, or one string), `refresh?=false` | Batch RMP lookup. Each name may be `"Last, First"` or `"First Last"`; a comma inside a single name is preserved. Unrated → `rating: null`, never `0.0`; `weak_evidence` flags n<3. Cache entries expire after 7 days; transient RMP failures are never cached. |
| `unw_conflicts` | `term`, `crns`, `refresh?=false` | Pairwise conflicts, ASCII week grid plus `grid_legend` (label → CRN list), total credits, unscheduled (TBA) sections, missing CRNs. |
| `unw_plan_draft` | `term`; `max_credits?=16`, `min_credits?=0`, `profile?="max_rated"`, `avoid_days?`, `earliest?`, `latest?`, `avoid_instructors?`, `exclude_arrangement?=false`, `required_crns?`, `required_courses?`, `exclude_crns?`, `exclude_courses?`, `hours?`, `include_summer?=false`, `refresh?=false` | Conflict-free draft: picks with rationale, credits, average RMP, grid plus `grid_legend`, verification problems, unplaced requirements + reasons. `required_crns`/`required_courses` are placed first and hard-fail (`ok: false` + `pin_errors`) when unavailable/conflicting/prereq-blocked/excluded/over budget; `min_credits` tops up from elective pools (`min_credits_met`); `hours` sets the credit hours reserved per CRN; `unw_roadmap` does not accept pins. `include_summer` also drafts the following summer for requirements that did not fit. |
| `unw_roadmap` | `start_term`; `terms?`, `include_summer?=false`, `max_credits?=16`, `refresh?=false` | Multi-term plan (default: the start term + next three non-summer terms), carried-forward requirements/prereqs, `final_remaining` and graduation-risk notes against the audit's `GRADTERMUG`. |
| `unw_offering_matrix` | `courses?` (list or comma string), `refresh?=false` | Offering pattern per `CYS 4245` / `ATTRIBUTE NATS` / `DAL *`: `every-term`, `fall-only`, `spring-only`, `summer-only`, `fall+summer`, `spring+summer`, `unknown`, with section counts/open counts/instructors per term. Omit `courses` for all unmet targets. |
| `unw_banner_plans` | `term` | Plans for **one term**. TERM-SCOPED: an empty list means no plans for THIS term. `plan_ahead_available: false` means Banner does not offer Plan Ahead for that term at all (e.g. 202710). |
| `unw_banner_plan_create` | `term`, `name`, `confirm` | **Writes.** Creates a named worksheet (max 5/term). `confirm` is required and enforced; without `true` it returns the intended effect and current plan count. |
| `unw_banner_plan_add` | `term`, `plan`, `confirm`, `crn?` or `subject`+`number` | **Writes.** Adds a section or course. `confirm` is required and enforced; refusal names the exact section and current plan state. `plan` = id or exact name; ambiguous/missing/duplicate rows are rejected without writing. |
| `unw_banner_plan_remove` | `term`, `plan`, `confirm`, `crn?` or `subject`+`number` | **Writes.** Removes one row. `confirm` is required and enforced; refusal names the row and projected credits. |
| `unw_banner_plan_delete` | `term`, `plan`, `confirm`, `confirm_name` | **Writes, irreversible.** `confirm` must be true and `confirm_name` must equal the exact plan name. Refusal reports the full intended deletion and current rows/credits. |
| `unw_register_preview` | `term`, `crns`, `refresh?=false` | **DRY RUN ONLY.** Banner's real eligibility flags/failure text plus local seat, conflict and prerequisite checks. `prereq_ok` is `true` (verified), `false` (blocked) or `null` (unknown; see `reason`). Never POSTs add/drop/register. |
| `unw_registration_window` | `term?` | **READ-ONLY.** Personal time-ticket window(s) from `term/search` eligibility prose and, when that has no window, Banner's server-rendered Prepare for Registration page. Returns America/Chicago `start`/`end`, `eligible`, `parse_status`, `source` (`banner_term`/`banner_prepare`), `standing`, `institution_hours`, `transfer_hours`, `time_ticket_text`, verbatim `raw_text`, seconds until opening and a countdown. `eligible=false` with a `start` means the ticket is assigned but the window is not open. No `term` returns the configured current term and the next selectable term. Never registers/adds/drops/withdraws. |

Section payloads include `credits_semantics` (`credits` is the display high
end, `planning_credits` is what the planner reserves, `credit_range` is the raw
range) and `part_of_term_note` (Banner-native code; `1` is full term, other
codes are session-specific and labels are not mapped). `unw_conflicts` and
`unw_plan_draft` also return `grid_legend`, mapping each grid label (including
overlap slugs such as `CYS 4245/BIB 4360`) to its CRN(s).

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
 "degraded": false,
 "state_age_human": "2.5 h",
 "cookie_count": 24,
 "earliest_cookie_expiry": "2026-11-21T18:37:55.974701+00:00",
 "read_hint": "try unw_profile/unw_terms once; read-only tools may still work",
 "write_hint": "run fix_command before any Banner plan write",
 "fix_command": "<venv python> -m eagle_flightplan.cli login --force"}
```

### `unw_profile`

```json
{"name": "REDACTED STUDENT", "id": "00000000", "classification": "Junior",
 "catalog_year": "2024-2025 (Fall 2024)",
 "catalog_years": ["202410", "202510"],
 "majors": ["Cybersecurity", "Computer Science"],
 "minors": ["Ministry", "Bible"],
 "programs": ["Computer Science-BS", "Cybersecurity-BS"],
 "advisors": [{"name": "Zderad, Jonathan A", "email": "advisor@example.edu", "source": "goalArray"},
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
   "faculty": [{"name": "Smith, James", "email": "professor@example.edu", "primary": true,
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

### Plan writes and enforced confirmation

All four write tools require `confirm: true` (the parameter is required, not
optional) and `unw_banner_plan_delete` additionally requires
`confirm_name` equal to the plan's exact name. Without them the server returns
`refused: true`, `writes_performed: []`, the exact intended effect and the
current plan state, and contacts Banner only to read.

Real refusal example — `unw_banner_plan_add(term="202620", plan="59021",
confirm=false, crn="22445")`:

```json
{"ok": false, "refused": true, "confirmation_required": true,
 "writes_performed": [],
 "intended_effect": "would add CYS 2081 CRN 22445 (Data Communications, 4 cr) to plan 59021 'Advisor Plan - Spring 2027' (currently 5 rows, 15 credits)",
 "current_state": {"plan_id": 59021, "plan_name": "Advisor Plan - Spring 2027",
                   "term": "202620", "sequence_number": 5,
                   "course_count": 5, "total_credits": 15.0},
 "required_confirmation": {"confirm": true},
 "error": "Confirmation required: would add CYS 2081 CRN 22445 … No write was performed.",
 "hint": "Show the intended effect to the user and get explicit approval before re-calling with the required confirmation."}
```

Real create refusal on a term with room — `unw_banner_plan_create(term="202630",
name="Plan D - Draft", confirm=false)`:

```json
{"ok": false, "refused": true, "confirmation_required": true,
 "writes_performed": [],
 "intended_effect": "would create a Plan Ahead worksheet 'Plan D - Draft' for term 202630 (current plans: 0/5)",
 "current_state": {"term": "202630", "plan_count": 0, "plan_limit": 5, "plans": []},
 "required_confirmation": {"confirm": true}}
```

Delete with a wrong name (real shape):

```json
{"ok": false, "refused": true, "writes_performed": [],
 "required_confirmation": {"confirm": true, "confirm_name": "Advisor Plan - Spring 2027"},
 "error": "Confirmation required: would DELETE plan 59021 'Advisor Plan - Spring 2027' … confirm_name 'Wrong Name' does not match the exact plan name …"}
```

Successful writes return a before/after diff **read back from Banner**, not
assumed. Tested shape for an add (mocked transport):

```json
{"ok": true, "term": "202620", "writes_performed": ["add_course"],
 "added": {"id": 206700, "code": "CYS 4855", "crn": "22449", "credits": 2.0, "status": "Planned"},
 "plan": {<plan with 2 rows>},
 "diff": {"action": "add_course", "plan_id": 59021, "plan_name": "Advisor Plan - Spring 2027",
          "rows_added": [{"crn": "22449", "code": "CYS 4855", …}],
          "rows_removed": [],
          "course_count_before": 1, "course_count_after": 2,
          "credits_before": 4.0, "credits_after": 6.0, "credits_delta": 2.0}}
```

Create/delete diffs also include `plan_count_before` / `plan_count_after`;
delete reports `remaining_plans` and every removed row. If Banner accepts the
write but the read-back fails, the tool returns `ok: false` with
`writes_performed` naming the action and a "do not repeat" hint.

---

## Error contract and cases

Domain failures are data, not exceptions: the tool returns `ok: false` with
`error` (`"<ExceptionType>: <message>"`), `error_type` and `hint`. MCP input
schema violations (wrong JSON type, missing required argument) are reported by
the SDK as an `isError` tool result instead.

| case | real behaviour | what to do |
| --- | --- | --- |
| expired session | `SessionExpiredError` (Banner returned HTML/guest page) | run `fix_command` from `unw_session`; never retry a write blindly |
| missing/expired credentials | `AuthError` | run `fix_command` from `unw_session`; confirm `.env` has all three values |
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

1. **Start with `unw_session`.** If `authenticated` is false but `degraded` is
   true, try one read tool (`unw_profile`/`unw_terms`) before asking for a login;
   read-only tools may still work. Only stop on `AuthError`/`SessionExpiredError`
   or before a Banner plan write, then use `fix_command`. Never ask for a
   password, TOTP code or cookie.
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
   **never attempt real registration**. Write tools enforce `confirm=true`
   (delete also requires the exact `confirm_name`); a refusal is a safe stop, not
   a prompt to retry automatically. Report the returned diff after a confirmed
   write. `unw_register_preview` is a dry run and `unw_registration_window` is
   read-only; Banner's eligibility failure text is the answer.
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
   <venv python> -m eagle_flightplan.cli login --force
   ```

   The packaged CLI drives Entra ID username → password → TOTP from `.env`
   (no human needed) and rewrites the state file. The module command works in
   editable and wheel installs.
2. Re-call `unw_session`; it should report `authenticated: true`.
3. If login still fails, check the pieces directly: `.env` exists with mode 600
   and all three credentials, `.state/unw_state.json` is fresh, Chromium is
   installed (`playwright install chromium`), and `unw_session` reports a live
   session. `ruff check .` and `pytest -q` verify the local install.
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
.venv/bin/eagle-flightplan advise --term 202620
.venv/bin/eagle-flightplan search --term 202620 --subject CYS
.venv/bin/python scripts/smoke_mcp.py
.venv/bin/python -m pytest -q
.venv/bin/ruff check .
```

`scripts/smoke_mcp.py` launches the server over real stdio, asserts the 20-tool
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
