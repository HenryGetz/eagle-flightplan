#!/usr/bin/env python
"""Thin wrapper for ``unw_planner.register_mcp`` (kept for older docs/scripts)."""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from unw_planner.register_mcp import main

if __name__ == "__main__":
    raise SystemExit(main())
