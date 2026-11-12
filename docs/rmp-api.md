# Rate My Professors web API

Reverse-engineered 2026-11-11 from the public UNW St. Paul search page:
<https://www.ratemyprofessors.com/search/professors/706?q=Jackie%20Glenny>.

## Endpoint

```
POST https://www.ratemyprofessors.com/graphql
Content-Type: application/json
User-Agent: Mozilla/5.0 ...
Origin: https://www.ratemyprofessors.com
Referer: https://www.ratemyprofessors.com/
```

Body is the standard Relay shape:

```json
{"query": "<operation document>", "operationName": "TeacherSearchResultsPageQuery", "variables": {...}}
```

A working request can be issued with plain `curl`; as of this writing the
endpoint does **not** require an `Authorization` header and is not behind a
Cloudflare challenge:

```bash
curl -sS https://www.ratemyprofessors.com/graphql \
  -H 'Content-Type: application/json' \
  -H 'User-Agent: Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 Chrome/131.0.0.0 Safari/537.36' \
  --data-binary @search-request.json
```

with `search-request.json`:

```json
{
  "query": "<SEARCH_PROFESSORS_QUERY below>",
  "operationName": "TeacherSearchResultsPageQuery",
  "variables": {
    "query": {
      "text": "Jackie Glenny",
      "schoolID": "U2Nob29sLTcwNg==",
      "fallback": true
    },
    "schoolID": "U2Nob29sLTcwNg==",
    "includeSchoolFilter": true
  }
}
```

**Important:** the school filter must be inside the `query` object
(`TeacherSearchQuery.schoolID`). Passing only the top-level `$schoolID`
returns professors from every school while still rendering the school card.
This was observed directly: omitting the nested value returned "Dalton Glenny
(Physics)" and "Glennys Farrar (Physics)" instead of the three UNW records.

## Auth token

The old `Authorization: Basic dGVzdDp0ZXN0` token is gone. The current bundle
(`/assets/index-9J7uI-NE.js`, discovered via the search page's
`<script type="module" src=...>`) contains:

```js
Authorization: Bj(n)
```

and `Bj` is:

```js
Bj = e => {
  let t = e && zO.parse(e);
  t = t?.rmpAuth;
  return t
    ? `Bearer ${t}`
    : !Dn() && (t = Fn.get("rmpAuth"))
      ? `Bearer ${t}`
      : null;
}
```

So:

* There is **no static bearer/basic token** in the bundle.
* Anonymous requests send `Authorization: null` (the header is effectively
  omitted) and work.
* A logged-in browser sends `Authorization: Bearer <rmpAuth cookie>`.
* The token is the site's own session cookie, so it is not long-lived. Treat
  it as short-lived and refresh it from the cookie/login flow.

If RMP starts requiring auth, obtain the cookie by logging in through the
normal web UI and reading the `rmpAuth` cookie, then:

```python
RMPClient(auth_token="<rmpAuth cookie value>")
# or refresh it lazily:
RMPClient(token_provider=lambda: read_rmp_auth_cookie())
```

`RMPClient` calls the provider once on a 401/403 before raising `Blocked`.
`refresh_token()` forces a re-read.

To re-extract after the bundle rotates:

1. `curl` the search page with a browser User-Agent.
2. Find `<script type="module" ... src="/assets/index-*.js">`, download it.
3. `grep -oE '.{40}Authorization.{80}'` and follow the interceptor.
4. `grep` for `query <OperationName>(` / `text:\`query` — Relay embeds the
   complete operation text in `params.text`, not a persisted-query id.

## School

`706` encodes as `U2Nob29sLTcwNg==` (`base64("School-706")`). Verified:

```
legacyId    706
name        University of Northwestern St. Paul
city        Roseville
state       MN
country     0-US-United States
numRatings  117
```

## GraphQL operation documents

These strings are copied verbatim from the bundle (Relay-compiled fragments
with generated suffixes). The same strings live in
`src/flightplan/rmp/queries.py`; edit them only by re-extracting.

Variables:

| Operation | Variables |
|---|---|
| `TeacherSearchResultsPageQuery` | `query: {text, schoolID?, fallback?}`, `schoolID`, `includeSchoolFilter` |
| `TeacherRatingsPageQuery` | `id: "VGVhY2hlci0xNjc3MTc4"` (`base64("Teacher-1677178")`) |
| `RatingsListQuery` | `count`, `id`, `courseFilter`, `cursor` |
| `SchoolRatingsPageQuery` | `id: "U2Nob29sLTcwNg=="` |

