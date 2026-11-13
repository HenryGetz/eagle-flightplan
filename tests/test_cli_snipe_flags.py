"""CLI safety-flag tests: rehearsal and live are mutually exclusive."""

from __future__ import annotations

from typer.testing import CliRunner

from eagle_flightplan.cli import app

runner = CliRunner()


def test_dry_run_and_live_are_mutually_exclusive() -> None:
    result = runner.invoke(app, ["snipe", "--term", "202620", "--dry-run", "--live"])
    assert result.exit_code == 2
    assert "mutually exclusive" in result.output
    assert "Traceback" not in result.output


def test_no_dry_run_requires_live() -> None:
    result = runner.invoke(app, ["snipe", "--term", "202620", "--no-dry-run"])
    assert result.exit_code == 2
    assert "requires --live" in result.output
    assert "Traceback" not in result.output


def test_dry_run_and_no_dry_run_are_mutually_exclusive() -> None:
    result = runner.invoke(
        app, ["snipe", "--term", "202620", "--dry-run", "--no-dry-run"]
    )
    assert result.exit_code == 2
    assert "mutually exclusive" in result.output
    assert "Traceback" not in result.output
