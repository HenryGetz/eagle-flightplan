# Banner 9 SSB API map (UNW, verified 2026-11-11)

Host: `https://registration-ss.oci.unw.edu`
Base: `/StudentRegistrationSsb/ssb`
UI framework: Banner Self-Service 9.41.1. Everything below was executed against
the live student account with the replayable cookie jar from `docs/login.md`.
No registration add/drop/register/withdraw POST was ever sent.

## Session, headers, cookies

Required headers on every XHR (all replayed automatically by
`unw_planner.auth.SSB_HEADERS`):

```
Accept: application/json, text/javascript, */*; q=0.01
X-Requested-With: XMLHttpRequest
Referer: https://registration-ss.oci.unw.edu/StudentRegistrationSsb/ssb/registration
Origin:  https://registration-ss.oci.unw.edu
Content-Type: application/json            (batch writes only)
Content-Type: application/x-www-form-urlencoded; charset=UTF-8   (form writes)
```

Cookies that matter: `JSESSIONID` (registration host), `X-Oracle-BMC-LBS-registration-ss`,
`JSESSIONID` + `X-Oracle-BMC-LBS-banner-ss` (banner host). There is **no CSRF
token header**: each HTML page embeds `<meta name="synchronizerToken">`, but no
endpoint exercised here required it. `X-Synchronizer-Token` is not sent by the UI.

**The term must be bound to the session first.** Plan pages in particular render
an empty list without it.

## Terms

`GET /ssb/classSearch/getTerms?searchTerm=&offset=1&max=50` (also
`/ssb/classRegistration/getTerms`) → JSON array:

```json
[{"code": "202630", "description": "Summer 2027"},
 {"code": "202620", "description": "Spring 2027"},
 {"code": "202610", "description": "Fall 2026"},
 {"code": "202530", "description": "Summer 2026 (View Only)"},
 {"code": "202520", "description": "Spring 2026 (View Only)"}]
```

Codes are `YYYY` + season (`10` fall, `20` spring, `30` summer) + `0`, i.e.
`YYYY+10` for fall as in `UNW_Fall_2026_202610_UTC.ics`.

| term | code | note |
| --- | --- | --- |
| Fall 2026 (current) | `202610` | registration time ticket expired 10/28/2026 |
| **Spring 2027 (next)** | **`202620`** | bookable in the planner now |
| Summer 2027 | `202630` | planner allows it |
| Summer 2026 / Spring 2026 | `202530` / `202520` | view only |

`POST /ssb/term/search?mode=<search|plan|registration|preReg>` with form body
`term=202620&studyPath=&studyPathText=&startDatepicker=&endDatepicker=`

* `mode=search` → `{"fwdURL": "/StudentRegistrationSsb/ssb/classSearch/classSearch"}`
* `mode=plan` → `{"fwdURL": ".../plan/selectPlan"}`
* `mode=registration`, ineligible term → HTTP 200 with
  ```json
  {"regAllowed": false, "studentEligValid": false,
   "studentEligFailures": ["Term not eligible for registration"],
   "fwdURL": ".../classRegistration/classRegistration"}
  ```
* `mode=registration`, current term, expired ticket →
  ```json
  {"studentEligValid": false,
   "studentEligFailures": ["You have no Registration Time Ticket for the current time.",
                           "You can register from 04/20/2026 08:30 AM to 10/28/2026 11:59 PM"]}
  ```

`POST /ssb/term/saveTerm?mode=<mode>` persists the same selection.

## Class search

`GET /ssb/searchResults/searchResults`

| param | meaning |
| --- | --- |
| `txt_term` | term code (required) |
| `txt_subject` | subject code, e.g. `CYS` (empty = all) |
| `txt_courseNumber` | course number, e.g. `2081` |
| `pageOffset`, `pageMaxSize` | pagination (`0`, `50`) |
| `sortColumn`, `sortDirection` | e.g. `subjectDescription`, `asc` |
| `uniqueSessionId` | optional client id; the UI sends one |

Response envelope (`SearchPage`):

```json
{"success": true, "totalCount": 2, "pageOffset": 0, "pageMaxSize": 10,
 "sectionsFetchedCount": 2, "pathMode": "search",
 "data": [ { ...section... } ]}
```

