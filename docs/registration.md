# Registration windows and sniping

## What is verified

Two Banner sources are used, in order:

1. `POST /ssb/term/search?mode=registration` returns `studentEligValid` plus
   `studentEligFailures`. This is the **eligibility** answer and, for a current or
   expired term, it also carries the window as prose.
2. `GET /ssb/prepareRegistration/prepareRegistration` is the server-rendered
   **Prepare for Registration** page. For a future term Banner still says "Term not
   eligible for registration", but this page already shows the assigned personal
   time ticket. It is a GET and never makes a term eligible.

`SSBClient.prepare_registration(term)` binds the term in registration mode, fetches
the page, and restores the previous term when practical. The HTML is parsed and
never persisted.

| term | `term/search` | prepare page | parsed result |
| --- | --- | --- | --- |
| 202610 Fall 2026 | `studentEligValid=false`, failures `"You have no Registration Time Ticket for the current time."`, `"You can register from 04/20/2026 08:30 AM to 10/28/2026 11:59 PM"` | same window | 2026-04-20 08:30 → 2026-10-28 23:59 America/Chicago, `source=banner_term` |
| 202620 Spring 2027 | `studentEligValid=false`, failure `"Term not eligible for registration"` | `"Please register within these times: 11/16/2026 08:30 AM - 03/22/2027 11:59 PM"`, `"Your class standing for registration purposes is Senior"`, `"Institution Hours: 70, Transfer Hours: 16"` | 2026-11-16 08:30 → 2027-03-22 23:59 America/Chicago, `eligible=false`, `source=banner_prepare`, `parse_status=parsed`, `standing=Senior` |
| 202630 Summer 2027 | same as 202620 | no time-ticket text yet | no window, `parse_status=not_eligible` |

`eligible=false` with a `start` is meaningful: the ticket is assigned but the window
is not open. Never promote `eligible` from a future ticket. Only Banner's
`studentEligValid` permits registration.

The myUNW portal (`https://my.unw.edu`) has only a **static tier table** on
`/pages/spring-registration-dates` (Seniors Nov 16, Juniors Nov 18, Sophomores Nov
20, Freshmen Nov 23; "Registration begins at 8:30am"). It is a cross-check, not a
personal ticket and not a submission gate.

The parser also handles "You may register from …", "Registration is closed",
"You may not register at this time", multiple date pairs (first pair wins as the
window, second as the end), and unknown text (`parse_status=unknown`, reason kept
verbatim, no crash). `eagle-flightplan windows [--term CODE] [--json]` prints all terms;
`eagle-flightplan watch --term CODE` polls and `--until-open` exits with code 10 the
moment eligibility flips, so it can be chained.

## MCP surface

The MCP server exposes this read-only as `unw_registration_window(term?)`. It
returns `parse_status`, `source` (`banner_term`/`banner_prepare`), `standing`,
`institution_hours`, `transfer_hours`, `time_ticket_text`, `start`/`end` in
America/Chicago, `opens_in_seconds`, `countdown` and Banner's verbatim `raw_text`;
`parse_status=unknown` means the text was not understood, so report the raw text.
`eligible=false` with a `start` means the ticket is assigned but not open. It never
registers, adds, drops or withdraws.

## Two-phase add/save (mapped from client code, not live-run)

The registration JS was read from the live Nuxt/Vue shell on 2026-11-11. Banner's
UI is **two-phase**, not one-click:

1. Add/CRN entry POSTs to a staging endpoint and creates a **pending** schedule
   row. This is not registration.
2. The user clicks Save. `#saveButton[data-registration-endpoint]` commits the
   batch of pending adds/drops in one call.

The literal staging and Save URLs are **server-rendered data attributes** on the
class-registration page; they are not present in any JS bundle. What is confirmed:

* the pending/commit split and the `#saveButton` data attributes,
* the add staging response shape: `{aaData, anyAuthorizationRequired, anyOLR}`
  with per-row `success`, `message`, `courseReferenceNumber`, `term`, `crnErrors`,
* the authorization/OLR popups (`.addAuthorizationPopup`, `.olrPopup`), which are
  never auto-accepted,
* `X-Synchronizer-Token` from `<meta name="synchronizerToken">` is added by the
  browser to every AJAX request; the Python client does not send it,
* `uniqueSessionId` is client-generated (`sessionStorage`/`Math.random()+Date.now()`).

`POST /ssb/registration/register` is not referenced by the captured client code
and remains unverified. No separate validate endpoint is proven; the add response
carries the per-CRN errors, and conflicts/prerequisites are checked locally.

## What is NOT verified