### TeacherSearchResultsPageQuery (school-scoped name search)

```graphql
query TeacherSearchResultsPageQuery(
  $query: TeacherSearchQuery!
  $schoolID: ID
  $includeSchoolFilter: Boolean!
) {
  search: newSearch {
    ...TeacherSearchPagination_search_2MvZSr
  }
  school: node(id: $schoolID) @include(if: $includeSchoolFilter) {
    __typename
    ... on School {
      name
      ...StickyHeaderContent_school
    }
    id
  }
}

fragment CardFeedback_teacher on Teacher {
  wouldTakeAgainPercent
  avgDifficulty
}

fragment CardName_teacher on Teacher {
  firstName
  lastName
}

fragment CardSchool_teacher on Teacher {
  department
  school {
    name
    id
  }
}

fragment CompareSchoolLink_school on School {
  legacyId
}

fragment HeaderDescription_school on School {
  name
  city
  state
  legacyId
  ...RateSchoolLink_school
  ...CompareSchoolLink_school
}

fragment HeaderRateButton_school on School {
  ...RateSchoolLink_school
  ...CompareSchoolLink_school
}

fragment RateSchoolLink_school on School {
  legacyId
  lockStatus
}

fragment StickyHeaderContent_school on School {
  name
  ...HeaderDescription_school
  ...HeaderRateButton_school
}

fragment TeacherBookmark_teacher on Teacher {
  id
  isSaved
}

fragment TeacherCard_teacher on Teacher {
  id
  legacyId
  avgRating
  numRatings
  ...CardFeedback_teacher
  ...CardSchool_teacher
  ...CardName_teacher
  ...TeacherBookmark_teacher
}

fragment TeacherSearchPagination_search_2MvZSr on newSearch {
  teachers(query: $query, first: 5, after: "") {
    didFallback
    edges {
      cursor
      node {
        ...TeacherCard_teacher
        id
        __typename
      }
    }
    pageInfo {
      hasNextPage
      endCursor
    }
    resultCount
    filters {
      field
      options {
        value
        id
      }
    }
  }
}

```

### TeacherRatingsPageQuery (detail, first 5 reviews)