**Filters that do NOT work server-side** (verified: identical `totalCount` with
and without them, e.g. 845 total sections for Spring 2027): `txt_level`,
`txt_campus`, `txt_instructor`, `txt_instructionalMethod`, `txt_attribute`,
`txt_college`, `txt_department`, `txt_scheduleType`, `txt_credithourlow/high`,
`chk_open_only`. The UI sends them, Banner ignores them on this deployment.
Filter client-side on `openSection`, `faculty[].displayName`,
`instructionalMethod`, `sectionAttributes[].code`, `creditHourLow`.

One search-result row (real, CRN 22445):

```json
{"id": 86262, "term": "202620", "termDesc": "Spring 2027",
 "courseReferenceNumber": "22445", "partOfTerm": "1",
 "courseNumber": "2081", "courseDisplay": "2081", "subject": "CYS",
 "subjectDescription": "CYS: Cyber Security", "sequenceNumber": "01",
 "campusDescription": "Main Campus", "scheduleTypeDescription": "Lecture",
 "courseTitle": "Data Communications", "creditHours": null,
 "maximumEnrollment": 30, "enrollment": 0, "seatsAvailable": 30,
 "waitCapacity": 5, "waitCount": 0, "waitAvailable": 5,
 "creditHourLow": 4, "creditHourHigh": null, "creditHourIndicator": null,
 "openSection": true, "linkIdentifier": null, "isSectionLinked": false,
 "subjectCourse": "CYS2081",
 "faculty": [{"bannerId": "9565", "displayName": "Smith, James",
              "emailAddress": "professor@example.edu", "primaryIndicator": true,
              "term": "202620"}],
 "meetingsFaculty": [{"category": "01", "courseReferenceNumber": "22445",
   "meetingTime": {"beginTime": "1245", "endTime": "1425",
     "building": "RIL", "buildingDescription": "Riley", "room": "R1332",
     "campus": "M", "campusDescription": "Main Campus",
     "meetingType": "CLAS", "meetingTypeDescription": "Class",
     "meetingScheduleType": "LC", "creditHourSession": 4.0, "hoursWeek": 3.33,
     "startDate": "01/11/2027", "endDate": "05/13/2027",
     "monday": false, "tuesday": true, "wednesday": false,
     "thursday": true, "friday": false, "saturday": false, "sunday": false,
     "term": "202620"}, "faculty": []}],
 "sectionAttributes": [{"code": "CDMS", "description": "Computing Data Math Sci Dept"},
                       {"code": "PSCO", "description": "PSEO Eligible Course"},
                       {"code": "TRBK", "description": "Eligble for TRAD block tuition"}],
 "instructionalMethod": "AE", "instructionalMethodDescription": "Activity/Experience"}
```

Seat fields: `seatsAvailable` / `maximumEnrollment` / `enrollment`,
`waitAvailable` / `waitCapacity` / `waitCount`, `openSection`.
Instructor: `faculty[].displayName`, `faculty[].emailAddress`,
`faculty[].primaryIndicator`. Meeting times: `meetingsFaculty[].meetingTime`.

### Meeting-time representation (used by `unw_planner.schedule`)

* `beginTime` / `endTime` are **`HHMM` 24-hour strings in campus-local wall
  clock** (`"1245"` = 12:45 PM, `"0800"` = 8:00 AM). No timezone is attached.
  UNW is `America/Chicago` (CST/CDT depending on the date).
* Days are seven booleans `monday` … `sunday`.
* `building` (code) + `buildingDescription` (name) + `room`.
* `meetingType` (`CLAS`, `LAB`, …) and `meetingTypeDescription` ("Class", "Lab").
* `meetingScheduleType` (`LC` lecture, `LL` lab, …), `creditHourSession`,
  `hoursWeek`, `startDate`/`endDate` (`MM/DD/YYYY`), `term`, `category`.
* **TBA / asynchronous** sections return `beginTime: null, endTime: null` (all
  day flags false). Example: CRN `22891`, CYS 2081-D01, Spring 2027. There is no
  "TBA" string here; `null` is the encoding. (A literal `TBA`/`ARR` string is
  accepted by `parse_banner_time` defensively.)
* 12-hour values only appear in the UI, never in JSON.
* `partOfTerm` is a short code (`"1"`), `sessionDescription` is not present in
  this payload; use the part-of-term code plus `startDate`/`endDate`.
* `campusDescription` and `instructionalMethodDescription` are top-level section
  fields, not per-meeting.

