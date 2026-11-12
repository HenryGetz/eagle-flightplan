# UNW degree audit (Ellucian Degree Works)

Host: `https://degreeaudit.oci.unw.edu`. The URL in the brief,
`/worksheets/WEB31`, is a **route into the Degree Works SPA**, not a worksheet id.

## What WEB31 actually is

* `GET /worksheets/WEB31` returns a 1 KB React shell
  (`<title>Ellucian Degree Works Dashboard</title>`, `dashboard.bundle.js`).
  The path segment `WEB31` is the Ellucian screen name (web31 = worksheet); it is
  identical for every student and does not rotate.
* The shell calls `GET /api/users/myself`, which returns the signed-in user
  (`{"id":"00000000","name":"REDACTED STUDENT","userClass":"STU", ...}`).
* The real identity for an audit request comes from
  `GET /api/students/myself` → `_embedded.students[0]`:
  `id`, `name`, `activeTerm`, `goals[]` (school `UG`, degree `BS`,
  catalogYear `202410`, details: COLLEGE `TR`, MAJOR `CSIS`/`CSCI`,
  PROGRAM `BS-CSCI`/`BS-CSIS`, MINOR `BIBL`).
* `GET /api/audit?studentId=00000000&school=UG&degree=BS&is-process-new=false&audit-type=AA&auditId=&include-inprogress=true&include-preregistered=true&aid-term=`
  returns the whole audit as **JSON** (`application/json`, internally
  `application/vnd.net.hedtech.degree+json`), ~150 KB.
* `auditHeader.auditId` (`A000D3oX`) is generated per audit run; it is not a
  stable handle and need not be sent. `WEB31` is what the student can bookmark.

Other API surface captured while loading the dashboard:
`/api/messages`, `/api/settings/map`,
`/api/validations/special-entities/{terms,block-types,audit-formats,audit-freeze-types,course-link-*}`,
`/api/audit` (PDF/format variants), `/about`.

## Payload shape → `eagle_flightplan.audit.models`

```
auditHeader              → Audit.student_name / student_id / gpa / raw_ref
                           residentApplied + transferApplied + examAppliedCredits
                             → Audit.credits_earned
                           residentAppliedInProgress + … → Audit.credits_in_progress
degreeInformation        → catalog_year (catalogYearLit), majors, minors (goalArray)
blockArray[]             → Audit.groups[] (RequirementGroup)
  requirementType        → GroupKind: DEGREE|MAJOR|MINOR|OTHER→gened
  header.qualifierArray  → CLASSESCREDITS gives credits/applied/needed
                             → required_credits / earned_credits / remaining_credits
  ruleArray[]            → nested sub_groups + blocks
    nodeType 4700        → Subset (container)
    nodeType 4600        → Group (container; numberOfGroups/numberOfRules)
    nodeType 4200        → Course (leaf → RequirementBlock)
    nodeType 4900        → Complete/waiver (leaf note block)
    requirement.courseArray        → allowed options (CourseRef, status="option")
    classesAppliedToRule.classArray → courses actually applied (status completed/in_progress)
classInformation.classArray        → titles, grades, transfer markers, terms
inProgress.classArray              → registered-for-this-term courses
```

`RequirementGroup.walk()` flattens nested groups; `Audit.unmet()` returns every
group whose status is not `met` (excluding GPA/residency bookkeeping).

### Mapping rules implemented in `audit/parser.py`

* **Status** — `percentComplete >= 100` → `met`; otherwise `in_progress` if any
  applied course in that node has an in-progress grade (`IP`, `PR`, `INP`), else
  `not_met`. Group status uses the block's own `percentComplete`.
* **Credits** — top-level groups read `CLASSESCREDITS` (`credits`, `applied`,
  `needed`; `needed = credits - applied` when absent). Nested groups parse
  `"… - 22 credits"` / `"… - 6-7 credits"` from the title and sum applied course
  credits (including in-progress), giving `remaining_credits`.
* **Options vs applied** — a `RequirementBlock.options` list starts with the
  applied courses (grade, term, `in_progress`, `transfer`, `ap_credit`) and then
  appends catalog options, deduped on `SUBJECT NUMBER`.
* **"Choose N credits from the following"** — `requirement.creditsBegin` /
  `creditsEnd` become `notes = "choose 3-4 credits from the following"`;
  `classesBegin` becomes `pick_n_of`.
* **Wildcards** — options with `discipline "@"` / `number "@"` plus `withArray`
  conditions become `CourseRef(subject="", number="",
  satisfies_notes="ATTRIBUTE = NATS")`. `except` clauses are appended to notes.