```graphql
query TeacherRatingsPageQuery(
  $id: ID!
) {
  node(id: $id) {
    __typename
    ... on Teacher {
      id
      legacyId
      firstName
      lastName
      department
      school {
        legacyId
        name
        city
        state
        country
        id
      }
      lockStatus
      ...StickyHeaderContent_teacher
      ...MiniStickyHeader_teacher
      ...TeacherBookmark_teacher
      ...RatingDistributionWrapper_teacher
      ...TeacherInfo_teacher
      ...SimilarProfessors_teacher
      ...TeacherRatingTabs_teacher
    }
    id
  }
}

fragment CompareProfessorLink_teacher on Teacher {
  legacyId
}

fragment CourseMeta_rating on Rating {
  attendanceMandatory
  wouldTakeAgain
  grade
  textbookUse
  isForOnlineClass
  isForCredit
}

fragment HeaderDescription_teacher on Teacher {
  id
  legacyId
  firstName
  lastName
  department
  school {
    legacyId
    name
    city
    state
    id
  }
  ...TeacherTitles_teacher
  ...TeacherBookmark_teacher
  ...RateTeacherLink_teacher
  ...CompareProfessorLink_teacher
}

fragment HeaderRateButton_teacher on Teacher {
  ...RateTeacherLink_teacher
  ...CompareProfessorLink_teacher
}

fragment MiniStickyHeader_teacher on Teacher {
  id
  legacyId
  firstName
  lastName
  department
  departmentId
  school {
    legacyId
    name
    city
    state
    id
  }
  ...TeacherBookmark_teacher
  ...RateTeacherLink_teacher
  ...CompareProfessorLink_teacher
}

fragment NameLink_teacher on Teacher {
  isProfCurrentUser
  id
  legacyId
  firstName
  lastName
  school {
    name
    id
  }
}

fragment NameTitle_teacher on Teacher {
  id
  firstName
  lastName
  department
  school {
    legacyId
    name
    id
  }
  ...TeacherDepartment_teacher
  ...TeacherBookmark_teacher
}

fragment NoRatingsArea_teacher on Teacher {
  lastName
  ...RateTeacherLink_teacher
}

fragment NumRatingsLink_teacher on Teacher {
  numRatings
  ...RateTeacherLink_teacher
}

fragment ProfessorNoteEditor_rating on Rating {
  id
  legacyId
  class
  teacherNote {
    id
    teacherId
    comment
  }
}

fragment ProfessorNoteEditor_teacher on Teacher {
  id
}

fragment ProfessorNoteFooter_note on TeacherNotes {
  legacyId
  flagStatus
}

fragment ProfessorNoteFooter_teacher on Teacher {
  legacyId
  isProfCurrentUser
}

fragment ProfessorNoteHeader_note on TeacherNotes {
  createdAt
  updatedAt
}

fragment ProfessorNoteHeader_teacher on Teacher {
  lastName
}

fragment ProfessorNoteSection_rating on Rating {
  teacherNote {
    ...ProfessorNote_note
    id
  }
  ...ProfessorNoteEditor_rating
}

fragment ProfessorNoteSection_teacher on Teacher {
  ...ProfessorNote_teacher
  ...ProfessorNoteEditor_teacher
}

fragment ProfessorNote_note on TeacherNotes {
  comment
  ...ProfessorNoteHeader_note
  ...ProfessorNoteFooter_note
}

fragment ProfessorNote_teacher on Teacher {
  ...ProfessorNoteHeader_teacher
  ...ProfessorNoteFooter_teacher
}

fragment RateTeacherLink_teacher on Teacher {
  legacyId
  numRatings
  lockStatus
}

fragment RatingDistributionChart_ratingsDistribution on ratingsDistribution {
  r1
  r2
  r3
  r4
  r5
}

fragment RatingDistributionWrapper_teacher on Teacher {
  ...NoRatingsArea_teacher
  ratingsDistribution {
    total
    ...RatingDistributionChart_ratingsDistribution
  }
}

fragment RatingFooter_rating on Rating {
  id
  comment
  adminReviewedAt
  flagStatus
  legacyId
  thumbsUpTotal
  thumbsDownTotal
  thumbs {
    thumbsUp
    thumbsDown
    computerId
    id
  }
  teacherNote {
    id
  }
  ...Thumbs_rating
}

fragment RatingFooter_teacher on Teacher {
  id
  legacyId
  lockStatus
  isProfCurrentUser
  ...Thumbs_teacher
}

fragment RatingHeader_rating on Rating {
  legacyId
  date
  class
  helpfulRating
  clarityRating
  isForOnlineClass
}

fragment RatingSuperHeader_rating on Rating {
  legacyId
}

fragment RatingSuperHeader_teacher on Teacher {
  firstName
  lastName
  legacyId
  school {
    name
    id
  }
}

fragment RatingTags_rating on Rating {
  ratingTags
}

fragment RatingValue_teacher on Teacher {
  avgRating
  numRatings
  ...NumRatingsLink_teacher
}

fragment RatingValues_rating on Rating {
  helpfulRating
  clarityRating
  difficultyRating
}

fragment Rating_rating on Rating {
  comment
  flagStatus
  createdByUser
  teacherNote {
    id
  }
  ...RatingHeader_rating
  ...RatingSuperHeader_rating
  ...RatingValues_rating
  ...CourseMeta_rating
  ...RatingTags_rating
  ...RatingFooter_rating
  ...ProfessorNoteSection_rating
}

fragment Rating_teacher on Teacher {
  ...RatingFooter_teacher
  ...RatingSuperHeader_teacher
  ...ProfessorNoteSection_teacher
}

fragment RatingsFilter_teacher on Teacher {
  courseCodes {
    courseCount
    courseName
  }
}

fragment RatingsList_teacher on Teacher {
  id
  legacyId
  lastName
  numRatings
  school {
    id
    legacyId
    name
    city
    state
    avgRating
    numRatings
  }
  ...Rating_teacher
  ...NoRatingsArea_teacher
  ratings(first: 5) {
    edges {
      cursor
      node {
        ...Rating_rating
        id
        __typename
      }
    }
    pageInfo {
      hasNextPage
      endCursor
    }
  }
}

fragment SimilarProfessorListItem_teacher on RelatedTeacher {
  legacyId
  firstName
  lastName
  avgRating
}

fragment SimilarProfessors_teacher on Teacher {
  department
  relatedTeachers {
    legacyId
    ...SimilarProfessorListItem_teacher
    id
  }
}

fragment StickyHeaderContent_teacher on Teacher {
  ...HeaderDescription_teacher
  ...HeaderRateButton_teacher
  ...MiniStickyHeader_teacher
}

fragment TeacherBookmark_teacher on Teacher {
  id
  isSaved
}

fragment TeacherDepartment_teacher on Teacher {
  department
  departmentId
  school {
    legacyId
    name
    isVisible
    id
  }
}

fragment TeacherFeedback_teacher on Teacher {
  numRatings
  avgDifficulty
  wouldTakeAgainPercent
}

fragment TeacherInfo_teacher on Teacher {
  id
  lastName
  numRatings
  ...RatingValue_teacher
  ...NameTitle_teacher
  ...TeacherTags_teacher
  ...NameLink_teacher
  ...TeacherFeedback_teacher
  ...RateTeacherLink_teacher
  ...CompareProfessorLink_teacher
}

fragment TeacherRatingTabs_teacher on Teacher {
  numRatings
  courseCodes {
    courseName
    courseCount
  }
  ...RatingsList_teacher
  ...RatingsFilter_teacher
}

fragment TeacherTags_teacher on Teacher {
  lastName
  teacherRatingTags {
    legacyId
    tagCount
    tagName
    id
  }
}

fragment TeacherTitles_teacher on Teacher {
  department
  school {
    legacyId
    name
    id
  }
}

fragment Thumbs_rating on Rating {
  id
  comment
  adminReviewedAt
  flagStatus
  legacyId
  thumbsUpTotal
  thumbsDownTotal
  thumbs {
    computerId
    thumbsUp
    thumbsDown
    id
  }
  teacherNote {
    id
  }
}

fragment Thumbs_teacher on Teacher {
  id
  legacyId
  lockStatus
  isProfCurrentUser
}

```

