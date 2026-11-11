"""Synchronous client for the Rate My Professors GraphQL endpoint.

The public web app POSTs Relay operation documents to
``https://www.ratemyprofessors.com/graphql``.  As of 2026-11 the endpoint
accepts anonymous requests (no ``Authorization`` header); logged-in browsers
send ``Authorization: Bearer <rmpAuth cookie>``.  See ``docs/rmp-api.md``.
"""

from __future__ import annotations

import base64
import json
import random
import threading
import time
from typing import Any, Callable, Mapping, Sequence

import httpx
from pydantic import ValidationError

from .cache import SqliteCache, make_key
from .models import Match, Professor, Rating, School
from . import queries

GRAPHQL_URL = "https://www.ratemyprofessors.com/graphql"
DEFAULT_SCHOOL_ID = 706
DEFAULT_USER_AGENT = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
)


class RMPError(Exception):
    """Base class for every Rate My Professors failure."""


class GraphQLError(RMPError):
    """The endpoint returned a non-empty ``errors`` array."""

    def __init__(self, errors: Sequence[Mapping[str, Any]]) -> None:
        self.errors = list(errors)
        messages = [str(error.get("message", error)) for error in self.errors]
        super().__init__("; ".join(messages) or "unknown GraphQL error")


class NotFound(RMPError):
    """The requested teacher/school does not exist (or was removed)."""


class RateLimited(RMPError):
    """HTTP 429 after retries, or a rate-limit GraphQL response."""

    def __init__(self, message: str, retry_after: float | None = None) -> None:
        self.retry_after = retry_after
        super().__init__(message)


class Blocked(RMPError):
    """Cloudflare/WAF challenge or an unexpected 401/403."""


class RMPNetworkError(RMPError):
    """Connection failure after retries."""


def encode_global_id(type_name: str, legacy_id: int) -> str:
    return base64.b64encode(f"{type_name}-{legacy_id}".encode()).decode()


def _global_id(value: int | str, type_name: str) -> str:
    if isinstance(value, int) or (isinstance(value, str) and value.isdigit()):
        return encode_global_id(type_name, int(value))
    if isinstance(value, str) and value.startswith(f"{type_name}-"):
        return encode_global_id(type_name, int(value.split("-", 1)[1]))
    return str(value)


def _parse_professor(node: Mapping[str, Any]) -> Professor:
    ratings_connection = node.get("ratings") or {}
    edges = ratings_connection.get("edges") or []
    payload = dict(node)
    payload["recent_ratings"] = [
        edge.get("node", {}) for edge in edges if isinstance(edge, dict)
    ]
    payload["courses"] = node.get("courseCodes") or []
    payload["related_professors"] = node.get("relatedTeachers") or []
    payload["rating_distribution"] = node.get("ratingsDistribution")
    return Professor.model_validate(payload)