Verified against the student's own ICS export:
`Computer Firewalls & Testing (CYS 4465)` → Banner `1645`–`1800`, Tuesday,
`RIL R1332`, term `202610`. The ICS event is `DTSTART:20260901T214500Z` /
`DTEND:20260901T230000Z`, Tuesday — i.e. 16:45–18:00 **CDT** (UTC−5) in
September, becoming `22:45Z` in November. So Banner times are local wall clock
and the ICS export is the UTC-converted view. No 12h encoding, no ambiguity.

### Section detail endpoints

All take `term` + `courseReferenceNumber` (the parameter is **not** `crn`; using
`crn` yields HTTP 500 and can invalidate the session).

| endpoint | returns |
| --- | --- |
| `GET /ssb/searchResults/getFacultyMeetingTimes` | JSON `{"fmt": [ {faculty: [...], meetingTime: {...}} ]}` |
| `GET /ssb/searchResults/getEnrollmentInfo` | HTML fragment (actual/max/available seats, waitlist) |
| `GET /ssb/searchResults/getSectionAttributes` | HTML fragment (`description CODE`) |
| `GET /ssb/searchResults/getSectionPrerequisites` | HTML fragment (`Catalog Prerequisites`, `<pre>` text) |
| `GET /ssb/searchResults/getCorequisites` | HTML fragment |
| `GET /ssb/searchResults/getRestrictions` | HTML fragment (levels/colleges/programs) |
| `GET /ssb/searchResults/getCourseDescription` | HTML fragment (catalog text incl. `(PQ: ...)` line) |
| `GET /ssb/searchResults/getClassDetails` | HTML fragment (term, CRN, campus, section, method) |
| `GET /ssb/searchResults/fetchLinkedSections` | JSON `{"linkedData": []}` |
| `GET /ssb/searchResults/getSectionBookstoreDetails` | HTML fragment (bookstore links) |
| `GET /ssb/courseSearchResults/*` | catalogue-side equivalents (`getPrerequisites`, `getCorequisites`, `getRestrictions`, `getCourseAttributes`, `getCourseCatalogDetails`, `getCourseDescription`) |

There is no single "section detail JSON": meeting times and linked sections are
JSON, everything else is a server-rendered fragment. `SSBClient.section_detail()`
fetches all of them and flattens the HTML to text (`SectionDetail`).

Course-level (no section) search for catalog pages:
`GET /ssb/courseSearchResults/courseSearchResults` with the same `txt_*` params.

### Filter lookups (select2 data)

`GET /ssb/courseSearch/get_subject|get_level|get_scheduleType|get_attribute|get_college|get_department`
with `searchTerm=&offset=1&max=20` (optionally `term=202620`) → `[{code, description}]`.
`get_level` returns `UG`/`GR`; `get_college` `FC`/`DE`/`GS`/`TR`.

## Planner ("Plan Ahead")

Pages: `GET /ssb/plan/selectPlan` (list), `GET /ssb/plan/plan` (draft editor),
`GET /ssb/plan/plan?select=<sequenceNumber>` (open an existing plan).

There is **no JSON list endpoint**. The list is a strict-JSON block inside the
page bootstrap:

```html
<script>
window.bootstraps = {
  plans: [ { "id": 58980, "description": "Plan A - CS/CYBR priority",
             "term": "202620", "sequenceNumber": 1, "preferredIndicator": true,
             "planCourses": [ {...} ], "totalPlannedCreditHours": 4.0, ... } ],
  ...
};
</script>
```

`sequenceNumber` is the per-term 1-based ordinal, distinct from the database
`id`. The editor URL uses the sequence number, the batch API uses the id.