* **Transfer / AP** — class records with `transferType` `TR`/`AP`, `transfer`
  `T`, or grade `AP`/`TA`/`TR` are flagged `transfer=True`;
  `transferType == "AP"` or grade `AP` sets `ap_credit=True`. UNW's `transfer:
  "C"` marker means "credit accepted" and is *not* used as a transfer signal.
* **`in_progress`** — also available top-level via `inProgress.classArray`
  (7 courses / 15 credits for this student in Fall 2026).

## Fixture

`tests/fixtures/audit_WEB31.json` is the real audit with only identity scrubbed
(`studentName`, `studentId`, `studentEmail`); every requirement, rule, course and
qualifier is untouched. Tests assert:

* 5 top-level groups, all four kinds present,
* the Degree group is 125 required / 101 applied / 24 remaining,
* `Major: Computer Science` exists with 48 earned and >0 remaining,
* `Python Programming` parses as unmet with options
  `COS 2005, COS 2211, COS 2212`,
* in-progress (`Statistics`, MAT 2055) and transfer/AP rows are flagged,
* attribute wildcards produce a `satisfies_notes` string.

## Parsed summary for this student (live, 2026-11-11)

| field | value |
| --- | --- |
| name | REDACTED STUDENT |
| id | 00000000 |
| catalog year | 2024-2025 (Fall 2024) |
| majors | Cybersecurity; Computer Science |
| minor | Bible |
| credits earned | 86 (resident 70 + transfer 16 + exam 0) |
| credits in progress | 15 (7 courses, Fall 2026) |
| GPA | 4.000 (system and Degree Works agree) |
| percent complete | 76 % |

Groups:

| kind | title | required | earned | remaining | status |
| --- | --- | --- | --- | --- | --- |
| degree | Degree: Bachelor of Science | 125 | 101 | 24 | not_met |
| gened | TRAD: Core Curriculum | 55 | 35 | 20 | in_progress |
| major | Major: Cybersecurity | 48 | 34 | 14 | in_progress |
| major | Major: Computer Science | 57 | 48 | 9 | in_progress |
| minor | Minor: Bible | 0 (pseudo) | 10 | 0 | not_met |

Outstanding requirements (`Audit.unmet()`), condensed to the actionable leaves:

| parent group | requirement | state | candidates |
| --- | --- | --- | --- |
| BIBLICAL THINKING AND LIVING (22 cr) | Bible Exposition – 4 credits | not_met, 4 cr | BIB 3020, BIB 3805, or ATTRIBUTE OTEX/NTCC/BEXP |
| BIBLICAL THINKING AND LIVING | Theology | not_met, 1 of | BIB 4360, BIB 4365, BIB 4460, BIB 4825 |
| BIBLICAL THINKING AND LIVING | Theological Philosophy | not_met, 3–4 cr | ATTRIBUTE TPHI |
| LEADERSHIP & PROFESSIONALISM (6–7 cr) | Leading with Courage and Calling – 2 cr | not_met, 1 of | LDR 4425, LDR 4825 |
| CULTURAL AND GLOBAL ENGAGEMENT (6 cr) | Cultural & Global Engagement / Modern World Language | not_met, 1 of | ATTRIBUTE CCUL |
| SCIENTIFIC & QUANTITATIVE LITERACY (8 cr) | Statistics (C- minimum) | in_progress, MAT 2055 | MAT 2055 |
| SCIENTIFIC & QUANTITATIVE LITERACY | Natural Science / Social Science | not_met, 4 cr, one NATS required | ATTRIBUTE NATS, SOSC, LABS |
| Core (both majors) | Python Programming | not_met, 3 cr | COS 2005, COS 2211, COS 2212 |
| CYBERSECURITY REQUIREMENTS (39 cr) | Tools & Techniques in CS & Cybersecurity | in_progress (CYS 3265) | CYS 3265 / COS 3265 |
| CYBERSECURITY REQUIREMENTS | Cybersecurity: Current Practices & Trends | not_met, 1 of | CYS 4245 / MIS 4245 |
| CYBERSECURITY REQUIREMENTS | Introduction to Cryptography | in_progress (CYS 4369) | CYS 4369 / MIS 4369 |
| CYBERSECURITY REQUIREMENTS | Computer Firewalls & Penetration Testing | in_progress (CYS 4465) | CYS 4465 / MIS 4465 |
| CYBERSECURITY REQUIREMENTS | Digital Forensics | not_met, 1 of | CYS 4466 |
| CYBERSECURITY REQUIREMENTS | Server Administration | not_met, 1 of | CYS 3185 / MIS 3185 |
| Major: Computer Science | Additional credits for double major with Cybersecurity | not_met, 9 cr | COS 4841, CYS 4841, CYS 4855, COS 4855, COS 4995, CYS 4995, DAL |
| Minor: Bible | REQUIRED COURSES | not_met, 10 cr applied | BIB 4360/4365/4460/4825/6312, ATTRIBUTE TPHI |

The four Fall-2026 courses in progress (CYS 3265, CYS 4369, CYS 4465, MAT 2055)
already satisfy their blocks; the remaining Cybersecurity gaps are the two
not-yet-registered courses (4245-class and a forensics/server course) plus the
double-major electives.

## Known limits

* `sessionDescription` is not in the class-search payload; `partOfTerm` is a code
  (`"1"`). Both are documented in `docs/banner-api.md`.
* The Bible minor's `CLASSESCREDITS` qualifier is `0 Credits Pseudo`, so
  `required_credits` is 0 while 10 credits are applied. The group status still
  reflects the worksheet's own percentage.
* Free-text conditions are preserved in `notes`/`satisfies_notes`; they are not
  interpreted into a machine-checkable predicate.
* The audit JSON is regenerated per request (`auditHeader.auditId`,
  `refresh.bridged` timestamps); nothing in the parser depends on those values.

## Raw artifacts

* `captures/audit.json` — the live, unmodified `/api/audit` payload (contains
  student identity; `captures/` is gitignored).
* `captures/audit.jsonl` — every request/response the dashboard made on load,
  including the `/api/audit` query string.
* `captures/audit-landing.html` — the React shell for `/worksheets/WEB31`.
* `tests/fixtures/audit_WEB31.json` — scrubbed copy used by the tests.
