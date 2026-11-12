#!/usr/bin/env python
"""CLI: refresh the cached UNW session.

    python scripts/login.py            # reuse a valid session, else log in
    python scripts/login.py --force    # always drive the browser login
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

import typer
from rich.console import Console

from eagle_flightplan import auth
from eagle_flightplan.config import get_settings

app = typer.Typer(add_completion=False)
console = Console()


@app.command()
def main(
    force: bool = typer.Option(False, "--force", help="Ignore cached state and log in again."),
    headless: bool = typer.Option(True, "--headless/--headed", help="Run the browser headless."),
) -> None:
    settings = get_settings()
    path = auth.login(force=force, settings=settings, headless=headless)
    console.print(f"[green]session state:[/] {path}")
    console.print(f"[green]valid:[/] {auth.session_valid(settings)}")
    console.print(f"[green]age:[/] {auth.state_age_seconds():.0f}s")


if __name__ == "__main__":
    app()
