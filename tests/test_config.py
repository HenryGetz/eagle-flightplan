"""Data-root resolution for editable vs wheel installs."""

from __future__ import annotations

from pathlib import Path

from flightplan.config import login_command, resolve_project_root


def _config_file(root: Path) -> Path:
    package = root / "src" / "flightplan"
    package.mkdir(parents=True)
    path = package / "config.py"
    path.write_text("")
    return path


def test_editable_root_is_the_checkout(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "pyproject.toml").write_text("")
    path = _config_file(repo)
    assert resolve_project_root(file_path=path, environ={}, cwd=tmp_path) == repo


def test_wheel_root_uses_unw_home(tmp_path):
    site = tmp_path / "venv" / "lib" / "python3.12" / "site-packages" / "flightplan"
    site.mkdir(parents=True)
    path = site / "config.py"
    path.write_text("")
    home = tmp_path / "data"
    assert (
        resolve_project_root(file_path=path, environ={"FLIGHTPLAN_HOME": str(home)}, cwd=tmp_path)
        == home
    )


def test_wheel_root_falls_back_to_cwd(tmp_path):
    site = tmp_path / "venv" / "lib" / "python3.12" / "site-packages" / "flightplan"
    site.mkdir(parents=True)
    path = site / "config.py"
    path.write_text("")
    work = tmp_path / "work"
    work.mkdir()
    assert resolve_project_root(file_path=path, environ={}, cwd=work) == work


def test_login_command_is_packaged_cli():
    command = login_command()
    assert "-m flightplan.cli login --force" in command
    assert "scripts/login.py" not in command


def test_no_user_facing_hint_mentions_scripts_login():
    source = Path(__file__).resolve().parents[1] / "src" / "flightplan"
    for path in source.rglob("*.py"):
        assert "scripts/login.py" not in path.read_text(encoding="utf-8"), path