A student may hold **5 plans per term** (page text "You are allowed a maximum of
5 plans for this term"; `maxPlans: 5` in the delete response), which is the budget
for writing alternative plans ("Plan A - CS/CYBR priority", etc.). One plan is
`preferredIndicator: true`; that is the one the UI opens by default.

| operation | request |
| --- | --- |
| bind term | `POST /term/search?mode=plan` (`term=202620&studyPath=&...`) |
| list plans | `GET /plan/selectPlan` → parse `plans:` bootstrap |
| open a plan | `GET /plan/plan?select=<sequenceNumber>` (session-side effect; verify the bootstrap's `selectedPlan.planHeader.id`) |
| draft editor | `GET /plan/plan` (creates an untitled draft in the session) |
| **create plan** | `POST /plan/submitPlan/batch` JSON `{"create":[{"headerDescription":"Plan A - CS/CYBR priority","headerComment":""}],"update":[],"destroy":[],"uniqueSessionId":"<5 alnum + epoch ms>"}` → `data.planHeader.id` |
| **add course** | `POST /plan/addPlanItem` form `subject=CYS&courseNumber=2081&course=course` **or** `term=202620&courseReferenceNumber=22445&course=section` → `{"success":true,"model":{...tuid...}}`, then `POST /plan/submitPlan/batch` JSON `{"create":[<that model>],...}` |
| **remove course** | `POST /plan/submitPlan/batch` JSON `{"update":[<planCourse with selectedPlanAction:"Delete", isDeleteAction:true>],...}` |
| **delete plan** | `POST /plan/delete` form `delete=<sequenceNumber as integer>` → `{"success":true,"data":[],"message":"Plan successfully deleted.","planCount":0,"maxPlans":5}` |
| read selected plan | `POST /plan/submitPlan/batch` JSON with empty `create`/`update`/`destroy` → `data.planHeader` (id, name, term, `planCourses[]`) |
| plan meeting times | `GET /plan/getMeetingInformationForPlan?planId=&term=` → `[]` when the plan has no dated courses |
| plan events | `GET /plan/getPlanEvents?planId=&term=` → `[]` |
| preference | `POST /plan/makePreferred` (UI link "Make Preferred") |
| other | `plan/addAllPlannedLinkedSections`, `plan/savePlanNote`, `plan/finishOLRPopup`, `plan/getOLRStartDate`, `plan/getOLREndDate`, `plan/getOLRRegistrationItem` |

### Idempotency and error behaviour (measured)

* **Duplicates are allowed.** Adding the same CRN twice produced two rows
  (`206667`, `206668`) with the same course data. There is no dedupe and no
  error. `PlanClient.add_course` refuses duplicates client-side
  (`DuplicateCourseError`) unless `allow_duplicate=True`.
* Adding the same *course* (subject+number) and the same *section* (CRN) are
  different rows: `courseReferenceNumber` is `null` for a course-level entry.
* `addPlanItem` without a usable identifier answers HTTP 200 with
  `{"success": false, "message": "Subject And Course Number or Course Reference Number are required fields."}`
* `destroy` in the batch payload is a no-op for plan courses (HTTP 200,
  `"message": "Successfully registered."`, row still present). Removal must use
  `update` + `selectedPlanAction: "Delete"`.
* Renaming a plan by updating `description`/`headerDescription` in the batch
  either has no effect or returns HTTP 500 (which also kills the SSB session).
  The UI exposes no working rename; `PlanClient` intentionally does not offer one.
* Opening `GET /plan/plan?select=1` when the student has **no** plan returns an
  error page (67 KB, no `selectedPlan`), and the next batch write 500s. Always
  open the bare `/plan/plan` draft before creating the first plan.
* Plans are term-scoped (`planHeader.term`), and every `planCourse` carries its
  own `term`. Moving a course between terms would be an `update` with a new
  `term`; this was not exercised (the Spring 2027 section used is not offered in
  Summer 2027), so treat cross-term moves as unverified.

### Worked example: create → add → verify → cleanup

```
POST /plan/submitPlan/batch
{"create":[{"headerDescription":"ZZ API TEST - DELETE ME","headerComment":""}],
 "update":[],"destroy":[],"uniqueSessionId":"abcde1794420000000"}
  -> data.planHeader.id = 58991, sequenceNumber = 2

POST /plan/addPlanItem        term=202620&courseReferenceNumber=22445&course=section
  -> {"success":true,"model":{"subject":"CYS","courseNumber":"2081",
       "courseReferenceNumber":"22445","courseTitle":"Data Communications",
       "creditHours":4.0,"planStatus":"Pending","tuid":-1767971968,...}}

POST /plan/submitPlan/batch   {"create":[<model>],...}
  -> planCourses: [{"id":206722,"subject":"CYS","courseNumber":"2081",
                    "courseReferenceNumber":"22445","planStatus":"Planned", ...}]

POST /plan/submitPlan/batch   {"create":[],"update":[],"destroy":[],...}
  -> planHeader with that row (read-back check)

POST /plan/submitPlan/batch   {"update":[<row + "selectedPlanAction":"Delete",
                                          "isDeleteAction":true>],...}
  -> planCourses: []

POST /plan/delete             delete=2
  -> {"success":true,"message":"Plan successfully deleted."}
```

`src/unw_planner/banner/plan.py` implements exactly this sequence and was run
end-to-end against the live account (plan created, CRN 22445 added, read back,
duplicate rejected locally, removed, plan deleted; the student's other plans were
never touched — there were none).

## Registration page (blocked; two-phase flow mapped from client code)

`POST /term/search?mode=registration` for `202610` returns
`studentEligValid: false` with `"You have no Registration Time Ticket for the
current time."`, and for `202620` `"Term not eligible for registration"`. The
server then redirects `/classRegistration/classRegistration` to the public
registration landing page, so the add/drop UI itself could not be exercised.

The client code was still read from the live JS bundles on 2026-11-11. It proves
the write path is **two-phase**, not one-click:

1. **Stage**: the Add/CRN control POSTs to a server-rendered staging endpoint and
   creates a *pending* schedule row. Response: `{aaData, anyAuthorizationRequired,
   anyOLR}`; each `aaData` row has `success`, `message`,
   `courseReferenceNumber`, `term`, `crnErrors`.
2. **Commit**: the schedule collection's URL is switched to
   `#saveButton[data-registration-endpoint]` and a Save click commits the pending
   adds/drops. The JS proves the collection sync but not the HTTP verb; a POST is
   the likely Backbone default and is **inferred, not proven**. The literal URL,
   request body and response are **not** in the JS; they are server-rendered
   attributes on the blocked page. `#saveButton[data-summary-endpoint]` handles
   credit-hour and study-path edits.

The term-selection page yields two exact endpoints and one exact body:

| method | endpoint | fields |
| --- | --- | --- |
| GET | `/ssb/classRegistration/getTerms` | `searchTerm`, `offset`, `max` → `{code, description}[]` |
| GET | `/ssb/term/saveTerm?mode=registration` | `{term, uniqueSessionId}` or `{startDatepicker, endDatepicker, uniqueSessionId}` → `{studyPath, maxterm}` |
| POST | `/ssb/term/search?mode=registration` | `term`, `studyPath`, `studyPathText`, `student`, `altPin`, `stu_pin`, `holdPassword`, `startDatepicker`, `endDatepicker`, `uniqueSessionId` |

The browser also adds `X-Synchronizer-Token: <meta name="synchronizerToken">` to
every AJAX request via `$.ajaxPrefilter`; the Python client does not send it.
`uniqueSessionId` is a client-generated value, not a security token.

Consequences, stated plainly:

* The UI's registration write path is **partially mapped**: the stage/commit
  split, the add response shape, and the term-selection bodies are known from
  client code; the literal stage/Save URLs, Save body, and Save response are
  server-rendered and remain **unverified**.
* `POST /ssb/registration/register` (the classic Banner registration write) is
  **unverified and is not referenced by the captured client code**; do not assume
  its body or response shape from this document.
* No separate validate endpoint is proven. The add response carries per-CRN
  `success`/`message`/`crnErrors`; because the planner is fully mapped, the safe
  substitute is: resolve sections via the planner/search endpoints and run
  `unw_planner.schedule.find_conflicts()` locally — it uses the same meeting-time
  data the registration engine uses and handles the linked lecture/lab exemption.

Endpoints visible in the registration markup:

* `GET /ssb/classRegistration/getTerms` — confirmed from the term-selection page.
* `GET /ssb/registrationHistory/registrationHistory` — confirmed landing link.
* `GET /ssb/registration/registerPostSignIn` — this is the unauthenticated
  `data-login` redirect, **not** a write endpoint.
* `GET /ssb/classRegistration/getRegistrationItems` — listed in earlier notes but
  not found in the pages fetched on 2026-11-11; still unverified.
* `#getRegistrationEvents[data-endpoint]` (GET `{termFilter, crn?}`),
  `#getSectionCatalogDetails[data-endpoint]`, and
  `#getCourseCatalogDetails[data-endpoint]` — read-only UI helpers; the last two
  resolve to `/ssb/searchResults/getClassDetails` and
  `/ssb/courseSearchResults/getCourseCatalogDetails` on the pages where they were
  captured.

## Error shapes

| situation | shape |
| --- | --- |
| session expired | HTTP 200 with `text/html` (the 61 KB guest shell) or 302 to `login.microsoftonline.com`; `meta fullName` empty |
| term not selected | plan list renders empty; no JSON error |
| term ineligible | `{"studentEligValid": false, "studentEligFailures": [...]}` |
| bad parameter (`crn` instead of `courseReferenceNumber`) | HTTP 500 HTML error page, and the SSB session is dropped |
| duplicate plan course | no error — a second row is created |
| batch write failure | HTTP 500 HTML, session dropped; treat as "re-login and re-read the plan" |

`probe.py` result: **pure httpx replay works**. `scripts/probe.py` replays 24
cookies with no browser and successfully calls `getTerms`, binds term `202620`,
runs a CYS 2081 class search (2 sections, seats/days/times/instructors), and
lists planner plans.

## Schedule conflict engine (`unw_planner.schedule`)

Pure, no I/O; fed by `Section.to_schedule_section()` /
`SectionDetail.to_schedule_section()`.

```python
from unw_planner.schedule import find_conflicts, render_grid, free_blocks, conflict_summary

sections = [section.to_schedule_section() for section in page.sections]
conflict_summary(find_conflicts(sections))      # "" conflicts -> "No conflicts."
print(render_grid(sections))                    # ASCII Mon..Sun grid + TBA footer
free_blocks(sections, "Wed", time(8), time(15)) # gaps in a window
```

* Intervals are half-open `[start, end)`: 10:00–11:00 and 11:00–12:00 do not clash.
* A section never conflicts with itself; sections sharing a non-empty
  `linkIdentifier` are exempt (cross-listed lecture/lab); so is a lecture + lab
  pair of the same `subject`+`number`+`term` (Banner's "Class"/"Lecture" vs
  "Lab" meeting types).
* `beginTime`/`endTime` null (TBA, async, online) ⇒ `unscheduled=True`, never a
  time conflict. `strictness="strict"` reports those as `kind="tba"` pairings;
  `include_tba=True` keeps them in `render_grid`'s footer listing.
* `Conflict` carries `day`, `start`, `end`, `minutes`, both CRNs and both full
  titles, and `.summary` renders
  `Wed 10:30-11:00 (30 min): CRN 12345 CYS 2081 (Data Communications) overlaps CRN 67890 ...`.

## Capture artifacts

| file | contents |
| --- | --- |
| `captures/ssb.jsonl` | merged `page.on("request"/"response")` transcript of every SSB UI walk (term select, plan list/create/add/save/remove/select, class-search with filters), each record tagged with its `source` walk |
| `captures/classsearch.html` / `term.html` / `plan.html` | authenticated page markup used to mine `data-endpoint` attributes |
| `captures/section_22445_details.json` | raw responses of all nine section-detail endpoints for CRN 22445 |
| `captures/search_CYS_202620.json` | full class-search payload (6 sections) |
| `captures/plan-*.jsonl` | per-walk request/response transcripts including the plan batch bodies |

## Plan page semantics (verified live 2026-11-11)

**Plans are term-scoped worksheets.** `planHeader.term` is the plan's term; the
selectPlan page counts and lists only plans for the *session's bound term*
("Plans you have created for this term: N"). A student whose session is bound to
Fall 2026 sees only Fall 2026 plans even when Spring 2027 plans exist — this is
the usual reason a plan looks "missing". Each `planCourse` also carries a `term`
field, but the worksheet itself is single-term: the editor bootstrap exposes one
`terms: 202620` value and there is no UI for mixing terms inside one plan.

The list itself is rendered **server-side** into the selectPlan HTML (plan names,
course rows and Delete/Edit controls are in the markup). A JSON `plans:` block is
present only on some loads, so scraping the visible rows is the reliable check.
Term binding happens through `POST /ssb/term/search?mode=plan`, and the human path
is `Plan Ahead -> Select a Term -> choose Spring 2027 -> Continue`; the dropdown
is the select2 widget on `/ssb/term/termSelection?mode=plan`.

`POST /ssb/plan/makePreferred` takes the plan's **sequenceNumber**, not its
database id (`preferred=<seq>`); it marks the plan preferred and makes it the
editor default but does not change which term's list is shown. Opening a plan is
`GET /ssb/plan/plan?select=<sequenceNumber>` (again a sequence number, 1-based per
term); it works from a fresh session and binds the editor to that plan's term.
Plan ids stay stable across sessions; sequence numbers are per-term ordinals.
