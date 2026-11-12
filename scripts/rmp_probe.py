#!/usr/bin/env python3
"""Resolve professor names against Rate My Professors and print JSON.

Usage:
    python scripts/rmp_probe.py "Jackie Glenny"
    python scripts/rmp_probe.py --school 706 "Glenny" "J. Glenny"
    python scripts/rmp_probe.py --no-cache "Jackie Glenny"

Run from the repository root (or with PYTHONPATH=src) so ``flightplan`` is
importable.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

SRC = Path(__file__).resolve().parents[1] / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from flightplan.rmp.client import RMPClient, RMPError  # noqa: E402
from flightplan.rmp.match import match_professor  # noqa: E402
from flightplan.rmp.models import Match, Professor  # noqa: E402


def professor_payload(professor: Professor | None) -> dict | None:
    if professor is None:
        return None
    payload = professor.model_dump(mode="json")
    payload["full_name"] = professor.full_name
    payload["profile_url"] = professor.profile_url
    return payload


def match_payload(match: Match) -> dict:
    return {
        "query": match.query,
        "professor": professor_payload(match.professor),
        "confidence": match.confidence,
        "alternatives": [
            {
                "rmp_id": professor.rmp_id,
                "name": professor.full_name,
                "department": professor.department,
                "avg_rating": professor.avg_rating,
                "avg_difficulty": professor.avg_difficulty,
                "num_ratings": professor.num_ratings,
                "profile_url": professor.profile_url,
            }
            for professor in match.alternatives
        ],
        "reason": match.reason,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("names", nargs="+", help="Professor names to resolve")
    parser.add_argument(
        "--school", type=int, default=706, help="RMP school id (default: 706 UNW St. Paul)"
    )
    parser.add_argument(
        "--no-cache", action="store_true", help="Bypass the SQLite cache for this run"
    )
    parser.add_argument(
        "--no-detail",
        action="store_true",
        help="Do not fetch the full professor detail (fewer requests)",
    )
    parser.add_argument("--pretty", action="store_true", help="Indent JSON output")
    args = parser.parse_args(argv)

    client = RMPClient()
    if args.no_cache:
        client.cache.clear()
    results: list[dict] = []
    try:
        for name in args.names:
            match = match_professor(name, school_id=args.school, client=client)
            if match.professor is not None and not args.no_detail:
                match.professor = client.get_professor(match.professor.rmp_id)
            results.append(match_payload(match))
    except RMPError as exc:
        print(json.dumps({"error": type(exc).__name__, "message": str(exc)}))
        return 1
    finally:
        client.close()

    payload: object = results[0] if len(results) == 1 else results
    print(json.dumps(payload, indent=2 if args.pretty else None, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
