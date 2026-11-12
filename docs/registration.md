# Registration windows and sniping

## What is verified

`POST /ssb/term/search?mode=registration` is the only observed source of time-ticket
information. It returns `studentEligValid` plus `studentEligFailures`, and the
window is prose:

| term | observed payload | parsed result |
| --- | --- | --- |
| 202610 Fall 2026 | `studentEligValid=false`, failures `"You have no Registration Time Ticket for the current time."`, `"You can register from 04/20/2026 08:30 AM to 10/28/2026 11:59 PM"` | window 2026-04-20 08:30 → 2026-10-28 23:59 America/Chicago (CDT/CST aware) |
| 202620 Spring 2027 | `studentEligValid=false`, failure `"Term not eligible for registration"` | no window, `parse_status=not_eligible` |
| 202630 Summer 2027 | same as 202620 | no window |

The parser also handles "You may register from …", "Registration is closed",
"You may not register at this time", multiple date pairs (first pair wins as the
window, second as the end), and unknown text (`parse_status=unknown`, reason kept
verbatim, no crash). `eagle-flightplan windows [--term CODE] [--json]` prints all terms;
`eagle-flightplan watch --term CODE` polls and `--until-open` exits with code 10 the
moment eligibility flips, so it can be chained.

## MCP surface

The MCP server exposes this read-only as `unw_registration_window(term?)`. It
returns `parse_status`, `start`/`end` in America/Chicago, `opens_in_seconds`,
`countdown` and Banner's verbatim `raw_text`; `parse_status=unknown` means the
text was not understood, so report the raw text. It never registers, adds, drops
or withdraws.

## Two-phase add/save (mapped from client code, not live-run)

The registration JS was read from the live Nuxt/Vue shell on 2026-11-11. Banner's
UI is **two-phase**, not one-click:

1. Add/CRN entry POSTs to a staging endpoint and creates a **pending** schedule
   row. This is not registration.
2. The user clicks Save. `#saveButton[data-registration-endpoint]` commits the
   batch of pending adds/drops in one call.

The literal staging and Save URLs are **server-rendered data attributes** on the
class-registration page; they are not present in any JS bundle. The page itself
still redirects to `/ssb/registration/registration` for this account ("Term not
eligible for registration"), so the exact URLs, request bodies, and Save response
remain unverified. What is confirmed:

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

No term has been registration-eligible for this account since the project began,
so the add/validate/drop **literal** endpoints and request bodies have never been
exercised. For 202620 the class-registration page is unreachable: `snipe --probe`
lands on `/ssb/registration/registration` with the notification "Term not eligible
for registration" and the CRN input/Add button do not exist (`crn_selector=null`,
`add_selector=null`).

## Snipe modes

```
eagle-flightplan snipe --term 202620 --plan "Advisor Plan - Spring 2027" --mode window --dry-run
eagle-flightplan snipe --term 202620 --crn 22449 --crn 23251 --mode seats --interval 60 --dry-run
```

* **Targets first.** From a plan name (its CRNs, ordered by the planner's priority
  classes: major_required → major_pool → gen_ed_required → gen_ed_fill) or explicit
  `--crn` values. The resolved table is printed before anything happens.
* **mode=window**: waits until the parsed window `start + offset`, then polls
  eligibility; executes the moment `studentEligValid` is true.
* **mode=seats**: polls the live term search for the target CRNs until
  `openSection` is true and `seatsAvailable > 0`, then executes. This is the mode
  that matters for scarce CS/CYBER sections.
* **Eligibility preflight.** Every non-probe run binds the term with
  `POST /ssb/term/search?mode=registration` before opening the registration UI. If
  `studentEligValid` is false it returns `not_eligible` with Banner's exact
  `studentEligFailures` text and does not open the UI. In `seats` mode the seat
  wait runs first, then the registration binding. `--probe` skips this gate.
* **Execution is browser-driven and two-phase.** Playwright opens one context and
  captures every `unw.edu` response to
  `captures/registration-<term>-<live|dry|probe>-<ts>.jsonl`. For each target it
  fills the CRN and clicks Add (stage). After all targets it clicks Save once
  (`SAVE_BUTTON_SELECTORS`). Selector candidates live in
  `src/eagle_flightplan/snipe.py`; they are provisional until a real eligible run
  confirms them.
* **dry-run (default)** fills every target CRN, clicks neither Add nor Save, and
  reports "would stage N CRN(s) and then click Save once". **`--probe`** only
  verifies the page and selector resolution. Neither submits anything.
* **Never claims registration from a stage.** `registered` is returned only from
  the post-Save page. A missing Save control returns `staged_not_committed`; an
  unclear Save result returns `save_unknown`; a page that clearly needs an
  authorization/OLR popup returns `authorization_required` (the popup is not
  accepted); unclear staging returns `stage_unknown` and never clicks Save.

## Stop conditions

First match wins, then the run exits and reports: `registered`,
`already_registered`, `closed`, `waitlist` (not joined unless `--allow-waitlist`),
`waitlist_joined`, `prereq`, `conflict`, `hold`, `session_dead`, `not_eligible`,
`staged_not_committed`, `save_unknown`, `stage_unknown`, `authorization_required`,
`timeout`. Only a blank/loading stage is retried up to the per-CRN attempt cap;
any other unclear stage stops immediately.

## Hard safety rules

* Never drop or withdraw (no such calls exist in the code path).
* Never retry after a success: the run returns immediately.
* Never claim registration without a Save click; a staged row is not registered.
* Never auto-accept an authorization/OLR popup.
* Never register a CRN outside the resolved target list.
* One browser context per run; no parallel attempts.
* `--live` requires both `--i-understand` **and** the exact term code typed on
  stdin. `--yes` is not accepted as consent.
* Dry-run is the default; a live run prints the exact target table and the term's
  eligibility verdict before starting.
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