### RatingsListQuery (paginated reviews)

```graphql
query RatingsListQuery(
  $count: Int!
  $id: ID!
  $courseFilter: String
  $cursor: String
) {
  node(id: $id) {
    __typename
    ... on Teacher {
      ...RatingsList_teacher_4pguUW
    }
    id
  }
}

fragment CourseMeta_rating on Rating {
  attendanceMandatory
  wouldTakeAgain
  grade
  textbookUse
  isForOnlineClass
  isForCredit
}

fragment NoRatingsArea_teacher on Teacher {
  lastName
  ...RateTeacherLink_teacher
}

fragment ProfessorNoteEditor_rating on Rating {
  id
  legacyId
  class
  teacherNote {
    id
    teacherId
    comment
  }
}

fragment ProfessorNoteEditor_teacher on Teacher {
  id
}

fragment ProfessorNoteFooter_note on TeacherNotes {
  legacyId
  flagStatus
}

fragment ProfessorNoteFooter_teacher on Teacher {
  legacyId
  isProfCurrentUser
}

fragment ProfessorNoteHeader_note on TeacherNotes {
  createdAt
  updatedAt
}

fragment ProfessorNoteHeader_teacher on Teacher {
  lastName
}

fragment ProfessorNoteSection_rating on Rating {
  teacherNote {
    ...ProfessorNote_note
    id
  }
  ...ProfessorNoteEditor_rating
}

fragment ProfessorNoteSection_teacher on Teacher {
  ...ProfessorNote_teacher
  ...ProfessorNoteEditor_teacher
}

fragment ProfessorNote_note on TeacherNotes {
  comment
  ...ProfessorNoteHeader_note
  ...ProfessorNoteFooter_note
}

fragment ProfessorNote_teacher on Teacher {
  ...ProfessorNoteHeader_teacher
  ...ProfessorNoteFooter_teacher
}

fragment RateTeacherLink_teacher on Teacher {
  legacyId
  numRatings
  lockStatus
}

fragment RatingFooter_rating on Rating {
  id
  comment
  adminReviewedAt
  flagStatus
  legacyId
  thumbsUpTotal
  thumbsDownTotal
  thumbs {
    thumbsUp
    thumbsDown
    computerId
    id
  }
  teacherNote {
    id
  }
  ...Thumbs_rating
}

fragment RatingFooter_teacher on Teacher {
  id
  legacyId
  lockStatus
  isProfCurrentUser
  ...Thumbs_teacher
}

fragment RatingHeader_rating on Rating {
  legacyId
  date
  class
  helpfulRating
  clarityRating
  isForOnlineClass
}

fragment RatingSuperHeader_rating on Rating {
  legacyId
}

fragment RatingSuperHeader_teacher on Teacher {
  firstName
  lastName
  legacyId
  school {
    name
    id
  }
}

fragment RatingTags_rating on Rating {
  ratingTags
}

fragment RatingValues_rating on Rating {
  helpfulRating
  clarityRating
  difficultyRating
}

fragment Rating_rating on Rating {
  comment
  flagStatus
  createdByUser
  teacherNote {
    id
  }
  ...RatingHeader_rating
  ...RatingSuperHeader_rating
  ...RatingValues_rating
  ...CourseMeta_rating
  ...RatingTags_rating
  ...RatingFooter_rating
  ...ProfessorNoteSection_rating
}

fragment Rating_teacher on Teacher {
  ...RatingFooter_teacher
  ...RatingSuperHeader_teacher
  ...ProfessorNoteSection_teacher
}

fragment RatingsList_teacher_4pguUW on Teacher {
  id
  legacyId
  lastName
  numRatings
  school {
    id
    legacyId
    name
    city
    state
    avgRating
    numRatings
  }
  ...Rating_teacher
  ...NoRatingsArea_teacher
  ratings(first: $count, after: $cursor, courseFilter: $courseFilter) {
    edges {
      cursor
      node {
        ...Rating_rating
        id
        __typename
      }
    }
    pageInfo {
      hasNextPage
      endCursor
    }
  }
}

fragment Thumbs_rating on Rating {
  id
  comment
  adminReviewedAt
  flagStatus
  legacyId
  thumbsUpTotal
  thumbsDownTotal
  thumbs {
    computerId
    thumbsUp
    thumbsDown
    id
  }
  teacherNote {
    id
  }
}

fragment Thumbs_teacher on Teacher {
  id
  legacyId
  lockStatus
  isProfCurrentUser
}

```

