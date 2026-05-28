"""Tests for `gw run`, covering the --project filter."""

import subprocess
from pathlib import Path
from unittest.mock import patch

from typer.testing import CliRunner

from goblin_watcher import state
from goblin_watcher.cli import app


def _init_repo(path: Path) -> None:
    path.mkdir(parents=True, exist_ok=True)
    subprocess.run(["git", "init", "-q", "-b", "main", str(path)], check=True)
    subprocess.run(["git", "-C", str(path), "config", "user.email", "t@t"], check=True)
    subprocess.run(["git", "-C", str(path), "config", "user.name", "tester"], check=True)
    (path / "README.md").write_text("hi")
    subprocess.run(["git", "-C", str(path), "add", "."], check=True)
    subprocess.run(["git", "-C", str(path), "commit", "-qm", "init"], check=True)


def _bootstrap_two_projects(tmp_path: Path) -> None:
    repo_a = tmp_path / "alpha"
    repo_b = tmp_path / "beta"
    _init_repo(repo_a)
    _init_repo(repo_b)
    runner = CliRunner()
    runner.invoke(app, ["project", "new", "alpha", "--dir", str(repo_a)])
    runner.invoke(app, ["project", "new", "beta", "--dir", str(repo_b)])
    runner.invoke(app, ["new", "--branch-name", "spike/foo", "--project", "alpha", "--no-launch"])
    runner.invoke(app, ["new", "--branch-name", "spike/foo", "--project", "beta", "--no-launch"])


def test_run_project_flag_scopes_task_lookup(isolated_xdg: Path, tmp_path: Path) -> None:
    """A task id that exists in two projects resolves to the --project one."""
    _bootstrap_two_projects(tmp_path)
    proj_a = state.get_project("alpha")
    proj_b = state.get_project("beta")
    [task_a] = state.list_tasks(proj_a)
    [task_b] = state.list_tasks(proj_b)
    assert task_a.id == task_b.id  # shared "spike-foo" id; only --project disambiguates.

    runner = CliRunner()
    with patch(
        "goblin_watcher.commands.run.launch_agent",
        return_value=(0, task_b),
    ) as launch:
        res = runner.invoke(app, ["run", task_b.id, "--project", "beta"])
    assert res.exit_code == 0, res.output
    kwargs = launch.call_args.kwargs
    assert kwargs["project"].name == "beta"
    assert kwargs["task"].id == task_b.id


def test_run_project_flag_unknown_errors(isolated_xdg: Path, tmp_path: Path) -> None:
    _bootstrap_two_projects(tmp_path)
    runner = CliRunner()
    res = runner.invoke(app, ["run", "spike-foo", "--project", "nope"])
    assert res.exit_code != 0


def test_run_new_and_session_are_mutually_exclusive(isolated_xdg: Path, tmp_path: Path) -> None:
    _bootstrap_two_projects(tmp_path)
    runner = CliRunner()
    res = runner.invoke(app, ["run", "spike-foo", "--project", "alpha", "--new", "--session", "x"])
    assert res.exit_code != 0
    assert res.exception is not None
    assert "mutually exclusive" in str(res.exception)


def test_run_prompt_and_session_are_mutually_exclusive(isolated_xdg: Path, tmp_path: Path) -> None:
    _bootstrap_two_projects(tmp_path)
    runner = CliRunner()
    res = runner.invoke(
        app,
        ["run", "spike-foo", "--project", "alpha", "--session", "x", "--prompt", "do work"],
    )
    assert res.exit_code != 0
    assert res.exception is not None
    assert "--prompt requires a fresh session" in str(res.exception)


def test_run_prompt_implies_fresh_and_seeds_prompt(isolated_xdg: Path, tmp_path: Path) -> None:
    _bootstrap_two_projects(tmp_path)
    proj_a = state.get_project("alpha")
    [task_a] = state.list_tasks(proj_a)

    runner = CliRunner()
    with patch(
        "goblin_watcher.commands.run.launch_agent",
        return_value=(0, task_a),
    ) as launch:
        res = runner.invoke(
            app,
            [
                "run",
                task_a.id,
                "--project",
                "alpha",
                "--prompt",
                "Refactor the foo module.",
            ],
        )
    assert res.exit_code == 0, res.output
    choice = launch.call_args.kwargs["choice"]
    # Fresh, not Resume — --prompt implies a new session.
    assert type(choice).__name__ == "Fresh"
    assert "Refactor the foo module." in choice.prompt
    assert "Wait for my next message" not in choice.prompt


def test_run_project_flag_task_missing_in_scope_errors(isolated_xdg: Path, tmp_path: Path) -> None:
    """A task id present in alpha but not beta must not silently fall back to alpha
    when --project beta is set."""
    _bootstrap_two_projects(tmp_path)
    proj_b = state.get_project("beta")
    [task_b] = state.list_tasks(proj_b)
    # Add a second task only to alpha.
    runner = CliRunner()
    runner.invoke(
        app, ["new", "--branch-name", "spike/only-alpha", "--project", "alpha", "--no-launch"]
    )

    res = runner.invoke(app, ["run", "spike-only-alpha", "--project", "beta"])
    assert res.exit_code != 0
    assert "spike-only-alpha" in res.output or (
        res.exception is not None and "spike-only-alpha" in str(res.exception)
    )
    # Sanity: the same id resolves fine without the filter.
    with patch(
        "goblin_watcher.commands.run.launch_agent",
        return_value=(0, task_b),
    ):
        res2 = runner.invoke(app, ["run", "spike-only-alpha"])
    assert res2.exit_code == 0, res2.output
