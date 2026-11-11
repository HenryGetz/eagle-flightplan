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
import re
from datetime import time
from pathlib import Path
from typing import Callable, Sequence

from pydantic import BaseModel, Field

from .audit.models import Audit
from .banner.search import TermSnapshot
from .catalog import (
    Availability,
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
    """RMP lookups with an on-disk cache (7-day TTL)."""

    def __init__(
        self,
        client=None,
        *,
        path: Path | None = None,
        ttl_seconds: int = 7 * 24 * 3600,
        lookup: Callable[[str], RatingInfo] | None = None,
    ) -> None:
        self.path = Path(path) if path is not None else RATINGS_PATH
        self.ttl_seconds = ttl_seconds
        self._client = client
        self._lookup = lookup
        self._memo: dict[str, RatingInfo] = {}
        self._cache: dict[str, dict] = {}
        if self.path.exists():
            try:
                self._cache = json.loads(self.path.read_text(encoding="utf-8"))
            except json.JSONDecodeError:
                self._cache = {}

    def _save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(json.dumps(self._cache, indent=1), encoding="utf-8")

    def get(self, instructor: str) -> RatingInfo:
        key = instructor.strip()
        if key in self._memo:
            return self._memo[key]
        cached = self._cache.get(key)
        if cached:
            info = RatingInfo(**cached)
            self._memo[key] = info
            return info
        info = self._lookup(key) if self._lookup else self._fetch(key)
        self._memo[key] = info
        self._cache[key] = info.model_dump()
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
    """Parsed Banner prerequisite text (``and`` binds tighter than ``or``)."""

    raw: str = ""
    codes: list[str] = Field(default_factory=list)
    tree: list = Field(default_factory=list)

    def satisfied_by(self, passed: set[str]) -> bool:
        if not self.tree:
            return True
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


def parse_prerequisites(text: str) -> PrereqExpression:
    """Parse the flattened Banner fragment; empty tree == no prerequisites."""
    raw = " ".join(text.split())
    if not raw or "no prerequisite information available" in raw.lower():
        return PrereqExpression(raw=raw)
    marker = raw.find("Prerequisites:")
    body = raw[marker + len("Prerequisites:"):] if marker >= 0 else raw
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
    if not tokens:
        return PrereqExpression(raw=raw)

    expression = PrereqExpression(raw=raw, codes=[value for kind, value in tokens if kind == "CODE"])
    index = 0

    def parse_expression() -> list:
        nonlocal index
        children = [parse_term()]
        while index < len(tokens) and tokens[index][0] == "OR":
            index += 1
            children.append(parse_term())
        return children[0] if len(children) == 1 else ["OR", children]

    def parse_term() -> list:
        nonlocal index
        children = [parse_factor()]
        while index < len(tokens) and tokens[index][0] == "AND":
            index += 1
            children.append(parse_factor())
        return children[0] if len(children) == 1 else ["AND", children]

    def parse_factor() -> list:
        nonlocal index
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

    expression.tree = parse_expression()
    return expression


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
                return PrereqExpression(**json.loads(path.read_text(encoding="utf-8")))
            except (json.JSONDecodeError, TypeError):
                pass
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

    def check(self, section: Section, passed: set[str]) -> tuple[bool, str]:
        try:
            expression = self.expression(section)
        except Exception as exc:  # noqa: BLE001 - never fail planning on fetch errors
            return True, f"prerequisite text unavailable ({exc.__class__.__name__})"
        missing = [code for code in expression.codes if code not in passed]
        if expression.satisfied_by(passed):
            return True, ""
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


class Unplaced(BaseModel):
    requirement_id: str
    requirement: str
    group: str
    reason: str


class DraftPlan(BaseModel):
    term: str
    label: str = ""
    selections: list[Selection] = Field(default_factory=list)
    unplaced_requirements: list[Unplaced] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)
    grid: str = ""
    total_credits: float = 0.0
    score_breakdown: list[str] = Field(default_factory=list)

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
            -(section.credits or 0),
            1 if section.unscheduled else 0,
            meeting_start(section),
            section.crn,
        )
        return key, name, rating

    # -------------------------------------------------------------------- hard
    def _fits(
        self,
        section: Section,
        selected: Sequence[Section],
        credits_used: float,
        constraints: Constraints,
        passed: set[str],
    ) -> tuple[bool, str]:
        credits = section.credits or 0
        if credits_used + credits > constraints.max_credits + 1e-6:
            return False, f"budget: {credits_used:g}+{credits:g} exceeds {constraints.max_credits:g} credits"
        if set(section.days) & set(constraints.avoid_days):
            blocked = ", ".join(day.value for day in set(section.days) & set(constraints.avoid_days))
            return False, f"meets on avoided day {blocked}"
        if constraints.earliest or constraints.latest:
            for meeting in section.meetings:
                if not meeting.scheduled:
                    continue
                if constraints.earliest and meeting.start < constraints.earliest:
                    return False, f"starts before {constraints.earliest.strftime('%H:%M')}"
                if constraints.latest and meeting.end > constraints.latest:
                    return False, f"ends after {constraints.latest.strftime('%H:%M')}"
        clash = find_conflicts([s.to_schedule_section() for s in selected] + [section.to_schedule_section()])
        if clash:
            return False, f"time conflict: {clash[0].summary}"
        ok, reason = self.prereqs.check(section, passed)
        if not ok:
            return False, reason
        return True, ""

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

        plan = DraftPlan(term=term, label=term_label(term))
        selected: list[Section] = []
        selected_codes: set[str] = set()
        by_crn: dict[str, Selection] = {}
        credits_used = 0.0
        unrated_picks = 0
        rated_picks: list[RatingInfo] = []

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
                    selected, selected_codes, by_crn, credits_used, passed,
                )
                credits_used = round(sum(section.credits or 0 for section in selected), 2)
                if credits_used > constraints.max_credits:
                    raise PlannerError("credit accounting exceeded max_credits")

        for selection in plan.selections:
            if selection.rating.rated:
                rated_picks.append(selection.rating)
            else:
                unrated_picks += 1
        if any(selection.verify_at_registration for selection in plan.selections):
            plan.warnings.append(
                "some sections are not open yet (future term); verify seats when registration opens"
            )
        plan.total_credits = round(sum(section.credits or 0 for section in selected), 2)
        plan.grid = render_grid(
            [section.to_schedule_section() for section in selected], include_tba=True
        ) if selected else ""
        plan.score_breakdown = self._score_breakdown(plan, rated_picks, unrated_picks, constraints)
        return plan

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
    ) -> None:
        placed_for_requirement = 0
        candidates = sorted(
            resolution.candidates,
            key=lambda section: self._rank(section, constraints)[0],
        )
        if not candidates:
            plan.unplaced_requirements.append(
                Unplaced(
                    requirement_id=requirement.id,
                    requirement=requirement.label,
                    group=requirement.parent_group,
                    reason=resolution.reason or "no candidate sections",
                )
            )
            return
        needed = requirement.pick_n_of or (len(candidates) if requirement.min_credits else 1)
        credits_needed = requirement.min_credits
        covered_credits = requirement.applied_credits
        if requirement.pick_n_of is not None:
            needed = max(0, requirement.pick_n_of - requirement.applied_count)

        for section in candidates:
            if placed_for_requirement >= needed:
                break
            if credits_needed is not None:
                if covered_credits >= credits_needed:
                    break
                if not section.credits:
                    continue
            if section.crn in by_crn:
                existing = by_crn[section.crn]
                if requirement.id not in existing.covers:
                    existing.covers.append(requirement.id)
                covered_credits += existing.credits or 0
                placed_for_requirement += 1
                continue
            if section.code in selected_codes:
                continue
            fits, reason = self._fits(section, selected, credits_used, constraints, passed)
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
                credits=section.credits,
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
            )
            plan.selections.append(selection)
            by_crn[section.crn] = selection
            selected_codes.add(section.code)
            selected.append(section)
            credits_used += section.credits or 0
            covered_credits += section.credits or 0
            placed_for_requirement += 1
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
            fits, why = self._fits(candidates[0], selected, credits_used, constraints, passed)
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
        return lines

    def roadmap(
        self,
        terms: Sequence[str],
        constraints: Constraints | None = None,
    ) -> Roadmap:
        """Run the draft term by term, carrying completed requirements forward."""
        constraints = constraints or Constraints()
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
                    if not (matches_code or matches_attribute):
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
    total = round(sum(section.credits or 0 for section in sections), 2)
    if abs(total - draft.total_credits) > 1e-6:
        problems.append(f"credit total mismatch: selections={total:g}, plan={draft.total_credits:g}")
    return problems


def _rating_key(rating: RatingInfo) -> tuple:
    return (0 if rating.rated else 1, -rating.effective_rating)