### SchoolRatingsPageQuery (school lookup by id)

```graphql
query SchoolRatingsPageQuery(
  $id: ID!
) {
  school: node(id: $id) {
    __typename
    ... on School {
      id
      legacyId
      name
      city
      state
      country
      numRatings
      lockStatus
      ...StickyHeaderContent_school
      ...MiniStickyHeader_school
      ...OverallRating_school
      ...SchoolSummary_school
      ...SchoolRatingsContainer_school
      ...RateSchoolLink_school
      ...NoRatingsArea_school
    }
    id
  }
}

fragment CompareSchoolLink_school on School {
  legacyId
}

fragment HeaderDescription_school on School {
  name
  city
  state
  legacyId
  ...RateSchoolLink_school
  ...CompareSchoolLink_school
}

fragment HeaderRateButton_school on School {
  ...RateSchoolLink_school
  ...CompareSchoolLink_school
}

fragment MiniStickyHeader_school on School {
  name
  city
  state
  legacyId
  ...RateSchoolLink_school
  ...CompareSchoolLink_school
}

fragment NoRatingsArea_school on School {
  name
  ...RateSchoolLink_school
}

fragment OverallRating_school on School {
  avgRatingRounded
  avgRating
  numRatings
}

fragment RateSchoolLink_school on School {
  legacyId
  lockStatus
}

fragment SchoolRatingFooter_rating on SchoolRating {
  id
  comment
  flagStatus
  legacyId
  ...Thumbs_schoolRating
}

fragment SchoolRatingFooter_school on School {
  id
  legacyId
  ...Thumbs_school
}

fragment SchoolRatingSuperHeader_rating on SchoolRating {
  legacyId
}

fragment SchoolRatingSuperHeader_school on School {
  name
  legacyId
}

fragment SchoolRating_rating on SchoolRating {
  clubsRating
  comment
  date
  facilitiesRating
  foodRating
  happinessRating
  internetRating
  locationRating
  opportunitiesRating
  reputationRating
  safetyRating
  socialRating
  legacyId
  flagStatus
  createdByUser
  ...SchoolRatingFooter_rating
  ...SchoolRatingSuperHeader_rating
}

fragment SchoolRating_school on School {
  ...SchoolRatingSuperHeader_school
  ...SchoolRatingFooter_school
}

fragment SchoolRatingsContainer_school on School {
  numRatings
  ...SchoolRatingsList_school
}

fragment SchoolRatingsList_school on School {
  id
  name
  city
  state
  country
  legacyId
  ratings(first: 5) {
    edges {
      cursor
      node {
        ...SchoolRating_rating
        id
        __typename
      }
    }
    pageInfo {
      hasNextPage
      endCursor
    }
  }
  ...SchoolRating_school
}

fragment SchoolSummary_school on School {
  summary {
    schoolReputation
    schoolSatisfaction
    internetSpeed
    campusCondition
    schoolSafety
    careerOpportunities
    socialActivities
    foodQuality
    clubAndEventActivities
    campusLocation
  }
}

fragment StickyHeaderContent_school on School {
  name
  ...HeaderDescription_school
  ...HeaderRateButton_school
}

fragment Thumbs_school on School {
  id
  legacyId
}

fragment Thumbs_schoolRating on SchoolRating {
  id
  legacyId
  thumbsDownTotal
  thumbsUpTotal
  userThumbs {
    computerId
    thumbsUp
    thumbsDown
    id
  }
}

```

