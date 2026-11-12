"""Merge a stdio MCP entry into local agent/client configuration files.

Read-only for credentials: this module only writes JSON config files, and never
logs or copies environment values. Existing servers are preserved; a backup is
made before any file is replaced. Registering the same entry twice is a no-op.
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import sys
from pathlib import Path

from .config import REPO_ROOT

SERVER_NAME = "eagle-flightplan"
CLIENTS = ("omp", "claude", "cursor", "all")


def config_paths(home: Path | None = None, *, platform: str | None = None) -> dict[str, Path]:
    """Standard MCP config path per client for this platform."""
    home = Path(home).expanduser() if home is not None else Path.home()
    platform = platform or sys.platform
    paths = {
        "omp": home / ".omp" / "agent" / "mcp.json",
        "cursor": home / ".cursor" / "mcp.json",
    }
    if platform == "darwin":
        paths["claude"] = (
            home / "Library" / "Application Support" / "Claude" / "claude_desktop_config.json"
        )
    elif platform.startswith("win"):
        appdata = Path(os.environ.get("APPDATA", home / "AppData" / "Roaming"))
        paths["claude"] = appdata / "Claude" / "claude_desktop_config.json"
    else:
        paths["claude"] = home / ".config" / "Claude" / "claude_desktop_config.json"
    return paths


def server_entry(root: Path | None = None) -> dict:
    """The stdio entry for this checkout/install.

    The console script is preferred (absolute path, per client configs); a wheel
    install without that script falls back to the same interpreter plus the
    module entry point.
    """
    root = Path(root) if root is not None else REPO_ROOT
    exe_name = "eagle-flightplan-mcp.exe" if os.name == "nt" else "eagle-flightplan-mcp"
    script = root / ".venv" / ("Scripts" if os.name == "nt" else "bin") / exe_name
    if script.exists():
        command, args = str(script), []
    else:
        which = shutil.which("eagle-flightplan-mcp")
        if which:
            command, args = which, []
        else:
            command, args = sys.executable, ["-m", "eagle_flightplan.mcp_server"]
    return {"command": command, "args": args, "env": {"PYTHONUNBUFFERED": "1"}}


def _read_config(path: Path) -> tuple[dict, bool]:
    if not path.exists():
        return {}, False
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError, TypeError):
        return {}, True
    if not isinstance(data, dict):
        return {}, True
    return data, False


def _backup(path: Path) -> Path:
    backup = path.with_name(path.name + ".bak")
    shutil.copy2(path, backup)
    return backup


def merge_client(path: Path, entry: dict) -> tuple[bool, str, Path | None]:
    """Merge ``entry`` into ``path``; returns (changed, message, backup)."""
    path = Path(path)
    data, invalid = _read_config(path)
    servers = data.get("mcpServers")
    if not isinstance(servers, dict):
        servers = {}
    if servers.get(SERVER_NAME) == entry and not invalid:
        return False, f"already registered in {path}", None
    backup = _backup(path) if path.exists() else None
    servers[SERVER_NAME] = entry
    data["mcpServers"] = servers
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
    os.replace(tmp, path)
    if invalid:
        return True, f"replaced invalid JSON in {path} (backup: {backup})", backup
    if backup is not None:
        return True, f"registered in {path} (backup: {backup})", backup
    return True, f"registered in {path}", None


def register_clients(
    *,
    root: Path | None = None,
    clients: str = "all",
    home: Path | None = None,
    platform: str | None = None,
) -> tuple[bool, str]:
    """Register the entry in one or more clients; never overwrites other servers."""
    paths = config_paths(home, platform=platform)
    if clients == "all":
        selected = ["omp"]
        selected += [name for name in ("claude", "cursor") if paths[name].parent.exists()]
    elif clients in paths:
        selected = [clients]
    else:
        return False, f"unknown client {clients!r}; choose from {', '.join(CLIENTS)}"
    entry = server_entry(root)
    lines: list[str] = []
    ok = True
    for name in selected:
        changed, message, _backup_path = merge_client(paths[name], entry)
        if not changed and not message.startswith("already registered"):
            ok = False
        lines.append(f"{name}: {message}")
    return ok, "; ".join(lines)


def register_default_clients(root: Path | None = None) -> tuple[bool, str]:
    """Setup-wizard hook: register in omp plus any present Claude/Cursor config."""
    return register_clients(root=root, clients="all")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Register the Eagle Flightplan MCP server")
    parser.add_argument("--root", type=Path, default=REPO_ROOT)
    parser.add_argument("--client", choices=CLIENTS, default="all")
    args = parser.parse_args(argv)
    ok, message = register_clients(root=args.root, clients=args.client)
    print(message)
    return 0 if ok else 1


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