The Spring 2027 (202620) window opened on 2026-11-16; the Add staging endpoint
(`POST /ssb/classRegistration/addCRNRegistrationItems`) and the post-Save page were
observed live, and five sections are registered. Drop/withdraw was mapped
read-only on 2026-11-17 (DOM, client JS and a live page, with no drop submitted),
so the **request body of `submitRegistration/batch` for a drop has not yet been
exercised**. Until first use, treat the drop result as unproven unless the
post-call schedule read-back confirms the CRN is gone. The 15-minute pre-window UI
has no `#txt_crn`/`#saveButton`; it redirects to `/ssb/registration/registration`
with "Term not eligible". The live run therefore adds a term-selection step
(`select_term`) before `execute_targets`; entering the registration app requires
the landing card (`#registerLink`) and Continue (`#term-go`), not a direct GET of
`classRegistration` (which redirects to the landing page). An alternate PIN is not
required today (`enterPin: false`) but is detected and never guessed.

## Confirmed drop and withdraw (mapped live, 2026-11-17)

The live page for 202620 was inspected read-only (no action selected, no Submit):

* each committed row renders a hidden `select.actionDdl` with
  `id="action-<crn>-ddl"`, `data-property="selectedAction"`, `data-term="202620"`
  and `data-coursereferencenumber`. The only action option is
  `value="DW"`, `text="Web Dropped"`; the default is `value="null"`, `"None"`;
* the row status lives in `td[data-property="status"]` (`Registered` for the five
  committed rows);
* `#saveButton` (text `Submit`, initially disabled) carries
  `data-registration-endpoint="/StudentRegistrationSsb/ssb/classRegistration/submitRegistration"`;
* Banner's client JS appends `/batch` and POSTs
  `{"create": [], "update": [<full row with selectedAction="DW">], "destroy": []}`
  as JSON; selecting `DW` flips the row model's `recordStatus` to `Q` and enables
  Submit, and nothing is sent before Submit;
* **there is no separate withdraw action, control or endpoint.** Withdrawing is
  `DW` on every registered row plus one Submit. The tool enumerates the rows from
  a read-only `submitSummaryRegistration` read first and refuses if the page set
  differs (`schedule_changed`).

The implementation (`src/eagle_flightplan/drop.py`) drives that UI: exact
`#action-<crn>-ddl`, canonical endpoint attribute check, one Submit click, network
capture to `captures/registration-<term>-<drop|withdraw>-<ts>.jsonl`, and a
before/after `submitSummaryRegistration` read. It never guesses a direct write
endpoint. Statuses: `dropped`, `withdrawn`, `dry_run`, `not_registered`,
`drop_rejected`, `drop_confirm_required`, `drop_unknown`, `drop_unconfirmed`,
`withdraw_partial`, `withdraw_unknown`, `withdraw_unconfirmed`, `schedule_changed`, `drop_not_staged`,
`drop_not_submitted`, `registration_ui_unavailable`, `session_dead`, `run_failed`.
Only `dropped`, `withdrawn` and `dry_run` are success exits for these commands.

CLI:

```
eagle-flightplan drop --term 202620 --crn 22449                      # dry run
eagle-flightplan drop --term 202620 --crn 22449 --yes --i-understand
eagle-flightplan withdraw --term 202620                             # dry run
eagle-flightplan withdraw --term 202620 --yes --i-understand
```

Live runs also require the exact term typed on stdin (interactive) or
`--confirm-term 202620` (non-interactive). The MCP tools are
`unw_drop_course(term, crn, confirm)` and
`unw_withdraw(term, confirm, confirm_term)`; both refuse without confirmation and
return the intended effect plus the current registered load.

## Snipe modes

```
eagle-flightplan snipe --term 202620 --plan "Advisor Plan - Spring 2027" --mode window --dry-run
eagle-flightplan snipe --term 202620 --plan "Advisor Plan - Spring 2027" --mode window --live --i-understand --prewarm 30 --poll 5
eagle-flightplan snipe --term 202620 --crn 22449 --crn 23251 --mode seats --interval 60 --dry-run
```

* **Targets first.** From a plan name (its CRNs, ordered by the planner's priority
  classes: major_required → major_pool → minor_required → gen_ed_required → gen_ed_fill) or explicit
  `--crn` values. The resolved table is printed before anything happens.
* **mode=window**: reads the personal ticket (prepare page), sleeps until
  `start + offset - prewarm`, launches the browser early, polls eligibility every
  `--poll` seconds near T0, selects the term, then runs the existing two-phase
  Add/Save loop. A future ticket alone never makes the term eligible.
* **mode=seats**: polls the live term search for the target CRNs until
  `openSection` is true and `seatsAvailable > 0`; then applies the same window wait,
  term selection and execution. This is the mode that matters for scarce CS/CYBER
  sections.
