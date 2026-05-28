"""Tests for `gw pr open` task resolution (explicit id vs. cwd-based)."""

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


def _bootstrap(tmp_path: Path) -> Path:
    repo = tmp_path / "alpha"
    _init_repo(repo)
    runner = CliRunner()
    runner.invoke(app, ["project", "new", "alpha", "--dir", str(repo)])
    runner.invoke(app, ["new", "--branch-name", "spike/foo", "--no-launch"])
    return repo


def test_pr_open_resolves_task_from_cwd(isolated_xdg: Path, tmp_path: Path, monkeypatch) -> None:
    """README advertises `gw pr open` with no task id. Confirm the command
    resolves the task from cwd when none is passed (like `gw run` does)."""
    _bootstrap(tmp_path)
    proj = state.get_project("alpha")
    [task] = state.list_tasks(proj)

    monkeypatch.chdir(task.worktree_path)
    runner = CliRunner()
    with (
        patch("goblin_watcher.commands.pr.git.push"),
        patch(
            "goblin_watcher.commands.pr.gh.create_pr",
            return_value="https://github.com/x/y/pull/1",
        ),
    ):
        res = runner.invoke(app, ["pr", "open"])
    assert res.exit_code == 0, res.output

    [persisted] = state.list_tasks(proj)
    assert persisted.pr_url == "https://github.com/x/y/pull/1"
    assert persisted.status == "pr-open"


def test_pr_open_without_id_outside_worktree_errors_helpfully(
    isolated_xdg: Path, tmp_path: Path, monkeypatch
) -> None:
    from goblin_watcher.errors import GoblinError

    _bootstrap(tmp_path)
    monkeypatch.chdir(tmp_path)  # not inside any worktree
    runner = CliRunner()
    res = runner.invoke(app, ["pr", "open"])
    assert res.exit_code != 0
    assert isinstance(res.exception, GoblinError)
    assert "task id" in res.exception.message.lower()
