# Porting to another school or tenant

This codebase was written against one institution: University of Northwestern –
St. Paul (UNW). Most of it is ordinary Banner 9 + Degree Works parsing, but a
few values are genuinely site-specific. This page lists every one of them and
the exact symbol to change. Read it before forking for a different school.

The short version: change the hosts/school id first, then the term and attribute
conventions, then the audit node mapping and login selectors. Keep the UNW
defaults in place behind configuration where possible.

## 1. Entra ID tenant and login flow

| what | UNW value | where |
| --- | --- | --- |
| Entra tenant | `713d3c09-b1b4-46a6-93dc-cd6b86269ddd` | Not hardcoded. `auth.login()` follows the SP redirect, so the tenant is discovered from the SAML handshake. See `docs/login.md`. |
| IdP host hints | `login.microsoftonline.com`, `msftauth`, `live.com`, `adfs` | `src/flightplan/auth.py` → `LOGIN_HINTS`, `_looks_like_login`, `_on_idp`. |
| Username field | `input[name='loginfmt']`, `#i0116`, `input[type='email']` | `src/flightplan/auth.py` → `login()`. |
| Password field | `input[name='passwd']`, `#i0118`, `input[type='password']` | `src/flightplan/auth.py` → `login()`. |
| Submit button | `#idSIButton9`, submit inputs/buttons | `src/flightplan/auth.py` → `login()`. |
| TOTP code page | `#idTxtBx_SAOTCC_OTC`, submit `#idSubmit_SAOTCC_Continue` | `src/flightplan/auth.py` → `_handle_mfa()`. |
| "Stay signed in?" | `#idBtn_Back` (answered No) | `src/flightplan/auth.py` → `login()`. |
| MFA fallback walk | authenticator-app → verification-code path | `_handle_mfa()` / `_wait_for_any()` selectors. |

If your tenant uses a different identity provider (Okta, Shibboleth, ADFS
without the Microsoft forms), the whole browser driver needs adapting. Keep the
`auth.login() -> Path` contract and the storage-state format; everything above
it (cookie replay, `ensure()`, `session_valid()`) does not care how the cookies
were obtained.

Push-only 2FA and number-matching flows without a "use a verification code"
fallback cannot be automated by this driver.

## 2. Banner 9 Student Self-Service

| what | UNW value | where |
| --- | --- | --- |
| SSB origin/base | `https://registration-ss.oci.unw.edu/StudentRegistrationSsb/ssb` | `src/flightplan/config.py` → `DEFAULT_SSB_BASE`; override with `UNW_SSB_BASE`. `Settings.ssb_origin` derives the scheme+host. |
| SSB endpoints | `/classSearch/getTerms`, `/term/search`, `/searchResults/searchResults`, `/searchResults/get*`, `/plan/*` | `src/flightplan/banner/ssb.py`, `banner/search.py`, `banner/plan.py`. |
| Page/guest detection | `<meta name="fullName">` empty means guest | `auth.session_valid()`, `auth._looks_like_login()`. |
| Term binding | `POST /term/search?mode=<search\|plan\|registration>` | `SSBClient.set_term()`. |
| Plan list bootstrap | `window.bootstraps = { plans: [...] }` | `banner/plan.py` → `_extract_json_block()`. |

Banner versions differ in small ways (which filters are actually applied,
whether the plan list is JSON or server-rendered). The class-search filters are
already filtered client-side because this deployment ignores them; another
deployment may honour them but the snapshot approach still works.

## 3. Degree Works audit

