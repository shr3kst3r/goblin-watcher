"""Tests for `gw cd`: prints the worktree path on stdout, picks via --project."""

import subprocess
from pathlib import Path

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


def _bootstrap_one_project(tmp_path: Path) -> None:
    repo = tmp_path / "alpha"
    _init_repo(repo)
    runner = CliRunner()
    runner.invoke(app, ["project", "new", "alpha", "--dir", str(repo)])
    runner.invoke(app, ["new", "--branch-name", "spike/foo", "--project", "alpha", "--no-launch"])


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


def test_cd_prints_worktree_path_for_task_id(isolated_xdg: Path, tmp_path: Path) -> None:
    _bootstrap_one_project(tmp_path)
    proj = state.get_project("alpha")
    [task] = state.list_tasks(proj)

    runner = CliRunner()
    res = runner.invoke(app, ["cd", task.id])
    assert res.exit_code == 0, res.output
    # Stdout should be exactly the worktree path (with a trailing newline).
    assert res.output.strip() == str(task.worktree_path)


def test_cd_project_flag_scopes_lookup(isolated_xdg: Path, tmp_path: Path) -> None:
    """Shared task id across two projects must resolve via --project."""
    _bootstrap_two_projects(tmp_path)
    proj_b = state.get_project("beta")
    [task_b] = state.list_tasks(proj_b)

    runner = CliRunner()
    res = runner.invoke(app, ["cd", task_b.id, "--project", "beta"])
    assert res.exit_code == 0, res.output
    assert res.output.strip() == str(task_b.worktree_path)


def test_cd_unknown_target_errors(isolated_xdg: Path, tmp_path: Path) -> None:
    _bootstrap_one_project(tmp_path)
    runner = CliRunner()
    res = runner.invoke(app, ["cd", "no-such-task"])
    assert res.exit_code != 0


def test_cd_unknown_project_errors(isolated_xdg: Path, tmp_path: Path) -> None:
    _bootstrap_one_project(tmp_path)
    runner = CliRunner()
    res = runner.invoke(app, ["cd", "spike-foo", "--project", "nope"])
    assert res.exit_code != 0


def test_cd_prints_workspace_for_multi_repo_task(isolated_xdg: Path, tmp_path: Path) -> None:
    """A multi-repo task's agent runs in the workspace; `gw cd` should land there."""
    from goblin_watcher.models import TaskRepo

    _bootstrap_one_project(tmp_path)
    proj = state.get_project("alpha")
    [task] = state.list_tasks(proj)
    ws = tmp_path / "ws"
    task = task.model_copy(
        update={
            "workspace_path": ws,
            "worktree_path": ws / "alpha",
            "secondary_repos": [
                TaskRepo(project="beta", branch="b", worktree_path=ws / "beta", base_branch="main")
            ],
        }
    )
    state.save_task(proj, task)

    runner = CliRunner()
    res = runner.invoke(app, ["cd", task.id])
    assert res.exit_code == 0, res.output
    assert res.stdout.strip() == str(ws)
