"""Resolve a human name to one Rate My Professors record.

The matcher is deliberately conservative: when two candidates are equally
plausible it returns ``professor=None`` plus the alternatives instead of
guessing.  Confidence is a heuristic score, not a probability.
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass
from typing import Iterable, Sequence

from .models import Match, Professor

DEFAULT_SCHOOL_ID = 706

_SUFFIXES = {"jr", "sr", "ii", "iii", "iv", "v", "phd", "md", "dr"}
_INITIAL_SUFFIXES = {"jr", "sr", "ii", "iii", "iv", "v"}
_TITLES = {"dr", "mr", "mrs", "ms", "miss", "prof"}

# ~40 common nickname groups. Groups are merged transitively, so Jackie is
# connected both to Jack/John/Jonathan and to Jacqueline.
_NICKNAME_GROUPS: tuple[frozenset[str], ...] = (
    frozenset({"michael", "mike", "mikey", "mick", "mickey"}),
    frozenset({"david", "dave", "davey"}),
    frozenset({"daniel", "dan", "danny"}),
    frozenset({"william", "will", "willie", "bill", "billy", "liam"}),
    frozenset({"robert", "rob", "robbie", "bob", "bobby"}),
    frozenset({"richard", "rich", "rick", "ricky", "dick"}),
    frozenset({"james", "jim", "jimmy", "jamie"}),
    frozenset({"joseph", "joe", "joey"}),
    frozenset({"thomas", "tom", "tommy"}),
    frozenset({"christopher", "chris", "christy"}),
    frozenset({"matthew", "matt", "matty"}),
    frozenset({"anthony", "tony"}),
    frozenset({"andrew", "andy", "drew"}),
    frozenset({"edward", "ed", "eddie", "ned", "ted", "teddy"}),
    frozenset({"kenneth", "ken", "kenny"}),
    frozenset({"patrick", "pat", "paddy"}),
    frozenset({"peter", "pete"}),
    frozenset({"stephen", "steven", "steve", "stevie"}),
    frozenset({"nicholas", "nick", "nicky"}),
    frozenset({"alexander", "alex", "alec", "xander"}),
    frozenset({"samuel", "sam", "sammy"}),
    frozenset({"benjamin", "ben", "benny"}),
    frozenset({"jennifer", "jen", "jenny", "jenn"}),
    frozenset({"elizabeth", "liz", "lizzie", "beth", "betty", "betsy"}),
    frozenset({"katherine", "kathryn", "catherine", "kate", "kathy", "katie", "cathy"}),
    frozenset({"margaret", "maggie", "meg", "peggy"}),
    frozenset({"patricia", "pat", "patty", "trish"}),
    frozenset({"deborah", "deb", "debbie"}),
    frozenset({"barbara", "barb", "babs"}),
    frozenset({"dorothy", "dot", "dottie"}),
    frozenset({"sandra", "sandy"}),
    frozenset({"pamela", "pam"}),
    frozenset({"carolyn", "caroline", "carol"}),
    frozenset({"janet", "jan"}),
    frozenset({"judith", "judy"}),
    frozenset({"gerald", "jerry", "gerry"}),
    frozenset({"lawrence", "larry"}),
    frozenset({"francis", "frank", "frankie"}),
    frozenset({"eugene", "gene"}),
    frozenset({"ronald", "ron", "ronnie"}),
    frozenset({"donald", "don", "donnie"}),
    frozenset({"arthur", "art", "artie"}),
    frozenset({"harold", "hal", "harry"}),
    frozenset({"jack", "john", "jonathan", "jackie", "jon"}),
    frozenset({"jacqueline", "jackie", "jacky", "jacqui"}),
    frozenset({"susan", "sue", "susie", "suzy", "suzanne"}),
    frozenset({"victoria", "vicky", "vicki", "tori"}),
    frozenset({"veronica", "ronnie", "ron"}),
    frozenset({"allison", "alison", "ali"}),
)


def _build_nickname_index() -> dict[str, str]:
    parent: dict[str, str] = {}

    def find(item: str) -> str:
        parent.setdefault(item, item)
        while parent[item] != item:
            parent[item] = parent[parent[item]]
            item = parent[item]
        return item

    def union(left: str, right: str) -> None:
        left_root, right_root = find(left), find(right)
        if left_root != right_root:
            parent[right_root] = left_root

    for group in _NICKNAME_GROUPS:
        members = sorted(group)
        for member in members[1:]:
            union(members[0], member)
    return {name: find(name) for name in parent}


_NICKNAME_INDEX = _build_nickname_index()


def strip_accents(text: str) -> str:
    decomposed = unicodedata.normalize("NFKD", text)
    return "".join(char for char in decomposed if not unicodedata.combining(char))


def normalize_name(text: str) -> str:
    """Lowercase, de-accent, drop apostrophes/periods, split on punctuation."""
    text = strip_accents(text).replace("\u2019", "'").replace("\u2018", "'")
    if "," in text:
        # "Glenny, Jackie" -> "Jackie Glenny"
        parts = [part.strip() for part in text.split(",", 1)]
        text = " ".join(reversed([part for part in parts if part]))
    text = text.replace("&", " and ")
    text = re.sub(r"[.'\u2019`]", "", text.lower())
    text = re.sub(r"[-_/]", " ", text)
    tokens = re.sub(r"[^a-z0-9]+", " ", text).split()
    while tokens and tokens[-1] in _SUFFIXES:
        tokens.pop()
    while tokens and tokens[0] in _INITIAL_SUFFIXES:
        tokens.pop(0)
    while tokens and tokens[0] in _TITLES:
        tokens.pop(0)
    return " ".join(tokens)


def _surname_variants(last_name: str | None) -> set[str]:
    normalized = normalize_name(last_name or "")
    if not normalized:
        return set()
    parts = normalized.split()
    variants = {normalized, "".join(parts)}
    return variants


def _surname_match(query_tokens: Sequence[str], last_name: str | None) -> int | None:
    """Return how many query tokens the surname consumes, or ``None``.

    ``query_tokens`` must already be normalized.  Matching is suffix-based so
    "Jackie Smith-Jones" also matches the hyphenated surname.
    """
    variants = _surname_variants(last_name)
    if not variants:
        return None
    for variant in sorted(variants, key=lambda item: len(item.split()), reverse=True):
        variant_tokens = variant.split()
        if len(variant_tokens) > len(query_tokens):
            continue
        if list(query_tokens[-len(variant_tokens) :]) == variant_tokens:
            return len(variant_tokens)
    return None


def _first_name_score(query_first: Sequence[str], candidate_first: str | None) -> float:
    if not query_first:
        return 0.0
    candidate_tokens = normalize_name(candidate_first or "").split()
    if not candidate_tokens:
        return 0.0

    query = query_first[0]
    candidate = candidate_tokens[0]

    if query == candidate:
        return 1.0
    if len(query) == 1 and candidate.startswith(query):
        return 0.85
    if _NICKNAME_INDEX.get(query) and _NICKNAME_INDEX.get(query) == _NICKNAME_INDEX.get(
        candidate
    ):
        return 0.92
    # Initials written without periods are normalized away ("J Glenny" stays
    # "j glenny"), but an extra middle token should not break an exact match.
    if len(query) >= 3 and candidate.startswith(query):
        return 0.8
    return 0.0


@dataclass
class _Candidate:
    professor: Professor
    score: float
    matched_first: bool


def rank_candidates(name: str, professors: Iterable[Professor]) -> Match:
    """Offline, network-free matcher used by both the client and the tests."""
    query_tokens = normalize_name(name).split()
    if not query_tokens:
        return Match(query=name, confidence=0.0, reason="empty name")

    candidates: list[_Candidate] = []
    for professor in professors:
        consumed = _surname_match(query_tokens, professor.last_name)
        if consumed is None:
            continue
        first_tokens = query_tokens[: len(query_tokens) - consumed]
        score = _first_name_score(first_tokens, professor.first_name)
        if first_tokens and score == 0.0:
            continue
        candidates.append(
            _Candidate(professor=professor, score=score, matched_first=bool(first_tokens))
        )

    if not candidates:
        return Match(
            query=name,
            confidence=0.0,
            reason=f"no professor matched {name!r} at this school",
        )

    if all(not candidate.matched_first for candidate in candidates):
        # Surname-only query.
        if len(candidates) == 1:
            return _finish(
                name,
                candidates[0].professor,
                0.7,
                [],
                "unique surname match",
            )
        ordered = sorted(candidates, key=lambda item: item.professor.num_ratings, reverse=True)
        return Match(
            query=name,
            confidence=0.4,
            alternatives=[candidate.professor for candidate in ordered],
            reason=f"surname-only query is ambiguous: {len(ordered)} professors",
        )

    best_score = max(candidate.score for candidate in candidates)
    best = [candidate for candidate in candidates if candidate.score == best_score]
    if len(best) > 1:
        ordered = sorted(best, key=lambda item: item.professor.num_ratings, reverse=True)
        names = ", ".join(professor.full_name for professor in [c.professor for c in ordered])
        return Match(
            query=name,
            confidence=0.5,
            alternatives=[candidate.professor for candidate in ordered],
            reason=f"ambiguous: multiple equally likely matches ({names})",
        )

    winner = best[0]
    if best_score == 1.0:
        confidence = 0.99
        reason = "exact first and last name match"
    elif best_score >= 0.9:
        confidence = 0.92
        reason = "nickname equivalent first name"
    elif best_score >= 0.85:
        confidence = 0.85
        reason = "first-initial match"
    else:
        confidence = 0.8
        reason = "prefix first-name match"

    alternatives = [
        candidate.professor
        for candidate in sorted(candidates, key=lambda item: item.score, reverse=True)
        if candidate.professor.rmp_id != winner.professor.rmp_id
    ]
    return _finish(name, winner.professor, confidence, alternatives, reason)


def _finish(
    query: str,
    professor: Professor,
    confidence: float,
    alternatives: list[Professor],
    reason: str,
) -> Match:
    if professor.num_ratings <= 0:
        confidence *= 0.8
        reason = f"{reason}; professor has no ratings, so no rating evidence"
    return Match(
        query=query,
        professor=professor,
        confidence=confidence,
        alternatives=alternatives,
        reason=reason,
    )


def match_professor(
    name: str,
    school_id: int = DEFAULT_SCHOOL_ID,
    *,
    client: object | None = None,
) -> Match:
    """Search RMP and return a conservative best match.

    ``client`` is injectable for tests; by default a short-lived
    :class:`~eagle_flightplan.rmp.client.RMPClient` is created.
    """
    owns_client = client is None
    if client is None:
        from .client import RMPClient

        client = RMPClient()
    try:
        professors = client.search_professors(name, school_id=school_id)  # type: ignore[attr-defined]
        return rank_candidates(name, professors)
    finally:
        if owns_client:
            client.close()  # type: ignore[attr-defined]
