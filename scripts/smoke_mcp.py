#!/usr/bin/env python
"""Smoke-test the UNW MCP server over real stdio.

    python scripts/smoke_mcp.py

Launches ``python -m unw_planner.mcp_server`` as a subprocess, lists the tool
surface and calls six read-only tools. Prints real results and exits non-zero on
any failure (server error, missing tool, empty data, or a failed tool payload).
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
from pathlib import Path
from typing import Any

REPO = Path(__file__).resolve().parents[1]

try:
    from mcp import Client
    from mcp.client.stdio import StdioServerParameters
except ModuleNotFoundError:  # allow `python scripts/smoke_mcp.py` outside the venv
    venv_python = REPO / ".venv" / "bin" / "python"
    if venv_python.exists() and Path(sys.executable).resolve() != venv_python.resolve():
        os.execv(
            str(venv_python),
            [str(venv_python), str(Path(__file__).resolve()), *sys.argv[1:]],
        )
    raise

EXPECTED = {
    "unw_audit",
    "unw_banner_plan_add",
    "unw_banner_plan_create",
    "unw_banner_plan_delete",
    "unw_banner_plan_remove",
    "unw_banner_plans",
    "unw_candidates",
    "unw_conflicts",
    "unw_course",
    "unw_offering_matrix",
    "unw_plan_draft",
    "unw_profile",
    "unw_ratings",
    "unw_register_preview",
    "unw_remaining",
    "unw_roadmap",
    "unw_search",
    "unw_session",
    "unw_terms",
}


def payload(result: Any) -> dict:
    """Extract the JSON dict a UNW tool returned."""
    if getattr(result, "is_error", False):
        text = "".join(
            getattr(block, "text", "") for block in getattr(result, "content", [])
        )
        raise RuntimeError(f"tool reported isError: {text[:500]}")
    if getattr(result, "structured_content", None):
        return result.structured_content
    for block in getattr(result, "content", []):
        if getattr(block, "type", None) == "text":
            return json.loads(block.text)
    raise RuntimeError("tool returned no text content")


async def call(client: Client, name: str, arguments: dict) -> dict:
    data = payload(await client.call_tool(name, arguments))
    print(f"\n=== {name}({json.dumps(arguments)}) ===")
    print(json.dumps(data, indent=1))
    if not data.get("ok", False):
        raise RuntimeError(f"{name} failed: {data.get('error')}")
    return data


async def run() -> int:
    params = StdioServerParameters(
        command=sys.executable,
        args=["-m", "unw_planner.mcp_server"],
        cwd=str(REPO),
        env={"PYTHONPATH": str(REPO / "src")},
    )
    failures: list[str] = []
    async with Client(params, read_timeout_seconds=180) as client:
        listing = await client.list_tools()
        tools = listing.tools
        names = {tool.name for tool in tools}
        print(f"MCP server: {client.server_info.name if client.server_info else '?'}")
        print(f"tools ({len(tools)}):")
        for tool in sorted(tools, key=lambda item: item.name):
            print(f"  {tool.name}: {(tool.description or '').splitlines()[0]}")
        if len(tools) != 19 or names != EXPECTED:
            failures.append(f"tool surface mismatch: {sorted(EXPECTED ^ names)}")
        for tool in tools:
            if not (tool.description or "").strip():
                failures.append(f"tool {tool.name} has no description")
        instructions = client.instructions or ""
        print(f"instructions: {len(instructions.splitlines())} lines")
        if not instructions.strip():
            failures.append("server did not advertise instructions")

        try:
            session = await call(client, "unw_session", {})
            if not session.get("authenticated"):
                failures.append("session is not authenticated")

            profile = await call(client, "unw_profile", {})
            if not profile.get("name") or not profile.get("majors"):
                failures.append("profile is missing name/majors")

            terms = await call(client, "unw_terms", {})
            if not terms.get("terms"):
                failures.append("terms list is empty")
            if not terms.get("next_term"):
                failures.append("no next term identified")

            search = await call(
                client, "unw_search", {"term": "202620", "subject": "CYS", "limit": 2}
            )
            if not search.get("sections"):
                failures.append("CYS 202620 search returned no sections")

            ratings = await call(
                client, "unw_ratings", {"names": ["Zderad, Jonathan"]}
            )
            rated = (ratings.get("ratings") or [{}])[0]
            if not rated.get("rated") or rated.get("rating") is None:
                failures.append("Zderad, Jonathan did not resolve to an RMP rating")

            plans = await call(client, "unw_banner_plans", {"term": "202620"})
            if plans.get("count", 0) < 1:
                failures.append("no Banner plans visible for 202620")
        except Exception as exc:  # noqa: BLE001 - smoke must report, not traceback
            failures.append(f"{type(exc).__name__}: {exc}")

    print("\n=== summary ===")
    if failures:
        for failure in failures:
            print(f"FAIL: {failure}")
        return 1
    print("PASS: server listed 19 tools and all six calls returned real data")
    return 0


def main() -> int:
    return asyncio.run(run())


if __name__ == "__main__":
    raise SystemExit(main())
