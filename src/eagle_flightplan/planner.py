"""The planning engine: which sections to take, which term, with whom.

Deterministic and offline-testable: Banner snapshots, RMP ratings and
prerequisite text all come from injectable providers.

Hard constraints
----------------
1. Every must-take major course that is offered in the term is placed.
2. No time conflicts (``schedule.find_conflicts``; TBA/async never clash).
3. ``max_credits`` (default 16), ``avoid_days``, ``earliest``/``latest``.
4. Prerequisites must be completed before the term (parsed from Banner's
   "Course or Test: ..." text; ``and`` binds tighter than ``or``).

Soft objective
--------------
Maximise Rate My Professors rating. Rated instructors rank first; ``n < 3``
ratings are down-weighted (weak evidence); unrated instructors are **not** 0.0 —
they sort after rated ones and are labelled ``unrated``. Tie-breaks: seats
desc, credits desc, earlier start, CRN (total order → deterministic output).
"""

from __future__ import annotations

import json
import os
import re
import shutil
import time as _time
from datetime import time
from pathlib import Path
from typing import Callable, Literal, Sequence

from pydantic import BaseModel, Field

from .audit.models import Audit
from .banner.search import TermSnapshot
from .catalog import (
    Availability,
    CourseKey,
    Priority,
    Requirement,
    applied_codes,
    availability_of,
    resolve,
    requirements_from_audit,
    term_label,
)
from .config import STATE_DIR
from .models import Section, meeting_start
from .schedule import Weekday, find_conflicts, render_grid

RATINGS_PATH = STATE_DIR / "ratings.json"
PREREQ_DIR = STATE_DIR / "prereqs"
LOW_CONFIDENCE_FACTOR = 0.85
LOW_CONFIDENCE_THRESHOLD = 3


class PlannerError(RuntimeError):
    pass


# --------------------------------------------------------------------------- #
# inputs
# --------------------------------------------------------------------------- #
class Constraints(BaseModel):
    max_credits: float = 16
    min_credits: float = 0
    avoid_days: list[Weekday] = Field(default_factory=list)
    earliest: time | None = None
    latest: time | None = None
    open_only: bool = True
    include_optional: bool = True
    exclude_arrangement: bool = False
    avoid_instructors: list[str] = Field(default_factory=list)
    #: Sections/courses the student must take this term. Pins are placed before
    #: requirement buckets and hard-fail (never silently dropped) when missing,
    #: excluded, conflicting or prerequisite-blocked.
    required_crns: list[str] = Field(default_factory=list)
    required_courses: list[str] = Field(default_factory=list)
    #: Sections/courses never to propose.
    excluded_crns: list[str] = Field(default_factory=list)
    excluded_courses: list[str] = Field(default_factory=list)
    #: CRN -> credit hours to reserve for variable-credit sections.
    credit_overrides: dict[str, float] = Field(default_factory=dict)


class RatingInfo(BaseModel):
    name: str
    matched_name: str | None = None
    rating: float | None = None
    difficulty: float | None = None
    num_ratings: int = 0
    confidence: float = 0.0
    source: str = "unrated"
    note: str = ""

    @property
    def rated(self) -> bool:
        return self.rating is not None and self.num_ratings > 0

    @property
    def effective_rating(self) -> float:
        if not self.rated:
            return 0.0
        factor = LOW_CONFIDENCE_FACTOR if self.num_ratings < LOW_CONFIDENCE_THRESHOLD else 1.0
        return round(float(self.rating) * factor, 4)

    @property
    def label(self) -> str:
        if not self.rated:
            return "unrated"
        return f"{self.rating:.1f}/5 (n={self.num_ratings})"


def _split_name(name: str) -> str:
    """Banner writes 'Last, First' — RMP wants 'First Last'."""
    if "," in name:
        last, _, first = name.partition(",")
        return f"{first.strip()} {last.strip()}".strip()
    return name.strip()


class RatingProvider:
    """RMP lookups with an on-disk cache (7-day TTL).

    Entries carry ``fetched_at`` so the TTL is real. Transient failures
    ("RMP unavailable: ...") are never cached, and legacy entries without a
    timestamp are discarded on load — an RMP outage can no longer mark a
    professor unrated forever.
    """

    def __init__(
        self,
        client=None,
        *,
        path: Path | None = None,
        ttl_seconds: int = 7 * 24 * 3600,
        lookup: Callable[[str], RatingInfo] | None = None,
        clock: Callable[[], float] = _time.time,
    ) -> None:
        self.path = Path(path) if path is not None else RATINGS_PATH
        self.ttl_seconds = ttl_seconds
        self._client = client
        self._lookup = lookup
        self._clock = clock
        self._memo: dict[str, RatingInfo] = {}
        self._cache = self._load()

    @staticmethod
    def _is_failure(info: RatingInfo) -> bool:
        return info.note.startswith("RMP unavailable")

    @staticmethod
    def parse_cache_entry(entry: object) -> tuple[dict, float] | None:
        """Normalize flat (current), nested (transitional) and legacy entries.

        Current format: all ``RatingInfo`` fields at the top level plus
        ``fetched_at``. The nested ``{"info": {...}, "fetched_at": ...}`` form is
        still read so old cache files and old processes remain compatible.
        A legacy entry without ``fetched_at`` is kept but treated as expired.
        """
        if not isinstance(entry, dict):
            return None
        if "info" in entry:
            payload = entry.get("info")
            if not isinstance(payload, dict):
                return None
            data = payload
            fetched_raw = entry.get("fetched_at", 0)
        else:
            data = {key: value for key, value in entry.items() if key != "fetched_at"}
            fetched_raw = entry.get("fetched_at", 0)
        try:
            info = RatingInfo(**data)
            fetched_at = float(fetched_raw or 0)
        except Exception:  # noqa: BLE001 - corrupt/legacy cache row
            return None
        if info.note.startswith("RMP unavailable"):
            return None
        return info.model_dump(), fetched_at

    def _load(self) -> dict[str, dict]:
        if not self.path.exists():
            return {}
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError, TypeError):
            return {}
        if not isinstance(raw, dict):
            return {}
        clean: dict[str, dict] = {}
        for key, entry in raw.items():
            parsed = self.parse_cache_entry(entry)
            if parsed is None:
                continue
            info, fetched_at = parsed
            clean[str(key)] = {**info, "fetched_at": fetched_at}
        return clean

    def _save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_name(self.path.name + ".tmp")
        # Flat format only: old processes can still construct RatingInfo from
        # this row (extra fetched_at is ignored); the nested form crashes them.
        tmp.write_text(json.dumps(self._cache, indent=1), encoding="utf-8")
        os.replace(tmp, self.path)

    def _fresh(self, entry: dict) -> bool:
        return self._clock() - float(entry.get("fetched_at", 0)) < self.ttl_seconds

    def get(self, instructor: str, refresh: bool = False) -> RatingInfo:
        key = instructor.strip()
        if not refresh:
            cached = self._cache.get(key)
            if cached and self._fresh(cached):
                info = RatingInfo(**{k: v for k, v in cached.items() if k != "fetched_at"})
                self._memo[key] = info
                return info
            # No fresh entry: a memoized success must not outlive the TTL, and a
            # memoized failure must never be served at all.
            self._memo.pop(key, None)
        info = self._lookup(key) if self._lookup else self._fetch(key)
        self._memo[key] = info
        if self._is_failure(info):
            self._cache.pop(key, None)
        else:
            self._cache[key] = {**info.model_dump(), "fetched_at": self._clock()}
        self._save()
        return info

    def _fetch(self, instructor: str) -> RatingInfo:
        generic = instructor.lower().split(",")[0].strip()
        if generic in {"faculty", "staff", "instructor"}:
            return RatingInfo(name=instructor, note="generic placeholder instructor")
        from .rmp.client import RMPError, RMPClient
        from .rmp.match import match_professor

        owns = self._client is None
        client = self._client or RMPClient()
        try:
            match = match_professor(_split_name(instructor), client=client)
        except (RMPError, OSError) as exc:
            return RatingInfo(name=instructor, note=f"RMP unavailable: {exc}")
        finally:
            if owns:
                client.close()  # type: ignore[attr-defined]
        professor = match.professor
        if professor is None:
            return RatingInfo(name=instructor, note=match.reason or "no RMP record")
        return RatingInfo(
            name=instructor,
            matched_name=professor.full_name,
            rating=professor.avg_rating,
            difficulty=professor.avg_difficulty,
            num_ratings=professor.num_ratings,
            confidence=round(match.confidence, 3),
            source="rmp",
        )


