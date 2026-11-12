"""Typed records returned by the Rate My Professors GraphQL API.

The GraphQL schema is untyped in the browser bundle, so these models are the
single place where camelCase API fields become snake_case Python attributes.
"""

from __future__ import annotations

from datetime import datetime, timezone

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


def _parse_rmp_date(value: str | None) -> datetime | None:
    """Parse ``2025-11-14 20:35:17 +0000 UTC`` (the only format RMP emits)."""
    if not value:
        return None
    raw = value.strip().removesuffix(" UTC")
    try:
        parsed = datetime.strptime(raw, "%Y-%m-%d %H:%M:%S %z")
    except ValueError:
        try:
            parsed = datetime.fromisoformat(value)
        except ValueError:
            return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed


def _clean_optional_number(value: float | int | None) -> float | None:
    """Treat RMP's sentinel values (``0`` / ``-1``) and nulls as "no data"."""
    if value is None:
        return None
    number = float(value)
    if number <= -1:
        return None
    return number


class SchoolRef(BaseModel):
    """The school object embedded in teacher records."""

    model_config = ConfigDict(populate_by_name=True)

    graphql_id: str | None = Field(default=None, alias="id")
    legacy_id: int | None = Field(default=None, alias="legacyId")
    name: str | None = None
    city: str | None = None
    state: str | None = None


class School(BaseModel):
    """A school returned by the school lookup query."""

    model_config = ConfigDict(populate_by_name=True)

    graphql_id: str = Field(alias="id")
    legacy_id: int = Field(alias="legacyId")
    name: str
    city: str | None = None
    state: str | None = None
    country: str | None = None
    num_ratings: int = Field(default=0, alias="numRatings")
    avg_rating: float | None = Field(default=None, alias="avgRating")
    avg_rating_rounded: float | None = Field(default=None, alias="avgRatingRounded")

    @model_validator(mode="before")
    @classmethod
    def _blank_stats_without_ratings(cls, data: object) -> object:
        if isinstance(data, dict) and not data.get("numRatings"):
            data = dict(data)
            data["avgRating"] = None
            data["avgRatingRounded"] = None
        return data


class CourseTaught(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    course_name: str = Field(alias="courseName")
    course_count: int = Field(default=0, alias="courseCount")


class RatingTag(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    name: str = Field(alias="tagName")
    count: int = Field(default=0, alias="tagCount")
    legacy_id: int | None = Field(default=None, alias="legacyId")


class RatingDistribution(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    r1: int = 0
    r2: int = 0
    r3: int = 0
    r4: int = 0
    r5: int = 0
    total: int = 0


class Rating(BaseModel):
    """One student review. All optional fields are genuinely nullable."""

    model_config = ConfigDict(populate_by_name=True)

    legacy_id: int | None = Field(default=None, alias="legacyId")
    date: datetime | None = None
    course: str | None = Field(default=None, alias="class")
    comment: str | None = None
    clarity: float | None = Field(default=None, alias="clarityRating")
    helpfulness: float | None = Field(default=None, alias="helpfulRating")
    difficulty: float | None = Field(default=None, alias="difficultyRating")
    grade: str | None = None
    would_take_again: bool | None = Field(default=None, alias="wouldTakeAgain")
    attendance_mandatory: str | None = Field(default=None, alias="attendanceMandatory")
    textbook_use: int | None = Field(default=None, alias="textbookUse")
    is_for_credit: bool | None = Field(default=None, alias="isForCredit")
    is_for_online_class: bool | None = Field(default=None, alias="isForOnlineClass")
    tags: list[str] = Field(default_factory=list, alias="ratingTags")
    thumbs_up: int = Field(default=0, alias="thumbsUpTotal")
    thumbs_down: int = Field(default=0, alias="thumbsDownTotal")

    @field_validator("date", mode="before")
    @classmethod
    def _date_from_rmp(cls, value: object) -> object:
        if isinstance(value, str):
            return _parse_rmp_date(value)
        return value

    @field_validator("tags", mode="before")
    @classmethod
    def _split_tags(cls, value: object) -> object:
        # The API sends "Tough grader--Lots of homework".
        if isinstance(value, str):
            return [part.strip() for part in value.split("--") if part.strip()]
        return value or []


class RelatedProfessor(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    graphql_id: str | None = Field(default=None, alias="id")
    legacy_id: int | None = Field(default=None, alias="legacyId")
    first_name: str | None = Field(default=None, alias="firstName")
    last_name: str | None = Field(default=None, alias="lastName")
    avg_rating: float | None = Field(default=None, alias="avgRating")


class Professor(BaseModel):
    """A professor record.

    ``avg_rating`` / ``avg_difficulty`` / ``would_take_again_percent`` are
    ``None`` when the professor has no ratings. They are never fabricated as
    ``0.0`` -- RMP uses 0 and -1 internally as "no data" sentinels.
    """

    model_config = ConfigDict(populate_by_name=True)

    graphql_id: str = Field(alias="id")
    rmp_id: int = Field(alias="legacyId")
    first_name: str | None = Field(default=None, alias="firstName")
    last_name: str = Field(alias="lastName")
    department: str | None = None
    department_id: str | None = Field(default=None, alias="departmentId")
    school: SchoolRef | None = None
    avg_rating: float | None = Field(default=None, alias="avgRating")
    avg_difficulty: float | None = Field(default=None, alias="avgDifficulty")
    would_take_again_percent: float | None = Field(
        default=None, alias="wouldTakeAgainPercent"
    )
    num_ratings: int = Field(default=0, alias="numRatings")
    rating_distribution: RatingDistribution | None = Field(
        default=None, alias="ratingsDistribution"
    )
    rating_tags: list[RatingTag] = Field(default_factory=list, alias="teacherRatingTags")
    recent_ratings: list[Rating] = Field(default_factory=list)
    courses: list[CourseTaught] = Field(default_factory=list)
    related_professors: list[RelatedProfessor] = Field(default_factory=list)
    lock_status: str | None = Field(default=None, alias="lockStatus")

    @model_validator(mode="before")
    @classmethod
    def _no_stats_without_ratings(cls, data: object) -> object:
        if not isinstance(data, dict):
            return data
        data = dict(data)
        if not data.get("numRatings"):
            for key in ("avgRating", "avgDifficulty", "wouldTakeAgainPercent"):
                data[key] = None
            distribution = data.get("ratingsDistribution")
            if isinstance(distribution, dict):
                data["ratingsDistribution"] = {
                    **distribution,
                    "total": 0,
                    "r1": 0,
                    "r2": 0,
                    "r3": 0,
                    "r4": 0,
                    "r5": 0,
                }
        else:
            data["avgRating"] = _clean_optional_number(data.get("avgRating"))
            data["avgDifficulty"] = _clean_optional_number(data.get("avgDifficulty"))
            data["wouldTakeAgainPercent"] = _clean_optional_number(
                data.get("wouldTakeAgainPercent")
            )
        return data

    @property
    def full_name(self) -> str:
        return " ".join(part for part in (self.first_name, self.last_name) if part)

    @property
    def profile_url(self) -> str:
        return f"https://www.ratemyprofessors.com/professor/{self.rmp_id}"


class Match(BaseModel):
    """Result of resolving a free-text name to one professor."""

    query: str
    professor: Professor | None = None
    confidence: float = 0.0
    alternatives: list[Professor] = Field(default_factory=list)
    reason: str = ""

    @model_validator(mode="after")
    def _confidence_bounds(self) -> "Match":
        self.confidence = min(1.0, max(0.0, round(self.confidence, 4)))
        return self
