"""Term-wide section snapshots.

One class-search sweep of a term (pages of 500) is enough for every planning
question: the payload already carries attributes, faculty, meeting times,
credits and seat counts. Snapshots are cached per term (15 min TTL) and indexed
in SQLite so cross-term offering history is a single query.

    snapshot = fetch_term("202620")
    snapshot.by_course("CYS", "4245")
    snapshot.by_attribute("CCUL")
    snapshot.open_only().by_subject("BIB")
"""

from __future__ import annotations

import os
import sqlite3
import time as _time
from datetime import datetime, timezone
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, Field

from ..auth import ensure
from ..config import TERM_CACHE_TTL_SECONDS, TERM_INDEX_PATH, TERMS_DIR, ensure_dirs
from ..models import Section
from .ssb import SSBClient

PAGE_SIZE = 500
MAX_PAGE_REQUESTS = 12


class SnapshotMeta(BaseModel):
    term: str
    fetched_at: datetime
    source: Literal["network", "cache"] = "network"
    request_count: int = 0
    elapsed_seconds: float = 0.0
    total_reported: int = 0


class TermSnapshot(BaseModel):
    """All sections of one term, queryable offline."""

    meta: SnapshotMeta
    sections: list[Section] = Field(default_factory=list)

    def model_post_init(self, __context: object) -> None:  # noqa: D401 - pydantic hook
        self._by_crn = {section.crn: section for section in self.sections}

    # ------------------------------------------------------------------ queries
    @property
    def term(self) -> str:
        return self.meta.term

    def by_crn(self, crn: str) -> Section | None:
        return self._by_crn.get(str(crn))

    def by_course(self, subject: str, number: str) -> list[Section]:
        subject = subject.upper()
        number = str(number)
        return sorted(
            (s for s in self.sections if s.subject.upper() == subject and s.number == number),
            key=lambda s: (not s.open, s.section),
        )

    by_course_code = by_course

    def by_subject(self, subject: str) -> list[Section]:
        subject = subject.upper()
        return sorted((s for s in self.sections if s.subject.upper() == subject), key=lambda s: s.crn)

    def by_attribute(self, code: str) -> list[Section]:
        code = code.upper()
        return sorted(
            (s for s in self.sections if code in s.attribute_codes),
            key=lambda s: (s.subject, s.number, s.crn),
        )

    def by_instructor(self, name: str) -> list[Section]:
        needle = name.strip().lower()
        return sorted(
            (
                s
                for s in self.sections
                if any(needle in person.name.lower() for person in s.faculty)
            ),
            key=lambda s: (s.subject, s.number, s.crn),
        )

    def open_only(self) -> "TermSnapshot":
        return TermSnapshot(
            meta=self.meta.model_copy(),
            sections=[section for section in self.sections if section.open],
        )

    def search(self, keyword: str) -> list[Section]:
        needle = keyword.strip().lower()
        return sorted(
            (
                s
                for s in self.sections
                if needle in s.title.lower()
                or needle in s.code.lower()
                or needle in s.subject_course.lower()
                or any(needle in person.name.lower() for person in s.faculty)
            ),
            key=lambda s: (s.subject, s.number, s.crn),
        )

    def offerings(self, subject: str, number: str) -> list[Section]:
        return self.by_course(subject, number)

    def instructors(self) -> list[str]:
        return sorted({person.name for section in self.sections for person in section.faculty if person.name})

    def attributes(self) -> dict[str, int]:
        counts: dict[str, int] = {}
        for section in self.sections:
            for attribute in section.attributes:
                counts[attribute.code] = counts.get(attribute.code, 0) + 1
        return dict(sorted(counts.items()))

    def __len__(self) -> int:
        return len(self.sections)

    # -------------------------------------------------------------------- cache
    def save(self) -> Path:
        ensure_dirs()
        path = TERMS_DIR / f"{self.term}.json"
        # Atomic replace: a crash mid-write must never leave a truncated JSON
        # file that makes every later read fail.
        tmp = path.with_name(path.name + ".tmp")
        tmp.write_text(self.model_dump_json(indent=1), encoding="utf-8")
        os.replace(tmp, path)
        TermIndex().upsert(self)
        return path


def snapshot_path(term: str) -> Path:
    return TERMS_DIR / f"{term}.json"


def load_snapshot(term: str) -> TermSnapshot | None:
    path = snapshot_path(term)
    if not path.exists():
        return None
    try:
        snapshot = TermSnapshot.model_validate_json(path.read_text(encoding="utf-8"))
    except (ValueError, OSError):
        # Truncated/corrupt cache (e.g. a crash during an old non-atomic write):
        # treat it as absent so fetch_term refetches instead of failing forever.
        return None
    snapshot.meta = snapshot.meta.model_copy(update={"source": "cache"})
    return snapshot


def cache_age_seconds(term: str) -> float | None:
    path = snapshot_path(term)
    if not path.exists():
        return None
    return _time.time() - path.stat().st_mtime