def migrate_ratings_file(path: Path) -> Path | None:
    """Rewrite ``path`` as flat rating entries, preserving entries in a backup.

    Applies to both the nested transitional format and legacy flat rows. Rated
    and unrated entries are kept; transient ``RMP unavailable`` failures are
    dropped. Returns the backup path, or ``None`` when there is nothing to do.
    """
    path = Path(path)
    if not path.exists():
        return None
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError, TypeError):
        return None
    if not isinstance(raw, dict):
        return None
    clean: dict[str, dict] = {}
    for key, entry in raw.items():
        parsed = RatingProvider.parse_cache_entry(entry)
        if parsed is None:
            continue
        info, fetched_at = parsed
        clean[str(key)] = {**info, "fetched_at": fetched_at}
    backup = path.with_name(path.name + ".bak")
    shutil.copy2(path, backup)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(clean, indent=1), encoding="utf-8")
    os.replace(tmp, path)
    return backup


# --------------------------------------------------------------------------- #
# prerequisites
# --------------------------------------------------------------------------- #
CODE_RE = re.compile(r"Course or Test:\s*([A-Za-z]{2,6})[^0-9]{0,60}?(\d{3,4}[A-Za-z]?)")
TOKEN_RE = re.compile(
    r"\s*(?:"
    r"(?P<code>Course or Test:\s*[A-Za-z]{2,6}[^0-9]{0,60}?\d{3,4}[A-Za-z]?)"
    r"|(?P<and>\band\b)|(?P<or>\bor\b)|(?P<open>\()|(?P<close>\))"
    r")",
    re.IGNORECASE,
)


class PrereqExpression(BaseModel):
    """Parsed prerequisite text (``and`` binds tighter than ``or``).

    ``parse_status`` is a tri-state gate, because "no prerequisites" and "text I
    could not parse" must never be conflated:

    * ``none``   -- Banner reports no prerequisite information (satisfied)
    * ``parsed`` -- at least one course code was extracted and a tree built
    * ``unknown`` -- non-empty text with no recognizable course code; callers
      must treat the prerequisite as unverified, not satisfied.
    """

    raw: str = ""
    codes: list[str] = Field(default_factory=list)
    tree: list = Field(default_factory=list)
    parse_status: Literal["none", "parsed", "unknown"] = "none"

    def satisfied_by(self, passed: set[str]) -> bool | None:
        if self.parse_status == "none":
            return True
        if self.parse_status != "parsed":
            return None
        return _evaluate(self.tree, passed)


def _evaluate(node: list, passed: set[str]) -> bool:
    operation = node[0]
    if operation == "CODE":
        return node[1] in passed
    if operation == "AND":
        return all(_evaluate(child, passed) for child in node[1])
    if operation == "OR":
        return any(_evaluate(child, passed) for child in node[1])
    return True


def _build_tree(tokens: list[tuple[str, str]]) -> list:
    """Build the boolean tree from ``(KIND, value)`` tokens, ``and`` > ``or``."""
    index = 0

    def parse_expression() -> list:
        nonlocal index
        while index < len(tokens) and tokens[index][0] == "OR":
            index += 1
        children = [parse_term()]
        while index < len(tokens) and tokens[index][0] == "OR":
            index += 1
            children.append(parse_term())
        return children[0] if len(children) == 1 else ["OR", children]

    def parse_term() -> list:
        nonlocal index
        while index < len(tokens) and tokens[index][0] == "AND":
            index += 1
        children = [parse_factor()]
        while index < len(tokens) and tokens[index][0] == "AND":
            index += 1
            children.append(parse_factor())
        return children[0] if len(children) == 1 else ["AND", children]

    def parse_factor() -> list:
        nonlocal index
        while index < len(tokens) and tokens[index][0] not in {"(", "CODE"}:
            index += 1
        if index < len(tokens) and tokens[index][0] == "(":
            index += 1
            node = parse_expression()
            if index < len(tokens) and tokens[index][0] == ")":
                index += 1
            return node
        if index < len(tokens) and tokens[index][0] == "CODE":
            node = ["CODE", tokens[index][1]]
            index += 1
            return node
        if index < len(tokens):
            index += 1
        return ["CODE", "__unparsed__"]

    return parse_expression()


def _banner_tokens(body: str) -> list[tuple[str, str]]:
    """Tokenize Banner's structured ``Course or Test:`` / and / or / () text."""
    tokens: list[tuple[str, str]] = []
    position = 0
    while position < len(body):
        match = TOKEN_RE.match(body, position)
        if not match:
            position += 1
            continue
        position = match.end()
        if match.group("code"):
            found = CODE_RE.search(match.group("code"))
            if found:
                tokens.append(("CODE", f"{found.group(1).upper()} {found.group(2).upper()}"))
        elif match.group("and"):
            tokens.append(("AND", "and"))
        elif match.group("or"):
            tokens.append(("OR", "or"))
        elif match.group("open"):
            tokens.append(("(", "("))
        elif match.group("close"):
            tokens.append((")", ")"))
    return tokens