* **Eligibility gate.** `studentEligValid` is the only permission to register. If
  the term has no ticket at all, the run returns `not_eligible` with Banner's exact
  text. If the ticket is future, the run waits rather than returning early.
* **Execution is browser-driven and two-phase.** Playwright opens one context and
  captures registration `unw.edu` XHR/JSON to
  `captures/registration-<term>-<live|dry|probe>-<ts>.jsonl` (assets are skipped).
  `select_term` picks the term (select2 v3 `select2-choice`/`select2-results`
  first, then v4; `#term-go`) and verifies the CRN input. Then each target is
  filled and Add is clicked (stage); the success check uses only the **new page
  text** and requires that CRN to appear in it, so an earlier "course added"
  notification cannot make an unmatched CRN look staged. After all targets Save
  is clicked once (`SAVE_BUTTON_SELECTORS`). The session is revalidated and the
  SSB client/state rebuilt after the pre-T0 sleep. Selector candidates live in
  `src/eagle_flightplan/snipe.py`; they are provisional until a real eligible run.
* **dry-run (default)** prints the known window, the target table and how many CRNs
  it would stage; it does not wait for hours. It fills CRNs only when the term is
  already eligible (no Add/Save click), and it never clicks Add or Save.
  `--dry-run` and `--live` are mutually exclusive; `--no-dry-run` requires
  `--live`. **`--probe`** only verifies the page and selector resolution.
* **Never claims registration from a stage.** `registered` is returned only from
  the post-Save page. A missing Save control returns `staged_not_committed`; an
  unclear Save result returns `save_unknown`; a page that clearly needs an
  authorization/OLR popup returns `authorization_required` (the popup is not
  accepted); unclear staging returns `stage_unknown` and never clicks Save.
* **Alternate PIN** returns `alternate_pin_required` before any Add/Save; no PIN is
  guessed.

## Stop conditions

First match wins, then the run exits and reports: `registered`,
`already_registered`, `closed`, `waitlist` (not joined unless `--allow-waitlist`),
`waitlist_joined`, `prereq`, `conflict`, `hold`, `session_dead`, `not_eligible`,
`alternate_pin_required`, `term_option_not_found`, `term_selection_failed`,
`registration_ui_unavailable`, `run_failed`, `staged_not_committed`, `save_unknown`,
`stage_unknown`, `authorization_required`, `timeout`. Only a blank/loading stage is
retried up to the per-CRN attempt cap; any other unclear stage stops immediately.

## CLI exit codes

Only `registered`, `already_registered`, `waitlist_joined`, `dry_run` and
`probe_ok` exit 0. Every other status — including `probe_unavailable`,
`term_option_not_found`, `term_selection_failed`, `registration_ui_unavailable`,
`no_seats`, `alternate_pin_required`, `run_failed`, `not_eligible`,
`session_dead`, `save_unknown` and `timeout` — exits 1.

## Hard safety rules

* Dropping/withdrawing happens only through the confirmed `drop`/`withdraw`
  commands and `unw_drop_course`/`unw_withdraw`; `snipe` still never drops.
* Never drop a CRN outside the exact requested target; a withdraw enumerates
  every registered row first and aborts if the page set differs.
* Never retry after a success, an unknown response or a rejection: one Submit per
  call, then read the schedule back.
* Never claim registration without a Save click; a staged row is not registered.
* Never auto-accept an authorization/OLR popup, an alternate PIN or Banner's
  conditional add/drop confirm prompt (`drop_confirm_required`).
* One browser context per run; no parallel attempts.
* `--live` requires both `--i-understand` **and** the exact term code typed on
  stdin (or `--confirm-term` when non-interactive). `--yes` alone is not consent.
* Dry-run is the default; a live drop prints the exact CRN/load and reads the
  schedule before and after.
* `GET /ssb/prepareRegistration/prepareRegistration` is read-only and its HTML is
  never persisted (it contains PII: student ID, holds, curriculum).
* Back off on 429/5xx (the Banner client already retries with jitter); do not
  hammer a section. This is the student's own registration, not a public race.

## Promoting to direct API calls

After one successful live run, `captures/registration-*.jsonl` will contain the
real request/response pairs for the stage call, the Save/commit call and any
validate/waitlist call. Two facts must be captured before promoting them:

* the literal `#saveButton[data-registration-endpoint]` and
  `#addCRN`/staging URLs (they are server-rendered data attributes, not in the JS),
* the `X-Synchronizer-Token` header value and the Save request
  serialization/response.

Promote only then into `banner/registration.py`: replace the UI loop with a
stage call followed by one Save call, keep the same target resolution, safety
gates and status taxonomy, and keep the browser path as a fallback. Until such a
capture exists, any guessed endpoint would be fabrication and must not be added.