## Field meanings

| GraphQL field | Meaning |
|---|---|
| `id` / `legacyId` | Global base64 id / numeric id used in URLs |
| `avgRating` | Mean overall quality, 1.0–5.0; null when `numRatings == 0` |
| `avgDifficulty` | Mean difficulty, 1.0–5.0; null when unrated |
| `wouldTakeAgainPercent` | 0–100; **-1 / null means no data** (e.g. 30.7692 displays as 31%) |
| `numRatings` | Number of reviews; 0 means the stats are placeholders |
| `department` / `departmentId` | Department name / numeric id |
| `teacherRatingTags` | Aggregated `{tagName, tagCount}` list |
| `ratingsDistribution` | Counts `r1..r5`, `total` |
| `ratings(first: N)` | Reviews: `date`, `class`, `clarityRating`, `helpfulRating`, `difficultyRating`, `comment`, `grade`, `ratingTags`, thumbs |
| `courseCodes` | `{courseName, courseCount}` for courses taught |
| `relatedTeachers` | Similar professors (`legacyId`, name, `avgRating`) |
| `didFallback` | True when RMP relaxed the name search |
| `lockStatus` | Rating-form lock state (`none` normally) |

`wouldTakeAgainPercent` in the 2025+ API is a float; older docs show the
rounded int field `wouldTakeAgainPercentRounded`, which the detail operation
does not request.

## Rate limits, Cloudflare and politeness

Observed on 2026-11-11:

* Plain `curl`/`httpx` from a datacenter Linux host: HTTP 200, JSON, no
  Cloudflare challenge, no auth challenge.
* Response headers include AWS ALB sticky cookies (`AWSALB`, `AWSALBCORS`),
  `server: nginx/1.22.0`, `cache-control: no-cache`.
* The search HTML is server-rendered and contains
  `window.__RELAY_STORE__`, so it is a usable fallback if GraphQL dies.
* No 429 was observed during this work. The client still:

  * sleeps 300–700 ms of jitter between network calls;
  * caps in-flight requests at 2 (effectively 1 because of the delay lock);
  * retries 429/5xx with jittered exponential backoff (max 4 retries, honors
    `Retry-After`, capped at 30 s), then raises `RateLimited`;
  * recognises Cloudflare HTML/`Just a moment...`/`Attention Required` and
    raises `Blocked`;
  * raises `GraphQLError` with every server message for non-empty `errors`.

Do not bulk-crawl. The public search only returns the first 5 matches per
query; use exact names and the cache.

## SQLite cache

`src/flightplan/rmp/cache.py`

* Path: `.state/rmp.sqlite` (override with `RMP_CACHE_PATH`).
* Creates `.state/` with `mkdir(parents=True, exist_ok=True)` and only ever
  touches its own file; other files in `.state/` (e.g. `unw_state.json`) are
  left alone.