#: Fallback for catalog prose ("Prerequisite: CYS 3065", "PQ: COS 2015 or COS 2112").
#: Uppercase-only subjects keep words like "in 2016" out of the result.
PROSE_CODE_RE = re.compile(r"(?<![A-Za-z])([A-Z]{2,6})\s*-?\s*(\d{3,4}[A-Za-z]?)(?![A-Za-z0-9])")
PROSE_LIST_RE = re.compile(
    r"\s*(?:(?:,|/)\s*(?:and|or)?|(?:and|or))\s*(\d{3,4}[A-Za-z]?)(?![A-Za-z0-9])"
)
PROSE_TARGET_RE = re.compile(
    r"\b(?:PQ|Prerequisites?)\s+for\s+([A-Za-z]{2,6})\s*-?\s*(\d{3,4}[A-Za-z]?)",
    re.IGNORECASE,
)
PROSE_BLOCKLIST = {"PQ", "AND", "OR", "THE", "FOR", "MIN", "GPA"}


def _prose_tokens(text: str) -> list[tuple[str, str]]:
    """Best-effort extraction from prose when no ``Course or Test:`` text exists."""
    target = PROSE_TARGET_RE.search(text)
    target_code = (
        f"{target.group(1).upper()} {target.group(2).upper()}" if target else None
    )
    found: list[tuple[int, int, str]] = []
    for match in PROSE_CODE_RE.finditer(text):
        subject = match.group(1).upper()
        if subject in PROSE_BLOCKLIST:
            continue
        code = f"{subject} {match.group(2).upper()}"
        if code == target_code:
            # "PQ for CYS 4855" names the section being described, not a prereq.
            continue
        found.append((match.start(), match.end(), code))
        position = match.end()
        while True:
            item = PROSE_LIST_RE.match(text, position)
            if not item:
                break
            found.append((item.start(1), item.end(), f"{subject} {item.group(1).upper()}"))
            position = item.end()

    ordered: list[tuple[int, int, str]] = []
    seen: set[str] = set()
    for entry in sorted(found, key=lambda row: row[0]):
        if entry[2] in seen:
            continue
        seen.add(entry[2])
        ordered.append(entry)

    tokens: list[tuple[str, str]] = []
    previous_end: int | None = None
    for start, end, code in ordered:
        if previous_end is not None:
            between = text[previous_end:start].lower()
            connector = "OR" if (" or " in between or "," in between or "/" in between) else "AND"
            tokens.append((connector, connector.lower()))
        tokens.append(("CODE", code))
        previous_end = end
    return tokens


def parse_prerequisites(text: str) -> PrereqExpression:
    """Parse Banner prerequisite text into a tri-state :class:`PrereqExpression`."""
    raw = " ".join(text.split())
    if not raw or "no prerequisite information available" in raw.lower():
        return PrereqExpression(raw=raw, parse_status="none")
    marker = raw.find("Prerequisites:")
    body = raw[marker + len("Prerequisites:"):] if marker >= 0 else raw

    structured = _banner_tokens(body)
    if any(kind == "CODE" for kind, _ in structured):
        return PrereqExpression(
            raw=raw,
            codes=[value for kind, value in structured if kind == "CODE"],
            tree=_build_tree(structured),
            parse_status="parsed",
        )

    prose = _prose_tokens(raw)
    if prose:
        return PrereqExpression(
            raw=raw,
            codes=[value for kind, value in prose if kind == "CODE"],
            tree=_build_tree(prose),
            parse_status="parsed",
        )
    return PrereqExpression(raw=raw, parse_status="unknown")


class PrereqProvider:
    """Fetch + cache Banner prereq text and decide whether a section is allowed."""

    def __init__(
        self,
        ssb=None,
        *,
        directory: Path | None = None,
        fetcher: Callable[[Section], str] | None = None,
    ) -> None:
        self.ssb = ssb
        self.directory = Path(directory) if directory is not None else PREREQ_DIR
        self.fetcher = fetcher

    def _path(self, term: str, crn: str) -> Path:
        return self.directory / f"{term}_{crn}.json"

    def expression(self, section: Section) -> PrereqExpression:
        path = self._path(section.term, section.crn)
        if path.exists():
            try:
                cached = json.loads(path.read_text(encoding="utf-8"))
            except (json.JSONDecodeError, TypeError):
                cached = None
            if cached and cached.get("raw"):
                # Re-parse cached text so parser fixes apply without a refetch.
                return parse_prerequisites(cached["raw"])
            if cached:
                if not cached.get("parse_status"):
                    cached["parse_status"] = "parsed" if cached.get("tree") else "none"
                return PrereqExpression(**cached)
        if self.fetcher is not None:
            text = self.fetcher(section)
        else:
            if self.ssb is None:
                from .auth import ensure
                from .banner.ssb import SSBClient

                self.ssb = SSBClient(ensure())
            text = " ".join(self.ssb.prerequisites(section.crn, section.term))
        expression = parse_prerequisites(text)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(expression.model_dump_json(), encoding="utf-8")
        return expression

    def check(self, section: Section, passed: set[str]) -> tuple[bool | None, str]:
        """``True`` satisfied, ``False`` blocked, ``None`` unverifiable.

        A fetch failure or unparsable non-empty text returns ``None``; callers
        must not treat that as an allowed prerequisite.
        """
        try:
            expression = self.expression(section)
        except Exception as exc:  # noqa: BLE001 - never fail planning on fetch errors
            return None, f"prerequisite text unavailable ({exc.__class__.__name__})"
        # A section tagged PREQ promises a prerequisite even when Banner returns
        # no text (or only "No prerequisite information available"). Treat that
        # as unverified, not as "no prerequisite".
        if "PREQ" in section.attribute_codes and expression.parse_status == "none":
            return None, "PREQ attribute present but Banner returned no prerequisite text"
        result = expression.satisfied_by(passed)
        if result is True:
            return True, ""
        if result is None:
            snippet = " ".join(expression.raw.split())[:160]
            return None, f"prerequisite text could not be parsed ({snippet!r})"
        missing = [code for code in expression.codes if code not in passed]
        return False, "needs " + (", ".join(missing) or "an unmet prerequisite")


# --------------------------------------------------------------------------- #
# output models
# --------------------------------------------------------------------------- #
class Selection(BaseModel):
    requirement_id: str
    requirement: str
    group: str
    crn: str
    course: str
    section: str
    title: str
    instructor: str
    rating: RatingInfo
    times: str
    days: str
    credits: float | None = None
    seats: int | None = None
    open: bool = False
    availability: str = "open"
    verify_at_registration: bool = False
    requires_arrangement: bool = False
    meets: list[str] = Field(default_factory=list)
    covers: list[str] = Field(default_factory=list)
    rationale: str = ""
    #: True when Banner's prerequisite text could not be fetched or parsed. The
    #: section is still placed (unknown is not proof of failure) but must be
    #: checked before registration.
    prereq_unverified: bool = False


class Unplaced(BaseModel):
    requirement_id: str
    requirement: str
    group: str
    reason: str


