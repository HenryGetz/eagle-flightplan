"""Rate My Professors GraphQL client, cache and name matcher."""

from .cache import SqliteCache, default_cache_path, make_key
from .client import (
    Blocked,
    GraphQLError,
    NotFound,
    RMPClient,
    RMPError,
    RMPNetworkError,
    RateLimited,
    encode_global_id,
)
from .match import match_professor, normalize_name, rank_candidates
from .models import (
    CourseTaught,
    Match,
    Professor,
    Rating,
    RatingDistribution,
    RatingTag,
    RelatedProfessor,
    School,
    SchoolRef,
)

__all__ = [
    "Blocked",
    "CourseTaught",
    "GraphQLError",
    "Match",
    "NotFound",
    "Professor",
    "RMPClient",
    "RMPError",
    "RMPNetworkError",
    "RateLimited",
    "Rating",
    "RatingDistribution",
    "RatingTag",
    "RelatedProfessor",
    "School",
    "SchoolRef",
    "SqliteCache",
    "default_cache_path",
    "encode_global_id",
    "make_key",
    "match_professor",
    "normalize_name",
    "rank_candidates",
]