* WAL mode + `synchronous=NORMAL`.
* Key: SHA-256 of canonical JSON `{operation, query, variables}`.
* TTL: 7 days; expired entries are deleted on read.
* Values: the raw GraphQL `data` object, so cache hits cost no network call.
* Corrupt JSON is treated as a miss.

## Name matching

`match_professor(name, school_id=706) -> Match`

* Normalizes accents, case, punctuation, apostrophes (`O'Brien` → `obrien`),
  hyphens (`Smith-Jones` → `smith jones`), suffixes (`Jr`, `III`) and titles.
* Handles `"Last, First"`.
* First-initial expansion (`J. Glenny`) and ~50 nickname groups with
  transitive equivalence (Jack/John/Jonathan/Jackie, Mike/Michael,
  Sue/Susan/Suzanne, ...).
* Surname-only queries return `professor=None` plus alternatives when more
  than one professor shares the surname; they never silently pick one.
* Tied exact names (e.g. two "John Printy" records) also return
  `professor=None`, confidence 0.5, and both alternatives.
* Confidence: exact 0.99, nickname 0.92, first initial 0.85, unique surname
  0.70, ambiguous 0.4–0.5, no match 0.0. Professors with `numRatings == 0` get
  confidence multiplied by 0.8 and `avgRating`/`avgDifficulty`/
  `wouldTakeAgainPercent` forced to `None` — never `0.0`.

## Failure modes / what breaks first

1. **Schema drift in the verbatim documents** (most likely). RMP removes or
   renames fields → `GraphQLError` such as `Cannot query field ...`. Fix:
   re-extract `params.text` from the current bundle and update
   `queries.py`; the operation names and variables are stable.
2. **Auth/Cloudflare policy change.** If 401/403/challenge HTML starts
   appearing, `Blocked` is raised. Fix: supply an `rmpAuth` bearer token from
   a logged-in browser, or use Playwright to POST from a real browser
   context; the server-rendered search HTML is the last-resort scraper.
3. **Search behavior/ranking changes.** Results are relevance-ranked and can
   include first-name matches. The nested `schoolID` filter and exact-name
   assertions in `tests/test_rmp_network.py` catch this. Re-verify the
   ground-truth numbers before trusting new results.
4. **Rate limiting.** Handled with backoff; if it persists, raise the delay
   or stop querying. `RateLimited.retry_after` exposes `Retry-After`.
5. **Cache staleness.** 7-day TTL means a new review may not show up
   immediately; delete `.state/rmp.sqlite` or call `client.cache.clear()`.

## Dependencies

Already present in `pyproject.toml`; the builder does not need to add
anything for this module:

* `httpx>=0.27`
* `pydantic>=2.6`
* `pytest>=8` (dev/test only)
* `sqlite3` from the standard library

## Tests

Offline (default; uses `httpx.MockTransport` + recorded fixtures under
`tests/fixtures/rmp/`):

```bash
PYTHONPATH=src .venv/bin/python -m pytest tests/test_rmp_client.py tests/test_rmp_cache.py tests/test_rmp_match.py -q
```

Live smoke test (skipped unless explicitly enabled; the `network` marker is
not registered in `pyproject.toml` because this module does not own that file,
so pytest may print an unknown-marker warning):

```bash
RMP_RUN_NETWORK=1 PYTHONPATH=src .venv/bin/python -m pytest tests/test_rmp_network.py -m network -q
```

Fixtures `teacher_zero_ratings.json` and `error_graphql.json` are synthetic
edge cases; everything else is a captured real response. `teacher_zero_ratings`
sets `numRatings: 0` with 0/-1 sentinels to test the null-rating path without
inventing a real professor.

## Probe CLI

```bash
PYTHONPATH=src .venv/bin/python scripts/rmp_probe.py "Jackie Glenny"
PYTHONPATH=src .venv/bin/python scripts/rmp_probe.py --pretty "Glenny"
PYTHONPATH=src .venv/bin/python scripts/rmp_probe.py --no-cache --no-detail "J. Glenny"
```

Prints JSON with the normalized professor record, confidence, alternatives
and reason. Exact-name matches are hydrated with the detail query (one extra
request, cached) so recent ratings/courses are included; `--no-detail`
suppresses that.