class Pinned(BaseModel):
    """Outcome of one required CRN/course the student insisted on."""

    request: str
    kind: Literal["crn", "course"]
    outcome: Literal[
        "placed", "unavailable", "conflict", "prereq_blocked", "excluded", "budget"
    ]
    crn: str | None = None
    course: str | None = None
    requirement_ids: list[str] = Field(default_factory=list)
    reason: str = ""

    @property
    def ok(self) -> bool:
        return self.outcome == "placed"


class DraftPlan(BaseModel):
    term: str
    label: str = ""
    selections: list[Selection] = Field(default_factory=list)
    unplaced_requirements: list[Unplaced] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)
    errors: list[str] = Field(default_factory=list)
    pinned: list[Pinned] = Field(default_factory=list)
    grid: str = ""
    total_credits: float = 0.0
    score_breakdown: list[str] = Field(default_factory=list)
    min_credits: float = 0.0
    min_credits_met: bool = True
    credit_overrides: dict[str, float] = Field(default_factory=dict)

    @property
    def crns(self) -> list[str]:
        return [selection.crn for selection in self.selections]


class RoadmapTerm(BaseModel):
    term: str
    label: str = ""
    status: str = "planned"
    draft: DraftPlan | None = None
    remaining: list[str] = Field(default_factory=list)
    notes: list[str] = Field(default_factory=list)


class Roadmap(BaseModel):
    start_term: str
    terms: list[RoadmapTerm] = Field(default_factory=list)
    final_remaining: list[str] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)