def fetch_term(
    term: str,
    refresh: bool = False,
    *,
    client: SSBClient | None = None,
    page_size: int = PAGE_SIZE,
) -> TermSnapshot:
    """Fetch (or reuse) every section of ``term``.

    Paginates ``pageOffset``/``pageMaxSize`` until ``totalCount`` is consumed.
    The server caps a page at 500 rows, so a 900-section term costs two requests.
    """
    if not refresh:
        cached = load_snapshot(term)
        age = cache_age_seconds(term)
        if cached is not None and age is not None and age < TERM_CACHE_TTL_SECONDS:
            cached.meta = cached.meta.model_copy(update={"source": "cache", "elapsed_seconds": 0.0})
            return cached

    ssb = client or SSBClient(ensure())
    started = _time.time()
    requests = 0
    ssb.set_term(term, mode="search")
    requests += 1

    collected: list[Section] = []
    total = 0
    offset = 0
    while requests < MAX_PAGE_REQUESTS:
        page = ssb.search(term=term, page_offset=offset, page_max_size=page_size)
        requests += 1
        total = page.total_count
        if not page.success and offset == 0:
            return _fetch_by_subject(term, ssb, started, requests, page_size)
        collected.extend(Section.from_wire(row) for row in page.sections)
        offset += len(page.sections)
        if not page.sections or offset >= total:
            break
    if not collected and total > 0:
        raise RuntimeError(
            f"Term {term}: Banner reports {total} sections but every page came back empty."
        )

    snapshot = TermSnapshot(
        meta=SnapshotMeta(
            term=term,
            fetched_at=datetime.now(timezone.utc),
            source="network",
            request_count=requests,
            elapsed_seconds=round(_time.time() - started, 2),
            total_reported=total,
        ),
        sections=collected,
    )
    snapshot.save()
    return snapshot


def _fetch_by_subject(
    term: str, ssb: SSBClient, started: float, requests: int, page_size: int
) -> TermSnapshot:
    """Fallback: an empty subject search was rejected, so union every subject."""
    subjects = [row["code"] for row in ssb._get_json("/courseSearch/get_subject", {"searchTerm": "", "offset": "1", "max": "500"})]
    collected: list[Section] = []
    for subject in subjects:
        page = ssb.search(term=term, subject=subject, page_max_size=page_size)
        requests += 1
        collected.extend(Section.from_wire(row) for row in page.sections)
    snapshot = TermSnapshot(
        meta=SnapshotMeta(
            term=term,
            fetched_at=datetime.now(timezone.utc),
            source="network",
            request_count=requests,
            elapsed_seconds=round(_time.time() - started, 2),
            total_reported=len(collected),
        ),
        sections=collected,
    )
    snapshot.save()
    return snapshot


class TermIndex:
    """SQLite index of every cached term, used for offering history."""

    def __init__(self, path: Path | None = None) -> None:
        self.path = Path(path) if path is not None else TERM_INDEX_PATH
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(str(self.path))
        self._conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS sections (
                term TEXT NOT NULL,
                crn TEXT NOT NULL,
                subject TEXT NOT NULL,
                number TEXT NOT NULL,
                open INTEGER NOT NULL,
                seats INTEGER,
                credits REAL,
                part_of_term TEXT,
                schedule_type TEXT,
                PRIMARY KEY (term, crn)
            );
            CREATE INDEX IF NOT EXISTS sections_course ON sections (subject, number);
            CREATE TABLE IF NOT EXISTS terms (
                term TEXT PRIMARY KEY,
                fetched_at TEXT NOT NULL,
                section_count INTEGER NOT NULL
            );
            """
        )

    def upsert(self, snapshot: TermSnapshot) -> None:
        rows = [
            (
                section.term,
                section.crn,
                section.subject.upper(),
                section.number,
                int(section.open),
                section.seats_available,
                section.credits,
                section.part_of_term,
                section.schedule_type,
            )
            for section in snapshot.sections
        ]
        with self._conn:
            self._conn.execute("DELETE FROM sections WHERE term = ?", (snapshot.term,))
            self._conn.executemany(
                "INSERT OR REPLACE INTO sections "
                "(term, crn, subject, number, open, seats, credits, part_of_term, schedule_type) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                rows,
            )
            self._conn.execute(
                "INSERT OR REPLACE INTO terms (term, fetched_at, section_count) VALUES (?, ?, ?)",
                (snapshot.term, snapshot.meta.fetched_at.isoformat(), len(snapshot.sections)),
            )

    def terms(self) -> list[str]:
        return [row[0] for row in self._conn.execute("SELECT term FROM terms ORDER BY term DESC")]

    def section_count(self, term: str) -> int:
        row = self._conn.execute(
            "SELECT COUNT(*) FROM sections WHERE term = ?", (term,)
        ).fetchone()
        return int(row[0]) if row else 0

    def offering_rows(self, subject: str, number: str) -> list[tuple[str, int, int]]:
        """(term, section_count, open_count) for one course across cached terms."""
        return [
            (row[0], int(row[1]), int(row[2]))
            for row in self._conn.execute(
                "SELECT term, COUNT(*), SUM(open) FROM sections "
                "WHERE subject = ? AND number = ? GROUP BY term ORDER BY term",
                (subject.upper(), str(number)),
            )
        ]

    def close(self) -> None:
        self._conn.close()
