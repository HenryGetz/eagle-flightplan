"""Hermetic stdio smoke test for CI: no credentials, no network, no writes."""

from __future__ import annotations

import sys
from pathlib import Path

from mcp import Client
from mcp.client.stdio import StdioServerParameters

ROOT = Path(__file__).resolve().parents[1]

EXPECTED_TOOLS = {
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
    "unw_registration_window",
    "unw_remaining",
    "unw_roadmap",
    "unw_search",
    "unw_session",
    "unw_terms",
}


async def test_stdio_server_starts_without_network_or_credentials() -> None:
    params = StdioServerParameters(
        command=sys.executable,
        args=["-m", "unw_planner.mcp_server"],
        cwd=str(ROOT),
        env={
            "PYTHONPATH": str(ROOT / "src"),
            "UNW_AUTO_LOGIN": "0",
            "PYTHONUNBUFFERED": "1",
        },
    )
    async with Client(params, read_timeout_seconds=30) as client:
        listing = await client.list_tools()
        tools = {tool.name: tool for tool in listing.tools}
        assert set(tools) == EXPECTED_TOOLS
        assert len(tools) == 20
        for tool in tools.values():
            assert (tool.description or "").strip(), tool.name
            assert tool.input_schema.get("type") == "object"
        assert tools["unw_banner_plan_delete"].input_schema["required"] == [
            "term",
            "plan",
            "confirm",
            "confirm_name",
        ]
        instructions = client.instructions or ""
        assert 30 <= len(instructions.splitlines()) <= 60
        assert "Confirmation is ENFORCED" in instructions