# --------------------------------------------------------------------------- #
# planner
# --------------------------------------------------------------------------- #
class Planner:
    def __init__(
        self,
        audit: Audit,
        snapshots: dict[str, TermSnapshot],
        *,
        ratings: RatingProvider | None = None,
        prereqs: PrereqProvider | None = None,
        terms_order: Sequence[str] | None = None,
        current_term: str = "202610",
    ) -> None:
        self.audit = audit
        self.snapshots = snapshots
        self.ratings = ratings or RatingProvider()
        self.prereqs = prereqs or PrereqProvider()
        self.requirements = [r for r in requirements_from_audit(audit) if not r.satisfied]
        self.passed = applied_codes(audit)
        self.terms_order = list(terms_order or sorted(snapshots))
        self.current_term = current_term

    # ------------------------------------------------------------------ ratings
    def _best_instructor(self, section: Section, constraints: Constraints) -> tuple[str, RatingInfo]:
        people = [person.name for person in section.faculty if person.name]
        if not people:
            return "Staff", RatingInfo(name="Staff", note="no instructor assigned")
        avoided = {name.lower() for name in constraints.avoid_instructors}
        considered = [name for name in people if name.lower() not in avoided] or people
        best_name = considered[0]
        best = self.ratings.get(best_name)
        for name in considered[1:]:
            info = self.ratings.get(name)
            if _rating_key(info) < _rating_key(best):
                best_name, best = name, info
        return best_name, best

    def _rank(
        self, section: Section, constraints: Constraints
    ) -> tuple[tuple, str, RatingInfo]:
        name, rating = self._best_instructor(section, constraints)
        key = (
            *_rating_key(rating),
            -(section.seats_available if section.seats_available is not None else -1),
            -(self._credits(section, constraints)),
            1 if section.unscheduled else 0,
            meeting_start(section),
            section.crn,
        )
        return key, name, rating

    # -------------------------------------------------------------------- hard
    @staticmethod
    def _credits(section: Section, constraints: Constraints) -> float:
        """Credits to reserve for one section (override > low end > high end)."""
        override = constraints.credit_overrides.get(str(section.crn))
        if override is not None:
            return float(override)
        return float(section.planning_credits or 0.0)

    @staticmethod
    def _excluded_sets(constraints: Constraints) -> tuple[set[str], set[str]]:
        return (
            {str(crn).strip() for crn in constraints.excluded_crns if str(crn).strip()},
            {str(code).strip().upper() for code in constraints.excluded_courses if str(code).strip()},
        )

    @staticmethod
    def _matches_requirement(section: Section, requirement: Requirement) -> bool:
        """Mirror :func:`catalog.resolve` matching for pinned-section coverage."""
        if requirement.all_of and not set(requirement.all_of) <= section.attribute_codes:
            return False
        if requirement.subjects and section.subject.upper() not in {
            subject.upper() for subject in requirement.subjects
        }:
            return False
        if section.code.upper() in {target.code.upper() for target in requirement.targets}:
            return True
        if requirement.attributes and set(requirement.attributes) & section.attribute_codes:
            return True
        return bool(
            requirement.subject_wildcards
            and section.subject.upper()
            in {wildcard.upper() for wildcard in requirement.subject_wildcards}
        )

    @staticmethod
    def _pin_failure_outcome(reason: str) -> str:
        lowered = reason.lower()
        if lowered.startswith("needs ") or "prerequisite" in lowered:
            return "prereq_blocked"
        if lowered.startswith("budget"):
            return "budget"
        return "conflict"

    def _record_pin(
        self,
        plan: DraftPlan,
        request: str,
        kind: str,
        outcome: str,
        crn: str | None,
        course: str | None,
        requirement_ids: Sequence[str],
        reason: str,
    ) -> None:
        plan.pinned.append(
            Pinned(
                request=request,
                kind=kind,  # type: ignore[arg-type]
                outcome=outcome,  # type: ignore[arg-type]
                crn=crn,
                course=course,
                requirement_ids=list(requirement_ids),
                reason=reason,
            )
        )
        if outcome != "placed":
            plan.errors.append(f"pinned {kind} {request!r} was not placed ({outcome}): {reason}")

    def _fits(
        self,
        section: Section,
        selected: Sequence[Section],
        credits_used: float,
        constraints: Constraints,
        passed: set[str],
    ) -> tuple[bool, str, bool]:
        """Return ``(allowed, reason, prereq_unverified)``."""
        credits = self._credits(section, constraints)
        if credits_used + credits > constraints.max_credits + 1e-6:
            return False, f"budget: {credits_used:g}+{credits:g} exceeds {constraints.max_credits:g} credits", False
        if set(section.days) & set(constraints.avoid_days):
            blocked = ", ".join(day.value for day in set(section.days) & set(constraints.avoid_days))
            return False, f"meets on avoided day {blocked}", False
        avoided = {name.strip().lower() for name in constraints.avoid_instructors if name.strip()}
        if avoided:
            people = [person.name.strip().lower() for person in section.faculty if person.name]
            if people and all(person in avoided for person in people):
                names = ", ".join(person.name for person in section.faculty if person.name)
                return False, f"taught only by avoided instructor(s): {names}", False
        if constraints.earliest or constraints.latest:
            for meeting in section.meetings:
                if not meeting.scheduled:
                    continue
                if constraints.earliest and meeting.start < constraints.earliest:
                    return False, f"starts before {constraints.earliest.strftime('%H:%M')}", False
                if constraints.latest and meeting.end > constraints.latest:
                    return False, f"ends after {constraints.latest.strftime('%H:%M')}", False
        clash = find_conflicts([s.to_schedule_section() for s in selected] + [section.to_schedule_section()])
        if clash:
            return False, f"time conflict: {clash[0].summary}", False
        ok, reason = self.prereqs.check(section, passed)
        if ok is False:
            return False, reason, False
        return True, reason, ok is None

    # ------------------------------------------------------------------- draft
    def draft(
        self,
        term: str,
        constraints: Constraints | None = None,
        *,
        requirements: Sequence[Requirement] | None = None,
        passed: set[str] | None = None,
    ) -> DraftPlan:
        constraints = constraints or Constraints()
        passed = set(self.passed if passed is None else passed)
        snapshot = self.snapshots.get(term)
        if snapshot is None:
            raise PlannerError(f"No catalog snapshot for term {term}. Fetch it first.")
        pool = [r for r in (requirements if requirements is not None else self.requirements) if not r.satisfied]

        plan = DraftPlan(
            term=term,
            label=term_label(term),
            min_credits=constraints.min_credits,
            credit_overrides={str(crn): float(hours) for crn, hours in constraints.credit_overrides.items()},
        )
        selected: list[Section] = []
        selected_codes: set[str] = set()
        by_crn: dict[str, Selection] = {}
        #: requirement id -> [credits already covered by pins, pick count]
        pin_progress: dict[str, list[float]] = {}
        unrated_picks = 0
        rated_picks: list[RatingInfo] = []

        self._place_pins(
            plan, snapshot, constraints, selected, selected_codes, by_crn, passed, pool, pin_progress
        )
        credits_used = round(sum(self._credits(section, constraints) for section in selected), 2)

        # Priority buckets: budget is allocated in this order, rating is the
        # tie-break inside a bucket (never across buckets).
        buckets: dict[Priority, list[tuple[tuple, Requirement, object]]] = {
            priority: [] for priority in Priority
        }
        for requirement in pool:
            if requirement.optional and not constraints.include_optional:
                plan.unplaced_requirements.append(
                    Unplaced(
                        requirement_id=requirement.id,
                        requirement=requirement.label,
                        group=requirement.parent_group,
                        reason="optional/elective pool skipped by constraints",
                    )
                )
                continue
            if requirement.requires_arrangement and constraints.exclude_arrangement:
                plan.unplaced_requirements.append(
                    Unplaced(
                        requirement_id=requirement.id,
                        requirement=requirement.label,
                        group=requirement.parent_group,
                        reason="excluded by constraint (arranged internship/research/capstone)",
                    )
                )
                continue
            resolution = resolve(
                requirement,
                snapshot,
                exclude_codes=passed,
                open_only=constraints.open_only,
                current_term=self.current_term,
            )
            best_key = min(
                (self._rank(section, constraints)[0] for section in resolution.candidates),
                default=(9, 0.0),
            )
            buckets[requirement.priority].append((best_key, requirement, resolution))

        for priority in Priority:
            for _, requirement, resolution in sorted(buckets[priority], key=lambda item: item[0]):
                self._place_requirement(
                    plan, requirement, resolution, snapshot, constraints,
                    selected, selected_codes, by_crn, credits_used, passed, pin_progress,
                )
                credits_used = round(sum(self._credits(section, constraints) for section in selected), 2)
                if credits_used > constraints.max_credits + 1e-6:
                    raise PlannerError("credit accounting exceeded max_credits")

        self._fill_to_minimum(
            plan, pool, snapshot, constraints, selected, selected_codes, by_crn, passed
        )

        for selection in plan.selections:
            if selection.rating.rated:
                rated_picks.append(selection.rating)
            else:
                unrated_picks += 1
        if any(selection.verify_at_registration for selection in plan.selections):
            plan.warnings.append(
                "some sections are not open yet (future term); verify seats when registration opens"
            )
        for selection in plan.selections:
            section = snapshot.by_crn(selection.crn)
            if section is None:
                continue
            low, high = section.credits_low, section.credits_high
            if (
                low is not None
                and high is not None
                and high != low
                and str(section.crn) not in constraints.credit_overrides
            ):
                plan.warnings.append(
                    f"{section.code} CRN {section.crn}: variable credit "
                    f"{low:g}-{high:g}; reserved {section.planning_credits:g} cr (low end). "
                    f"Set hours for CRN {section.crn} to reserve more."
                )
        plan.total_credits = round(
            sum(self._credits(section, constraints) for section in selected), 2
        )
        if constraints.min_credits:
            plan.min_credits_met = plan.total_credits + 1e-9 >= constraints.min_credits
        plan.grid = render_grid(
            [section.to_schedule_section() for section in selected], include_tba=True
        ) if selected else ""
        plan.score_breakdown = self._score_breakdown(plan, rated_picks, unrated_picks, constraints)
        return plan

    def _place_pins(
        self,
        plan: DraftPlan,
        snapshot: TermSnapshot,
        constraints: Constraints,
        selected: list[Section],
        selected_codes: set[str],
        by_crn: dict[str, Selection],
        passed: set[str],
        pool: Sequence[Requirement],
        pin_progress: dict[str, list[float]],
    ) -> None:
        """Place the student's explicitly required CRNs/courses before anything else."""
        requests: list[tuple[str, str]] = [
            ("crn", str(crn).strip()) for crn in constraints.required_crns if str(crn).strip()
        ]
        requests += [
            ("course", str(code).strip()) for code in constraints.required_courses if str(code).strip()
        ]
        excluded_crns, excluded_courses = self._excluded_sets(constraints)
        credits_used = round(sum(self._credits(section, constraints) for section in selected), 2)

        for kind, request in requests:
            prereq_unverified = False
            if kind == "crn":
                section = snapshot.by_crn(request)
                if section is None:
                    self._record_pin(
                        plan, request, kind, "unavailable", None, None, [],
                        f"CRN {request} is not offered in {snapshot.term}",
                    )
                    continue
                if request in excluded_crns or section.code.upper() in excluded_courses:
                    self._record_pin(
                        plan, request, kind, "excluded", request, section.code, [],
                        "the CRN or its course is in the exclusion list",
                    )
                    continue
                if request in by_crn:
                    existing = by_crn[request]
                    self._record_pin(
                        plan, request, kind, "placed", request, section.code,
                        list(existing.covers), "already placed by an earlier pin",
                    )
                    continue
                allowed, reason, prereq_unverified = self._fits(
                    section, selected, credits_used, constraints, passed
                )
                if not allowed:
                    self._record_pin(
                        plan, request, kind, self._pin_failure_outcome(reason), request,
                        section.code, [], reason,
                    )
                    continue
                chosen = section
            else:
                try:
                    key = CourseKey.parse(request)
                except ValueError as exc:
                    self._record_pin(
                        plan, request, kind, "unavailable", None, None, [], str(exc)
                    )
                    continue
                if key.code.upper() in excluded_courses:
                    self._record_pin(
                        plan, request, kind, "excluded", None, key.code, [],
                        "the course is in the exclusion list",
                    )
                    continue
                candidates = [
                    candidate
                    for candidate in snapshot.by_course(key.subject, key.number)
                    if str(candidate.crn) not in excluded_crns
                ]
                if not candidates:
                    self._record_pin(
                        plan, request, kind, "unavailable", None, key.code, [],
                        f"{key.code} is not offered in {snapshot.term}",
                    )
                    continue
                chosen = None
                first_failure = ""
                for candidate in sorted(candidates, key=lambda item: self._rank(item, constraints)[0]):
                    if candidate.crn in by_crn:
                        chosen = candidate
                        prereq_unverified = False
                        break
                    allowed, reason, unknown = self._fits(
                        candidate, selected, credits_used, constraints, passed
                    )
                    if allowed:
                        chosen = candidate
                        prereq_unverified = unknown
                        break
                    first_failure = first_failure or reason
                if chosen is None:
                    outcome = self._pin_failure_outcome(first_failure or "no eligible section")
                    self._record_pin(
                        plan, request, kind, outcome, None, key.code, [],
                        first_failure or "no eligible section matched the constraints",
                    )
                    continue

            if chosen.crn in by_crn:
                existing = by_crn[chosen.crn]
                self._record_pin(
                    plan, request, kind, "placed", chosen.crn, chosen.code,
                    list(existing.covers), "already placed by an earlier pin",
                )
                continue

            state = availability_of(chosen, self.current_term)
            if not chosen.open and state is Availability.CLOSED:
                plan.warnings.append(
                    f"pinned {chosen.code} CRN {chosen.crn} is closed/full in {plan.term}; verify before registering"
                )
            if state is not Availability.OPEN:
                plan.warnings.append(
                    f"pinned {chosen.code} CRN {chosen.crn} is not open yet in {plan.term}; verify at registration"
                )
            _, instructor, rating = self._rank(chosen, constraints)
            matched = [requirement for requirement in pool if self._matches_requirement(chosen, requirement)]
            primary = matched[0] if matched else None
            credits = self._credits(chosen, constraints)
            selection = Selection(
                requirement_id=primary.id if primary else "pin",
                requirement=primary.label if primary else "pinned by request",
                group=primary.parent_group if primary else "pins",
                crn=chosen.crn,
                course=chosen.code,
                section=chosen.section,
                title=chosen.title,
                instructor=instructor,
                rating=rating,
                times=chosen.time_label,
                days=chosen.day_label,
                credits=credits,
                seats=chosen.seats_available,
                open=chosen.open,
                availability=state.value,
                verify_at_registration=state is not Availability.OPEN,
                requires_arrangement=bool(primary and primary.requires_arrangement),
                meets=[
                    f"{meeting.day.value} {meeting.start.strftime('%H:%M')}-{meeting.end.strftime('%H:%M')} {meeting.location}".strip()
                    for meeting in chosen.meetings
                    if meeting.scheduled
                ],
                covers=[requirement.id for requirement in matched],
                rationale="pinned by request"
                + (f"; {self._rationale(primary, chosen, instructor, rating, state)}" if primary else ""),
                prereq_unverified=prereq_unverified,
            )
            plan.selections.append(selection)
            by_crn[chosen.crn] = selection
            selected_codes.add(chosen.code)
            selected.append(chosen)
            credits_used = round(credits_used + credits, 2)
            if prereq_unverified:
                plan.warnings.append(
                    f"pinned {chosen.code} CRN {chosen.crn}: prerequisite could not be "
                    "verified; confirm before registering"
                )
            for requirement in matched:
                bucket = pin_progress.setdefault(requirement.id, [0.0, 0.0])
                bucket[0] += credits
                bucket[1] += 1
            self._record_pin(
                plan, request, kind, "placed", chosen.crn, chosen.code,
                [requirement.id for requirement in matched], "",
            )

    def _place_requirement(
        self,
        plan: DraftPlan,
        requirement: Requirement,
        resolution,
        snapshot: TermSnapshot,
        constraints: Constraints,
        selected: list[Section],
        selected_codes: set[str],
        by_crn: dict[str, Selection],
        credits_used: float,
        passed: set[str],
        pin_progress: dict[str, list[float]],
    ) -> None:
        excluded_crns, excluded_courses = self._excluded_sets(constraints)
        candidates = sorted(
            (
                section
                for section in resolution.candidates
                if str(section.crn) not in excluded_crns
                and section.code.upper() not in excluded_courses
            ),
            key=lambda section: self._rank(section, constraints)[0],
        )
        if not candidates:
            reason = resolution.reason or "no candidate sections"
            if resolution.candidates:
                reason = "every candidate section is excluded by constraints"
            plan.unplaced_requirements.append(
                Unplaced(
                    requirement_id=requirement.id,
                    requirement=requirement.label,
                    group=requirement.parent_group,
                    reason=reason,
                )
            )
            return
        pin_credits, pin_count = (pin_progress.get(requirement.id) or [0.0, 0.0])[:2]
        placed_for_requirement = int(pin_count)
        needed = requirement.pick_n_of or (len(candidates) if requirement.min_credits else 1)
        credits_needed = requirement.min_credits
        covered_credits = requirement.applied_credits + float(pin_credits)
        if requirement.pick_n_of is not None:
            needed = max(0, requirement.pick_n_of - requirement.applied_count)

        for section in candidates:
            if placed_for_requirement >= needed:
                break
            credits = self._credits(section, constraints)
            if credits_needed is not None:
                if covered_credits >= credits_needed:
                    break
                if not credits:
                    continue
            if section.crn in by_crn:
                existing = by_crn[section.crn]
                if requirement.id in existing.covers:
                    # Already counted when the section was placed as a pin.
                    continue
                existing.covers.append(requirement.id)
                covered_credits += existing.credits or 0
                placed_for_requirement += 1
                continue
            if section.code in selected_codes:
                continue
            fits, reason, prereq_unverified = self._fits(
                section, selected, credits_used, constraints, passed
            )
            if not fits:
                continue
            _, instructor, rating = self._rank(section, constraints)
            state = availability_of(section, self.current_term)
            selection = Selection(
                requirement_id=requirement.id,
                requirement=requirement.label,
                group=requirement.parent_group,
                crn=section.crn,
                course=section.code,
                section=section.section,
                title=section.title,
                instructor=instructor,
                rating=rating,
                times=section.time_label,
                days=section.day_label,
                credits=credits,
                seats=section.seats_available,
                open=section.open,
                availability=state.value,
                verify_at_registration=state is not Availability.OPEN,
                requires_arrangement=requirement.requires_arrangement,
                meets=[
                    f"{m.day.value} {m.start.strftime('%H:%M')}-{m.end.strftime('%H:%M')} {m.location}".strip()
                    for m in section.meetings
                    if m.scheduled
                ],
                covers=[requirement.id],
                rationale=self._rationale(requirement, section, instructor, rating, state),
                prereq_unverified=prereq_unverified,
            )
            plan.selections.append(selection)
            by_crn[section.crn] = selection
            selected_codes.add(section.code)
            selected.append(section)
            credits_used += credits
            covered_credits += credits
            placed_for_requirement += 1
            if prereq_unverified:
                plan.warnings.append(
                    f"{section.code} CRN {section.crn}: prerequisite could not be verified "
                    f"({reason}); confirm before registering"
                )
            if state is not Availability.OPEN:
                plan.warnings.append(
                    f"{section.code} CRN {section.crn} is not open yet in {plan.term}; verify at registration"
                )
            if requirement.requires_arrangement:
                plan.warnings.append(
                    f"{section.code} CRN {section.crn} requires an arranged faculty sponsor "
                    "and credit hours set; confirm with the department"
                )

        if placed_for_requirement == 0:
            _, why, _ = self._fits(candidates[0], selected, credits_used, constraints, passed)
            plan.unplaced_requirements.append(
                Unplaced(
                    requirement_id=requirement.id,
                    requirement=requirement.label,
                    group=requirement.parent_group,
                    reason=why or "offered but blocked by constraints",
                )
            )
        elif requirement.min_credits is not None and covered_credits + 1e-9 < requirement.min_credits:
            plan.unplaced_requirements.append(
                Unplaced(
                    requirement_id=requirement.id,
                    requirement=requirement.label,
                    group=requirement.parent_group,
                    reason=(
                        f"partial: placed {covered_credits:g} of {requirement.min_credits:g} "
                        f"required credits in {plan.term}"
                    ),
                )
            )

    def _fill_to_minimum(
        self,
        plan: DraftPlan,
        pool: Sequence[Requirement],
        snapshot: TermSnapshot,
        constraints: Constraints,
        selected: list[Section],
        selected_codes: set[str],
        by_crn: dict[str, Selection],
        passed: set[str],
    ) -> None:
        """Top up to ``min_credits`` from elective/optional pools only.

        Never invents courses: every added section is a real open catalog section
        that passes every hard constraint.
        """
        target = constraints.min_credits
        if target <= 0:
            return
        excluded_crns, excluded_courses = self._excluded_sets(constraints)
        credits_used = round(sum(self._credits(section, constraints) for section in selected), 2)
        if credits_used + 1e-9 >= target:
            plan.min_credits_met = True
            return

        ordered = sorted(
            (
                requirement
                for requirement in pool
                if requirement.optional
                or requirement.priority in {Priority.MAJOR_POOL, Priority.GEN_ED_FILL}
            ),
            key=lambda requirement: (
                0 if requirement.optional else 1,
                list(Priority).index(requirement.priority),
                requirement.id,
            ),
        )
        for requirement in ordered:
            if credits_used + 1e-9 >= target:
                break
            resolution = resolve(
                requirement,
                snapshot,
                exclude_codes=passed,
                open_only=constraints.open_only,
                current_term=self.current_term,
            )
            for section in sorted(
                resolution.candidates, key=lambda item: self._rank(item, constraints)[0]
            ):
                if credits_used + 1e-9 >= target:
                    break
                if str(section.crn) in excluded_crns or section.code.upper() in excluded_courses:
                    continue
                if section.crn in by_crn or section.code in selected_codes:
                    continue
                credits = self._credits(section, constraints)
                if not credits:
                    continue
                fits, reason, prereq_unverified = self._fits(
                    section, selected, credits_used, constraints, passed
                )
                if not fits:
                    continue
                _, instructor, rating = self._rank(section, constraints)
                state = availability_of(section, self.current_term)
                selection = Selection(
                    requirement_id=requirement.id,
                    requirement=requirement.label,
                    group=requirement.parent_group,
                    crn=section.crn,
                    course=section.code,
                    section=section.section,
                    title=section.title,
                    instructor=instructor,
                    rating=rating,
                    times=section.time_label,
                    days=section.day_label,
                    credits=credits,
                    seats=section.seats_available,
                    open=section.open,
                    availability=state.value,
                    verify_at_registration=state is not Availability.OPEN,
                    requires_arrangement=requirement.requires_arrangement,
                    meets=[
                        f"{m.day.value} {m.start.strftime('%H:%M')}-{m.end.strftime('%H:%M')} {m.location}".strip()
                        for m in section.meetings
                        if m.scheduled
                    ],
                    covers=[requirement.id],
                    rationale=(
                        f"fills the {target:g}-credit minimum load; "
                        + self._rationale(requirement, section, instructor, rating, state)
                    ),
                    prereq_unverified=prereq_unverified,
                )
                plan.selections.append(selection)
                by_crn[section.crn] = selection
                selected_codes.add(section.code)
                selected.append(section)
                credits_used = round(credits_used + credits, 2)
                if prereq_unverified:
                    plan.warnings.append(
                        f"{section.code} CRN {section.crn}: prerequisite could not be verified "
                        f"({reason}); confirm before registering"
                    )
        plan.min_credits_met = credits_used + 1e-9 >= target
        if not plan.min_credits_met:
            plan.warnings.append(
                f"minimum load not met: placed {credits_used:g} of {target:g} credits; "
                "no additional eligible sections"
            )

    def _rationale(
        self,
        requirement: Requirement,
        section: Section,
        instructor: str,
        rating: RatingInfo,
        state: Availability = Availability.OPEN,
    ) -> str:
        bits = []
        if requirement.major_specific:
            bits.append("required for the major")
        elif requirement.min_credits:
            bits.append(f"fills {requirement.min_credits:g} credits of {requirement.label}")
        else:
            bits.append(f"satisfies {requirement.label}")
        if rating.rated:
            bits.append(f"RMP {rating.rating:.1f}/5 from {rating.num_ratings} ratings")
        else:
            bits.append("no RMP record (ranked below rated instructors)")
        if requirement.all_of:
            bits.append("attributes " + "+".join(sorted(set(requirement.all_of) | section.attribute_codes)))
        if requirement.requires_arrangement:
            bits.append("needs arranged faculty sponsor / credit hours set (verify with department)")
        if state is not Availability.OPEN:
            bits.append("NOT yet open for registration (verify at registration)")
        elif section.seats_available is not None:
            bits.append(f"{section.seats_available} seats open")
        return "; ".join(bits)

    def _score_breakdown(
        self,
        plan: DraftPlan,
        rated_picks: list[RatingInfo],
        unrated_picks: int,
        constraints: Constraints,
    ) -> list[str]:
        lines = [
            f"{len(plan.selections)} section(s), {plan.total_credits:g}/{constraints.max_credits:g} credits",
            f"{len(rated_picks)} rated instructor pick(s), {unrated_picks} unrated",
        ]
        if rated_picks:
            average = sum(r.rating or 0 for r in rated_picks) / len(rated_picks)
            lines.append(f"average RMP rating of rated picks: {average:.2f}/5")
            lines.append(
                "weights: effective_rating = rating × "
                f"{LOW_CONFIDENCE_FACTOR} when n < {LOW_CONFIDENCE_THRESHOLD}; "
                "priority buckets: major_required > major_pool > gen_ed_required > gen_ed_fill"
            )
        if plan.unplaced_requirements:
            lines.append(f"{len(plan.unplaced_requirements)} requirement(s) could not be placed")
        if constraints.min_credits:
            lines.append(
                f"minimum load {plan.total_credits:g}/{constraints.min_credits:g} credits "
                f"({'met' if plan.min_credits_met else 'NOT met'})"
            )
            if constraints.min_credits > constraints.max_credits + 1e-9:
                lines.append(
                    f"min_credits {constraints.min_credits:g} exceeds max_credits "
                    f"{constraints.max_credits:g}; it cannot be met"
                )
        if plan.pinned:
            placed = sum(1 for pin in plan.pinned if pin.outcome == "placed")
            lines.append(f"{placed}/{len(plan.pinned)} pinned section(s) placed")
        return lines

    def roadmap(
        self,
        terms: Sequence[str],
        constraints: Constraints | None = None,
        *,
        include_summer: bool = False,
    ) -> Roadmap:
        """Run the draft term by term, carrying completed requirements forward."""
        constraints = constraints or Constraints()
        if not include_summer:
            terms = [term for term in terms if not term_label(term).startswith("Summer")]
        remaining = [requirement for requirement in self.requirements if not requirement.satisfied]
        progress: dict[str, float] = {
            requirement.id: requirement.applied_credits for requirement in self.requirements
        }
        counts: dict[str, int] = {
            requirement.id: requirement.applied_count for requirement in self.requirements
        }
        passed = set(self.passed)
        roadmap = Roadmap(start_term=terms[0] if terms else "")
        for term in terms:
            label = term_label(term)
            snapshot = self.snapshots.get(term)
            if snapshot is None:
                roadmap.terms.append(
                    RoadmapTerm(
                        term=term,
                        label=label,
                        status="no_catalog",
                        remaining=[f"{r.label} ({r.parent_group})" for r in remaining],
                        notes=self._availability_notes(remaining),
                    )
                )
                continue
            adjusted = [
                requirement.model_copy(
                    update={
                        "applied_credits": progress.get(requirement.id, requirement.applied_credits),
                        "applied_count": counts.get(requirement.id, requirement.applied_count),
                    }
                )
                for requirement in remaining
            ]
            draft = self.draft(term, constraints, requirements=adjusted, passed=passed)
            for selection in draft.selections:
                section = snapshot.by_crn(selection.crn)
                passed.add(selection.course.upper())
                for requirement in remaining:
                    codes = {target.code.upper() for target in requirement.targets}
                    matches_code = selection.course.upper() in codes
                    matches_attribute = bool(
                        section
                        and requirement.attributes
                        and set(requirement.attributes) & set(section.attribute_codes)
                    )
                    matches_wildcard = bool(
                        section
                        and requirement.subject_wildcards
                        and section.subject.upper()
                        in {wildcard.upper() for wildcard in requirement.subject_wildcards}
                    )
                    if not (matches_code or matches_attribute or matches_wildcard):
                        continue
                    progress[requirement.id] = progress.get(requirement.id, 0.0) + (selection.credits or 0)
                    counts[requirement.id] = counts.get(requirement.id, 0) + 1
            remaining = [
                requirement
                for requirement in remaining
                if not self._satisfied(requirement, progress, counts)
            ]
            roadmap.terms.append(
                RoadmapTerm(
                    term=term,
                    label=label,
                    status="planned",
                    draft=draft,
                    remaining=[f"{r.label} ({r.parent_group})" for r in remaining],
                    notes=list(draft.warnings),
                )
            )
        roadmap.final_remaining = [f"{r.label} ({r.parent_group})" for r in remaining]
        if remaining:
            roadmap.warnings.extend(self._availability_notes(remaining))
        return roadmap

    @staticmethod
    def _satisfied(requirement: Requirement, progress: dict[str, float], counts: dict[str, int]) -> bool:
        if requirement.pick_n_of is not None:
            return counts.get(requirement.id, 0) >= requirement.pick_n_of
        if requirement.min_credits is not None:
            return progress.get(requirement.id, 0.0) + 1e-9 >= requirement.min_credits
        return counts.get(requirement.id, 0) > 0

    def _availability_notes(self, requirements: Sequence[Requirement]) -> list[str]:
        from .catalog import offering_history_attribute, offering_history_course

        terms = self.terms_order
        notes: list[str] = []
        for requirement in requirements:
            bits = []
            for target in requirement.targets:
                history = offering_history_course(target, terms, self.snapshots)
                bits.append(f"{target.code}: {history.pattern.value} ({', '.join(term_label(t) for t in history.offered_terms) or 'never'})")
            for attribute in requirement.attributes:
                history = offering_history_attribute(attribute, terms, self.snapshots)
                bits.append(f"attr {attribute}: {history.pattern.value}")
            if bits:
                notes.append(f"{requirement.label}: " + "; ".join(bits))
        return notes

    def _sections_for(self, draft: DraftPlan, term: str) -> list[Section]:
        snapshot = self.snapshots.get(term)
        if snapshot is None:
            return []
        sections = []
        for selection in draft.selections:
            section = snapshot.by_crn(selection.crn)
            if section is not None:
                sections.append(section)
        return sections