| what | UNW value | where |
| --- | --- | --- |
| Audit host | `https://degreeaudit.oci.unw.edu` | `src/flightplan/audit/client.py` → `AUDIT_ORIGIN`. This is a constant: `UNW_AUDIT_URL` only changes the browser landing page. |
| Dashboard/screen URL | `https://degreeaudit.oci.unw.edu/worksheets/WEB31` | `src/flightplan/config.py` → `DEFAULT_AUDIT_URL` (`WEB31` is Ellucian's screen name, not a per-student id). |
| Identity endpoint | `/api/students/myself` | `audit/client.py` → `MYSELF_PATH`. |
| Audit endpoint | `/api/audit?studentId=…&school=…&degree=…&audit-type=AA…` | `audit/client.py` → `AUDIT_PATH`, `AuditClient.fetch()`. |
| Worksheet node types | `4700` subset, `4600` group, `4200` course, `4900` complete/waiver | `src/flightplan/audit/parser.py` → `nodeType` branches. |
| Top-level block kinds | `DEGREE`, `MAJOR`, `MINOR`, `OTHER` | `audit/parser.py` → `BLOCK_KIND`, `KIND_KEYWORDS`. |
| Credit qualifiers | `CLASSESCREDITS`, `credits`/`applied`/`needed` | `audit/parser.py`; see `docs/audit.md`. |

Other Degree Works tenants may return a different `degreeInformation` shape or
different `goalArray` codes. Start by capturing a scrubbed `/api/audit`
response, then update the parser and its fixture.

## 4. Academic calendar and term codes

UNW encodes a term as `YYYY` + season + `0`:

| code suffix | season | example |
| --- | --- | --- |
| `10` | Fall | `202610` = Fall 2026 |
| `20` | Spring | `202620` = Spring 2027 (academic-year prefix) |
| `30` | Summer | `202630` = Summer 2027 |
| `40` | Winter | `202640` (mapping exists, not seen at UNW) |

The academic-year prefix means Spring `2026xx` is calendar year 2027. The code
adds one year for anything that is not Fall.

| what | where |
| --- | --- |
| Season map and year-label rule | `src/flightplan/catalog.py` → `SEASON`, `term_season()`, `term_label()`. |
| Chronological ordering | `src/flightplan/mcp_server.py` → `_term_key()`. |
| Roadmap sequence | `mcp_server._term_sequence()`, `_next_summer()`. |
| "Current" term used for availability/registration | `mcp_server.CURRENT_TERM` (env `UNW_CURRENT_TERM`, default `202610`) and `report.CURRENT_TERM`. |
| Legacy ordering tuple | `catalog.TERM_ORDER` (reference only; do not treat it as the full catalog). |

A school using `2026FA`/`SP27` strings needs a different parser, not just a
different map. Keep term codes as opaque strings outside these helpers.

## 5. Attribute, requirement and option conventions

UNW's gen-ed blocks use Degree Works attribute targets such as `CCUL` (cultural
and global engagement), `TPHI` (theological philosophy), `NATS`/`SOCS` (natural
and social science), `BEXP`/`OTEX`/`NTCC` (Bible exposition), and `LABS` (lab
science, combined with a minimum-credit qualifier).

| what | where |
| --- | --- |
| Parse `ATTRIBUTE = …` from option notes | `src/flightplan/catalog.py` → `ATTRIBUTE_IN_NOTES`, `_parse_attribute_codes()`. |
| Turn worksheet blocks into targets | `catalog.requirements_from_audit()`. |
| Resolve targets/attributes/wildcards to sections | `catalog.resolve()`. |
| Attribute matching on sections | `Section.attribute_codes` in `src/flightplan/models.py`; `TermSnapshot.by_attribute()` in `banner/search.py`. |
| `LABS` special case | `catalog.requirements_from_audit()` (`all_of = ["LABS"]`). |
| Credit-pool wording | `CREDITS_IN_NOTES`, `RequirementBlock.notes` (`"choose N credits from the following"`). |

Every school has its own attribute vocabulary and wildcard rules. Treat the
attribute codes as data where possible; the current parser intentionally keeps
free-text conditions in `notes` rather than inventing predicates.

## 6. Rate My Professors

| what | UNW value | where |
| --- | --- | --- |
| School id | `706` | `src/flightplan/rmp/match.py` → `DEFAULT_SCHOOL_ID`; `src/flightplan/rmp/client.py` → `DEFAULT_SCHOOL_ID`. |
| School GraphQL id | `base64("School-706")` | `rmp/client.py` → `encode_global_id()`. |
| Name search | `TeacherSearchResultsPageQuery` with nested `query.schoolID` | `rmp/client.py` → `search_professors()`, `rmp/queries.py`. |
| Matching heuristics | nicknames, suffixes, surname/initial rules | `rmp/match.py`. |

Change both constants (or thread `school_id` through your calls). If the target
school has no RMP presence, ratings should remain `null`; never fabricate `0.0`
or re-rank unrated faculty as bad.

## 7. Planner assumptions

- Default credit budget is `16` in `planner.Constraints` and CLI/MCP defaults.
- Course numbers `4841`, `4855`, `4995` and labels containing
  `internship`/`research`/`capstone` count as "requires arrangement" and are
  skipped unless `include_arrangement=True`. That is a UNW convention; adjust
  `catalog.ARRANGEMENT_NUMBERS` and `_needs_arrangement()` for another catalog.
- Prerequisite parsing (`planner.parse_prerequisites`) understands Banner's
  `Course or Test: …` `and`/`or` text. Grade minimums and placement tests are
  not modelled.
- Time conflicts use half-open wall-clock intervals and Banner's linked
  lecture/lab exemption (`schedule.py`). Timezones are not converted.

## 8. Tests and fixtures after porting

1. Capture an anonymous or scrubbed page for each changed endpoint.
2. Scrub the student name, id, email, cookies, tokens and `/home/<user>` paths
   (see `CONTRIBUTING.md`).
3. Add an offline test with `httpx.MockTransport` for every new/changed parser.
4. Run `ruff check .` and `pytest -q` with `UNW_AUTO_LOGIN=0`; no test may need
   credentials or network.
5. Update `README.md` and this file with the new defaults, and add a changelog
   entry.

If the change is large, keep it behind a small adapter layer rather than
forking the package. The stable contracts are: `auth.login()/ensure()`,
`SSBClient`, `TermSnapshot`, `Audit`/`requirements_from_audit`,
`planner.Planner`, and the JSON-shaped MCP/CLI payloads.