class RMPClient:
    """Small, polite GraphQL client.

    Parameters mirror the knobs needed by tests and by the planner:
    ``transport`` accepts an ``httpx.MockTransport``; ``cache`` is any object
    with ``get``/``set``; ``sleep``/``clock`` are injectable.
    """

    def __init__(
        self,
        *,
        cache: SqliteCache | None = None,
        transport: httpx.BaseTransport | None = None,
        timeout: float = 20.0,
        auth_token: str | None = None,
        token_provider: Callable[[], str | None] | None = None,
        max_retries: int = 4,
        min_delay: float = 0.3,
        max_delay: float = 0.7,
        sleep: Callable[[float], None] = time.sleep,
        clock: Callable[[], float] = time.monotonic,
        user_agent: str = DEFAULT_USER_AGENT,
    ) -> None:
        self._owns_cache = cache is None
        self.cache = cache or SqliteCache()
        self._auth_token = self._normalize_token(auth_token)
        self._token_provider = token_provider
        self.max_retries = max_retries
        self.min_delay = min_delay
        self.max_delay = max_delay
        self._sleep = sleep
        self._clock = clock
        self._last_request: float | None = None
        self._delay_lock = threading.Lock()
        self._network_semaphore = threading.BoundedSemaphore(2)
        self._http = httpx.Client(
            headers={
                "Content-Type": "application/json",
                "Accept": "application/json",
                "User-Agent": user_agent,
                "Origin": "https://www.ratemyprofessors.com",
                "Referer": "https://www.ratemyprofessors.com/",
            },
            timeout=timeout,
            transport=transport,
            follow_redirects=True,
        )

    @staticmethod
    def _normalize_token(token: str | None) -> str | None:
        if not token:
            return None
        token = token.strip()
        if token.lower().startswith("bearer "):
            token = token[7:].strip()
        return token or None

    def refresh_token(self) -> str | None:
        """Re-read the auth token from ``token_provider`` (rotation hook)."""
        if self._token_provider is not None:
            self._auth_token = self._normalize_token(self._token_provider())
        return self._auth_token

    def _headers(self) -> dict[str, str]:
        headers: dict[str, str] = {}
        if self._auth_token:
            headers["Authorization"] = f"Bearer {self._auth_token}"
        return headers

    def _polite_delay(self) -> None:
        with self._delay_lock:
            now = self._clock()
            if self._last_request is not None:
                wait = self._last_request + random.uniform(
                    self.min_delay, self.max_delay
                ) - now
                if wait > 0:
                    self._sleep(wait)
            self._last_request = self._clock()

    def _post(
        self,
        query: str,
        operation_name: str,
        variables: Mapping[str, Any],
    ) -> dict[str, Any]:
        key = make_key(query, operation_name, variables)
        cached = self.cache.get(key)
        if cached is not None:
            return cached

        body = {
            "query": query,
            "operationName": operation_name,
            "variables": variables,
        }
        attempt = 0
        refreshed = False
        while True:
            self._polite_delay()
            try:
                with self._network_semaphore:
                    response = self._http.post(
                        GRAPHQL_URL, json=body, headers=self._headers()
                    )
            except httpx.RequestError as exc:
                if attempt >= self.max_retries:
                    raise RMPNetworkError(f"request failed: {exc}") from exc
                self._retry_sleep(attempt)
                attempt += 1
                continue

            if response.status_code == 200:
                content_type = response.headers.get("content-type", "")
                if "html" in content_type.lower():
                    raise Blocked("received HTML instead of JSON (Cloudflare/WAF?)")
                try:
                    payload = response.json()
                except json.JSONDecodeError as exc:
                    raise Blocked(f"non-JSON response: {response.text[:200]}") from exc
                if not isinstance(payload, dict):
                    raise RMPError("unexpected GraphQL response shape")
                if payload.get("errors"):
                    raise GraphQLError(payload["errors"])
                data = payload.get("data")
                if not isinstance(data, dict):
                    raise RMPError("GraphQL response has no data")
                self.cache.set(key, data)
                return data

            if 500 <= response.status_code < 600:
                if attempt >= self.max_retries:
                    raise RMPError(f"server error {response.status_code}")
                self._retry_sleep(attempt, response.headers.get("retry-after"))
                attempt += 1
                continue

            if response.status_code == 429:
                retry_after = self._retry_after_seconds(response.headers.get("retry-after"))
                if attempt >= self.max_retries:
                    raise RateLimited("rate limited by RMP", retry_after=retry_after)
                self._retry_sleep(attempt, response.headers.get("retry-after"))
                attempt += 1
                continue

            if response.status_code in (401, 403):
                text = response.text.lower()
                if "cloudflare" in text or "just a moment" in text or "attention required" in text:
                    raise Blocked("Cloudflare challenge")
                if not refreshed and self._token_provider is not None:
                    self.refresh_token()
                    refreshed = True
                    continue
                raise Blocked(f"HTTP {response.status_code} from RMP")
            raise RMPError(f"unexpected HTTP {response.status_code}: {response.text[:200]}")

    @staticmethod
    def _retry_after_seconds(header: str | None) -> float | None:
        if not header:
            return None
        try:
            return float(header)
        except ValueError:
            return None

    def _retry_sleep(self, attempt: int, retry_after: str | None = None) -> None:
        retry_after_seconds = self._retry_after_seconds(retry_after)
        if retry_after_seconds is not None:
            self._sleep(min(retry_after_seconds, 30.0))
            return
        backoff = min(30.0, (0.5 * (2**attempt)) * random.uniform(0.5, 1.5))
        self._sleep(backoff)

    # ------------------------------------------------------------------ API

    def search_professors(
        self, name: str, school_id: int | None = DEFAULT_SCHOOL_ID
    ) -> list[Professor]:
        """Search teachers by name, optionally restricted to one school."""
        school_gid = encode_global_id("School", school_id) if school_id else None
        search_query: dict[str, Any] = {"text": name, "fallback": True}
        if school_gid:
            # TeacherSearchQuery.schoolID is what actually filters the search;
            # the top-level $schoolID only drives the sidebar school card.
            search_query["schoolID"] = school_gid
        variables: dict[str, Any] = {
            "query": search_query,
            "schoolID": school_gid,
            "includeSchoolFilter": school_gid is not None,
        }
        data = self._post(
            queries.SEARCH_PROFESSORS_QUERY,
            queries.SEARCH_PROFESSORS_QUERY_OPERATION,
            variables,
        )
        teachers = ((data.get("search") or {}).get("teachers") or {})
        edges = teachers.get("edges") or []
        return [
            _parse_professor(edge["node"])
            for edge in edges
            if isinstance(edge, dict) and edge.get("node")
        ]

    def get_professor(self, rmp_id: int | str) -> Professor:
        """Fetch one professor by legacy integer id or GraphQL global id."""
        data = self._post(
            queries.PROFESSOR_DETAIL_QUERY,
            queries.PROFESSOR_DETAIL_QUERY_OPERATION,
            {"id": _global_id(rmp_id, "Teacher")},
        )
        node = data.get("node")
        if not node:
            raise NotFound(f"professor {rmp_id} not found")
        professor = _parse_professor(node)
        if professor.num_ratings > 0 and not professor.recent_ratings:
            professor.recent_ratings = self.get_ratings(rmp_id, count=20)
        return professor

    def get_ratings(
        self,
        rmp_id: int | str,
        *,
        count: int = 20,
        cursor: str | None = None,
    ) -> list[Rating]:
        """Fetch a page of reviews (max 20 per request in the web client)."""
        data = self._post(
            queries.RATINGS_QUERY,
            queries.RATINGS_QUERY_OPERATION,
            {
                "count": count,
                "id": _global_id(rmp_id, "Teacher"),
                "courseFilter": None,
                "cursor": cursor,
            },
        )
        node = data.get("node") or {}
        connection = node.get("ratings") or {}
        ratings: list[Rating] = []
        for edge in connection.get("edges") or []:
            if isinstance(edge, dict) and edge.get("node"):
                try:
                    ratings.append(Rating.model_validate(edge["node"]))
                except ValidationError:
                    continue
        return ratings

    def get_school(self, school_id: int | str = DEFAULT_SCHOOL_ID) -> School:
        data = self._post(
            queries.SCHOOL_QUERY,
            queries.SCHOOL_QUERY_OPERATION,
            {"id": _global_id(school_id, "School")},
        )
        node = data.get("school") or data.get("node")
        if not node:
            raise NotFound(f"school {school_id} not found")
        if node.get("__typename") not in (None, "School"):
            raise NotFound(f"id {school_id} is not a school")
        return School.model_validate(node)

    def get_professor_by_name(
        self, name: str, school_id: int = DEFAULT_SCHOOL_ID
    ) -> Match:
        from .match import rank_candidates

        return rank_candidates(name, self.search_professors(name, school_id=school_id))

    def close(self) -> None:
        self._http.close()
        if self._owns_cache:
            self.cache.close()

    def __enter__(self) -> "RMPClient":
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.close()
