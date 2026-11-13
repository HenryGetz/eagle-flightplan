# Planning engine

Turns the degree audit + Banner catalog + Rate My Professors into a concrete,
conflict-free term plan and a multi-term roadmap.

```
audit JSON ──► catalog.requirements_from_audit ──┐
                                                ├─► planner.draft(term) ──► DraftPlan
Banner snapshots (7 terms) ──► catalog.resolve ─┘          │
RMP ratings (cached) ──► RatingInfo                       └─► planner.roadmap(terms) ──► Roadmap
Banner prereq text (cached) ──► PrereqExpression
```

## Term snapshots (`src/eagle_flightplan/banner/search.py`)

`fetch_term(term)` pages `GET /ssb/searchResults/searchResults`
(`pageOffset`/`pageMaxSize=500`) until `totalCount` is consumed. Observed:
3 requests and ~7 s per ~900-section term; the server caps a page at 500 rows.
Snapshots are cached at `.state/terms/<term>.json` (15 min TTL) and indexed in
`.state/terms/index.sqlite`.

| term | sections | requests | notes |
| --- | --- | --- | --- |
| 202520 Spring 2026 | 844 | 3 | view-only history |
| 202530 Summer 2026 | 130 | 2 | |
| 202610 Fall 2026 | 911 | 3 | student's current term |
| 202620 Spring 2027 | 845 | 3 | planning target |
| 202630 Summer 2027 | 119 | 2 | |
| 202710 Fall 2027 | 894 | 3 | **fetchable but not selectable** (`studentEligValid=false`) |
| 202720 Spring 2028 | 820 | 3 | **fetchable but not selectable** |
| 202810 Fall 2028 | 0 | 1 | catalog not published |

Total: 19 requests, ~6.1 MB of JSON on disk. `TermSnapshot` exposes
`by_course`, `by_subject`, `by_attribute`, `by_instructor`, `open_only`,
`search`, `offerings`, `instructors`, `attributes`.

## Requirements (`src/eagle_flightplan/catalog.py`)

`requirements_from_audit` flattens every non-met worksheet block into targets:

* explicit courses (`BIB 4360`, `CYS 3185`, …),
* attribute targets (`ATTRIBUTE = CCUL/NATS/SOSC/TPHI/OTEX/NTCC/BEXP/LABS`),
* subject wildcards (`DAL *`),
* credit pools ("choose 8 credits from the following") with `applied_credits`
  and `applied_count` carried over from completed/in-progress work.