def verify_draft(draft: DraftPlan, snapshot: TermSnapshot) -> list[str]:
    """Sanity-check a draft against the snapshot it was built from."""
    problems: list[str] = []
    sections: list[Section] = []
    for selection in draft.selections:
        section = snapshot.by_crn(selection.crn)
        if section is None:
            problems.append(f"CRN {selection.crn} ({selection.course}) is not in snapshot {snapshot.term}")
            continue
        sections.append(section)
        if section.open and not selection.credits:
            problems.append(f"CRN {selection.crn} ({selection.course}) has no credit value")
        if section.open and (section.seats_available or 0) <= 0:
            problems.append(f"CRN {selection.crn} ({selection.course}) is marked open with 0 seats")
    for conflict in find_conflicts([section.to_schedule_section() for section in sections]):
        problems.append(conflict.summary)
    total = round(
        sum(
            float(draft.credit_overrides.get(str(section.crn), section.planning_credits or 0.0))
            for section in sections
        ),
        2,
    )
    if abs(total - draft.total_credits) > 1e-6:
        problems.append(f"credit total mismatch: selections={total:g}, plan={draft.total_credits:g}")
    for pin in draft.pinned:
        if pin.outcome != "placed":
            problems.append(
                f"pinned {pin.kind} {pin.request!r} not placed ({pin.outcome}): {pin.reason}"
            )
    if draft.min_credits and not draft.min_credits_met:
        problems.append(
            f"minimum load not met: {draft.total_credits:g} of {draft.min_credits:g} credits"
        )
    return problems


def _rating_key(rating: RatingInfo) -> tuple:
    return (0 if rating.rated else 1, -rating.effective_rating)
