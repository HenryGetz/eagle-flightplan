"""MCP registrar: merge, idempotency, backups, invalid JSON, client paths."""

from __future__ import annotations

import json
from pathlib import Path

from eagle_flightplan.register_mcp import config_paths, merge_client, register_clients, server_entry


def make_root(tmp_path: Path) -> Path:
    root = tmp_path / "repo"
    exe = root / ".venv" / "bin" / "eagle-flightplan-mcp"
    exe.parent.mkdir(parents=True)
    exe.write_text("#!/bin/sh\nexit 0\n")
    exe.chmod(0o755)
    return root


def test_merge_preserves_other_servers_and_entry(tmp_path):
    path = tmp_path / "mcp.json"
    path.write_text(json.dumps({"mcpServers": {"other": {"command": "other"}}}))
    entry = {"command": "/x/eagle-flightplan-mcp", "args": [], "env": {"PYTHONUNBUFFERED": "1"}}
    changed, message, backup = merge_client(path, entry)
    assert changed is True and backup is not None and backup.exists()
    data = json.loads(path.read_text())
    assert data["mcpServers"]["other"] == {"command": "other"}
    assert data["mcpServers"]["eagle-flightplan"] == entry
    assert "registered" in message


def test_merge_is_idempotent(tmp_path):
    path = tmp_path / "mcp.json"
    entry = {"command": "/x/eagle-flightplan-mcp", "args": [], "env": {"PYTHONUNBUFFERED": "1"}}
    assert merge_client(path, entry)[0] is True
    changed, message, backup = merge_client(path, entry)
    assert changed is False
    assert "already registered" in message
    assert backup is None


def test_invalid_json_is_backed_up_and_replaced(tmp_path):
    path = tmp_path / "mcp.json"
    path.write_text("{ not valid")
    entry = {"command": "/x/eagle-flightplan-mcp", "args": [], "env": {"PYTHONUNBUFFERED": "1"}}
    changed, message, backup = merge_client(path, entry)
    assert changed is True
    assert backup is not None and backup.read_text() == "{ not valid"
    assert json.loads(path.read_text())["mcpServers"]["eagle-flightplan"] == entry
    assert "replaced invalid JSON" in message


def test_register_all_uses_present_clients_and_exact_command(tmp_path):
    root = make_root(tmp_path)
    home = tmp_path / "home"
    (home / ".omp" / "agent").mkdir(parents=True)
    (home / ".config" / "Claude").mkdir(parents=True)
    # no ~/.cursor => all should skip Cursor
    ok, message = register_clients(root=root, clients="all", home=home, platform="linux")
    assert ok is True
    assert "omp" in message and "claude" in message and "cursor" not in message
    paths = config_paths(home, platform="linux")
    entry = json.loads(paths["omp"].read_text())["mcpServers"]["eagle-flightplan"]
    assert entry["command"] == str(root / ".venv" / "bin" / "eagle-flightplan-mcp")
    assert entry["args"] == []
    assert entry["env"] == {"PYTHONUNBUFFERED": "1"}
    assert paths["omp"].exists() and paths["claude"].exists()
    assert not paths["cursor"].exists()


def test_explicit_client_creates_parent(tmp_path):
    root = make_root(tmp_path)
    home = tmp_path / "home"
    ok, message = register_clients(root=root, clients="cursor", home=home, platform="linux")
    assert "cursor" in message
    assert ok is True
    assert (home / ".cursor" / "mcp.json").exists()


def test_platform_paths(tmp_path):
    home = tmp_path / "home"
    assert config_paths(home, platform="darwin")["claude"].parts[-4:] == (
        "Library", "Application Support", "Claude", "claude_desktop_config.json"
    )
    assert config_paths(home, platform="linux")["claude"] == (
        home / ".config" / "Claude" / "claude_desktop_config.json"
    )


def test_server_entry_falls_back_to_module(tmp_path):
    root = tmp_path / "empty"
    root.mkdir()
    entry = server_entry(root)
    assert entry["command"]
    assert entry["env"]["PYTHONUNBUFFERED"] == "1"
    assert entry["args"] == [] or entry["args"] == ["-m", "eagle_flightplan.mcp_server"]