`resolve` maps a requirement onto `TermSnapshot` sections, subtracts
already-applied courses, applies `open_only`, and explains empty results
("attribute TPHI has zero sections in 202620" vs "1 section exists but is not
open" vs "already completed or in progress").

## Draft algorithm (`src/eagle_flightplan/planner.py`)

Order: required major → required gen-ed → optional major electives → remaining
electives. For each requirement, candidates are ranked and the first that passes
**all** hard constraints is placed; credit pools keep placing until their
credits are met or constraints block.

Hard constraints

1. Must-take major courses that are offered are placed (ordering + priority).
2. No time overlap: `schedule.find_conflicts` (half-open intervals, linked/lab
   exemptions, TBA never clashes).
3. `max_credits` (default **16**), `avoid_days`, `earliest`/`latest` window, and
   `avoid_instructors` (a section taught only by avoided instructors is rejected).
4. Prerequisites: Banner's "Course or Test: …" text parsed into an `and`/`or`
   tree; `and` binds tighter. A course needs the prereq already completed or in
   progress in an *earlier* term. The check is tri-state: unparsable text or a
   fetch failure is `unknown`, which places the section with
   `prereq_unverified` and a warning rather than silently allowing it.
5. `required_crns`/`required_courses` (draft only) are placed before requirement
   buckets. An unavailable, excluded, conflicting, prerequisite-blocked or
   over-budget pin is reported in `pinned` and `errors`, and the draft response
   is `ok: false`; it is never dropped silently. A pin whose course is already
   completed or in progress reports `already_completed` and is a hard failure
   unless `allow_repeat=true`, which places the repeat (with a warning) and
   counts its credits normally.
6. `min_credits` tops up from remaining elective/optional candidates after the
   normal pass. If it still cannot be met, `min_credits_met` is false and a
   warning names the shortfall; no course is invented.
7. `excluded_crns`/`excluded_courses` remove candidates; `credit_overrides`
   (CRN → hours) sets the credits reserved for a variable-credit section.
   Variable-credit sections otherwise reserve the low end of their range; a
   0-low range reserves the high end (never zero) until hours are set.
   `exclude_arrangement` filters arranged candidates (4841/4855/4995), not the
   whole requirement pool.

Ranking (deterministic total order)

```
key = (unrated?, -effective_rating, -seats, -credits, tba?, start_time, crn)
effective_rating = rating × 0.85   when num_ratings < 3
```

* Priority buckets, in budget order: `major_required` → `major_pool` →
  `minor_required` → `gen_ed_required` → `gen_ed_fill` (optional minor pools are
  filler).
* Rated instructors always beat unrated ones; unrated is **not** 0.0 — the
  selection is labelled `unrated` with the RMP reason ("no RMP record").
* `avg_difficulty`, `num_ratings` and match confidence are carried on every
  selection but do not affect the order.
* Tie-breaks in order: more seats, more credits, scheduled before TBA, earlier
  start, lower CRN. No randomness anywhere.

A requirement can be satisfied by a section already placed for another
requirement (Python Programming exists under both majors); the extra ids are
recorded in `Selection.covers` and no duplicate section is added.

## Roadmap

`roadmap(terms, constraints, include_summer=...)` runs `draft` per term while
carrying forward:

* `passed` course codes (placed in earlier terms count for later prereqs),
  seeded with `constraints.assume_completed` (AP/transfer or earlier planned terms),
* per-requirement credit/count progress (so a pool filled in Spring is not
  refilled in Fall),
* requirements still open, and the terms with no published catalog (reported as
  `no_catalog`, never invented).

Every other constraint (`max_credits`, `min_credits`, `avoid_days`,
`earliest`/`latest`, `avoid_instructors`, `exclude_arrangement`, `delivery`,
`hours`) applies to **every** term; pins (`required_crns`/`required_courses`)
apply to the **first** term only, because a pin names a section in one term.
`include_summer=False` filters summer terms out of `terms`; an explicitly
requested summer term is therefore only planned with `include_summer=True`.

A term snapshot with **zero sections** is unpublished, not empty: `roadmap`
marks it `no_catalog`, plans nothing there, records it in
`terms_without_catalog`, and keeps its requirements open for the next published
term. `Planner.draft` raises `NoCatalogError` for such a term, which the MCP
surface reports as `error_type: "NoCatalog"` — never as a 0-credit plan and never
as "the courses are not offered".

### Constraint honesty

`DraftPlan.constraints_unenforced` lists day/time/delivery preferences that could
not actually be evaluated: when every placed section is unscheduled (Banner has
not published its meeting times yet) `avoid_days`/`earliest`/`latest` are
vacuous, and when no placed section reports a delivery mode `delivery` is
vacuous. The draft adds a warning and `all_selections_unscheduled`; the caller
must not report the preference as honored. `delivery_class()` reads Banner's
`instructionalMethod` first ("Online"/"Virtual" → online, "Blended"/"HyFlex" →
hybrid, everything else → in-person) and falls back to the campus ("Virtual").

`Constraints.assume_completed` merges codes into the completed/prerequisite set,
so a standalone Spring draft neither re-places a course already planned for Fall
nor reports its dependents as `prereq_blocked`.

`verify_draft(draft, snapshot)` re-checks every CRN exists, open sections have
seats, credits add up, `find_conflicts` is empty, prerequisites are verified and
every requirement was placed. A plan with unknown prerequisites or unplaced
requirements therefore reports `verified: false` (`verified_scope` says it is an
internal consistency check, not a registration guarantee). `scripts/draft.py`
fails with a non-zero exit if it does not pass.

## Real result — Spring 2027 (202620)

16 credits, 5 sections, no conflicts (verified):

| CRN | Course | Title | Instructor | RMP | Days | Time | Cr |
| --- | --- | --- | --- | --- | --- | --- | --- |
| 22201 | COS 2005-01 | Python Programming | Goetzman, Isaac | unrated | MWF | 12:45-13:50 | 4 |
| 23189 | BIB 3223-D01 | Pauline Epistles | Nam, Andrew | 2.0/5 (n=13) | TBA | TBA | 2 |
| 20737 | PHI 2016-01 | Ethics | Siekawitch, Larry | 3.7/5 (n=9) | TT | 10:50-12:30 | 4 |
| 20226 | BIB 4360-D01 | Systematic Theology | Muska, Mark | 4.0/5 (n=49) | TBA | TBA | 4 |
| 23109 | LIT 2157-01 | Contemporary Fiction | Fox, Rebecca | 4.5/5 (n=8) | MWF | 14:05-15:10 | 2 |

`COS 2005` is the only offered major requirement this term; the remaining
Cybersecurity courses are Fall-only (see matrix). Gen-ed fills were chosen by
profile rating, e.g. Fox 4.5 beat alternatives for the CCUL slot, and Muska 4.0
won the Theology slot only because the higher-rated Johnson section (4.4) is at
MWF 12:45 — the same time as COS 2005.

Not placed: CYS 4245 / CYS 4466 / CYS 3185 (not offered in Spring 2027),
Leadership (budget), Natural Science (budget), double-major electives (budget).

Full machine output: `captures/draft_202620.json` / `.md`.

## Real result — roadmap

| term | credits | courses |
| --- | --- | --- |
| Spring 2027 (202620) | 16 | COS 2005, BIB 3223, PHI 2016, BIB 4360, LIT 2157 |
| Summer 2027 (202630) | 10 | LDR 4425 (King), PSY 1005 (Hoefs), COS 4995 (Zderad) |
| Fall 2027 (202710) | 8 | CYS 4245, CYS 4466, CYS 4855 (Zderad) |
| Spring 2028 (202720) | 0 | nothing left that is offered there |
| Fall 2028 | — | catalog not published |

Final remainder: **Server Administration (CYS 3185 / MIS 3185)**. CYS 3185 is
fall-only and its single Fall 2027 section is `openSection: false` with 0 seats
(unpublished catalog row); MIS 3185 does not exist in any observed term. The
roadmap therefore carries it to the next Fall once that section opens.

## Offering matrix (condensed)

`captures/offering_matrix.md` has the full 43-target table. Pattern legend:
`every-term` = observed in both a spring and a fall; `fall-only` / `spring-only`
= observed in one season only; `insufficient_history` = a single observation (a
new course is not "fall-only"); `unknown` = never observed.

Only terms whose snapshot has **sections > 0** are used. An unpublished term
(Fall 2028 and Spring 2029 return 0 sections) would otherwise read as "the course
is not offered", which turns missing data into a false constraint; it is listed
in `terms_without_catalog` instead. A season label needs at least two
observations, and each row carries `observed_count` plus `weak_evidence`.

| target | Sp26 | Su26 | Fa26 | Sp27 | Su27 | Fa27 | Sp28 | pattern |
| --- | --- | --- | --- | --- | --- | --- | --- | --- |
| BIB 3020 | Y2 | Y1 | Y2 | Y1 | Y1 | Y2 | Y2 | every-term |
| BIB 3805 | N | N | N | Y1 | N | N | Y1 | spring-only |
| BIB 4360 | Y5 | Y2 | Y5 | Y5 | Y2 | Y5 | Y6 | every-term |
| BIB 4365 | Y1 | N | N | Y1 | N | Y1 | Y1 | every-term |
| BIB 4460 | Y1 | N | Y1 | Y1 | N | Y1 | Y1 | every-term |
| BIB 4825 | N | N | Y1 | N | N | Y1 | N | fall-only |
| COS 2005 | Y2 | N | Y1 | Y2 | N | Y1 | Y2 | every-term |
| COS 2211 | N | N | N | N | N | N | N | unknown |
| COS 2212 | N | N | N | N | N | N | N | unknown |
| COS 4841 | Y2 | N | Y1 | Y1 | N | Y1 | Y1 | every-term |
| COS 4855 | Y1 | Y1 | Y1 | Y1 | N | Y1 | Y1 | every-term |
| COS 4995 | Y1 | Y1 | Y1 | Y1 | Y1 | Y1 | Y1 | every-term |
| CYS 3185 | N | N | N | N | N | Y1 | N | fall-only |
| CYS 4245 | N | N | N | N | N | Y1 | N | fall-only |
| CYS 4369 | N | N | Y1 | N | N | N | N | fall-only |
| CYS 4465 | N | N | Y2 | N | N | N | N | fall-only |
| CYS 4466 | N | N | N | N | N | Y1 | N | fall-only |
| CYS 4855 | Y1 | N | Y1 | Y1 | N | Y1 | Y1 | every-term |
| LDR 4425 | Y6 | Y1 | Y5 | Y4 | Y1 | Y5 | Y6 | every-term |
| LDR 4825 | Y1 | N | N | Y1 | N | N | Y1 | spring-only |
| MAT 2055 | Y4 | Y1 | Y5 | Y4 | Y1 | Y5 | Y4 | every-term |
| DAL * | Y5 | N | Y4 | Y5 | N | Y3 | Y3 | every-term |
| attr CCUL | Y21 | Y4 | Y22 | Y26 | Y4 | Y26 | Y24 | every-term |
| attr TPHI | Y14 | Y3 | Y15 | Y13 | Y4 | Y15 | Y14 | every-term |
| attr NATS | Y14 | Y1 | Y19 | Y16 | Y1 | Y35 | Y16 | every-term |
| attr SOSC | Y45 | Y4 | Y37 | Y40 | Y5 | Y42 | Y36 | every-term |
| MIS 3185 / 4245 | N | N | N | N | N | N | N | unknown |

Consequences for this student: CYS 4245, CYS 4466 and CYS 3185 can only ever be
taken in a fall; CYS 4369/4465 are already in progress (Fall 2026). Spring 2027
can only advance Python (COS 2005) plus gen-ed and electives.

## Data that contradicts the audit

* The audit still lists **COS 2211 / COS 2212** as options for "Python
  Programming" (both "choose 4 credits"); neither course exists in any of the
  seven terms. The planner resolves the requirement through **COS 2005**.
* The audit lists **CYS 3185 / MIS 3185** ("Server Administration"), **CYS 4245 /
  MIS 4245** and **CYS 4466**; the MIS aliases never appear and the CYS sections
  only exist in the Fall 2027 draft catalog (with `openSection: false` for 3185).
* The Bible minor repeats the Theology / Theological-Philosophy blocks that the
  gen-ed core already requires; the planner places one section and credits both.

## Known limitations

* **Prerequisite parsing is heuristic.** Banner emits `( Course or Test: … )
  and/or ( … )`; grades ("Minimum Grade of C-"), "May not be taken
  concurrently" and test/placement clauses are ignored. A missing prereq blocks
  the pick; a parsing miss would allow it.
* **Part-of-term is not modelled in conflicts.** Meetings carry
  `startDate`/`endDate`, but `schedule` compares weekday+time only; two
  half-term courses that never overlap in calendar weeks would still be flagged.
* **Seats are a snapshot** (15-min TTL). The draft reports `seats_available` at
  fetch time; it does not re-check live seats.
* **Seven terms of history** and only one Fall observation for the newest
  Cybersecurity courses; "fall-only" can mean "alternate-year".
* **Future catalogs are drafts.** 202710/202720 are searchable but not
  registration-eligible; sections may be closed (`openSection: false`), lack
  instructors (Fall 2027 CYS 4245/4466 show "Faculty, Unassigned") or have no
  meeting time at all. Time-of-day preferences are unenforceable on such rows and
  are reported in `constraints_unenforced` instead of silently passing.
* **The draft is a plan, not a registration action.** Nothing is written to the
  Banner planner from this module (see `banner/plan.py` for that API).
* RMP lookups include a polite delay and are cached in `.state/ratings.json`
  (7-day TTL). Names with no RMP record are reported as `unrated`, never 0.0.

## Reproduce

```bash
# full term snapshots (first run ~20 s; cached afterwards)
python scripts/draft.py --matrix

# Spring 2027 draft + captures/draft_202620.{json,md}
python scripts/draft.py --term 202620

# multi-term roadmap + captures/roadmap.md
python scripts/draft.py --roadmap

# tighter budget
python scripts/draft.py --term 202620 --max-credits 12

# tests (offline, < 5 s)
python -m pytest tests/test_planner_*.py tests/test_catalog_*.py -q
```
