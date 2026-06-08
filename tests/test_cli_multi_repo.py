"""CLI-level tests for multi-repo tasks: `gw new --with-project`, `gw task add-repo`,
and multi-repo teardown via `gw task rm`."""

import subprocess
from pathlib import Path

from typer.testing import CliRunner

from goblin_watcher import paths, state
from goblin_watcher.cli import app


def _init_repo(path: Path) -> None:
    path.mkdir(parents=True, exist_ok=True)
    subprocess.run(["git", "init", "-q", "-b", "main", str(path)], check=True)
    subprocess.run(["git", "-C", str(path), "config", "user.email", "t@t"], check=True)
    subprocess.run(["git", "-C", str(path), "config", "user.name", "tester"], check=True)
    (path / "README.md").write_text("hi")
    subprocess.run(["git", "-C", str(path), "add", "."], check=True)
    subprocess.run(["git", "-C", str(path), "commit", "-qm", "init"], check=True)


def _two_projects(tmp_path: Path) -> tuple[Path, Path]:
    alpha = tmp_path / "alpha"
    beta = tmp_path / "beta"
    _init_repo(alpha)
    _init_repo(beta)
    runner = CliRunner()
    runner.invoke(app, ["project", "new", "alpha", "--dir", str(alpha)])
    runner.invoke(app, ["project", "new", "beta", "--dir", str(beta)])
    return alpha, beta


def test_new_with_project_creates_multi_repo_workspace(isolated_xdg: Path, tmp_path: Path) -> None:
    _two_projects(tmp_path)
    runner = CliRunner()
    res = runner.invoke(
        app,
        [
            "new",
            "--project",
            "alpha",
            "--with-project",
            "beta",
            "--branch-name",
            "shared-feat",
            "--no-launch",
        ],
    )
    assert res.exit_code == 0, res.output

    proj = state.get_project("alpha")
    [task] = state.list_tasks(proj)
    assert task.is_multi_repo
    assert [r.project for r in task.all_repos()] == ["alpha", "beta"]
    ws = paths.task_workspace(task.id)
    assert task.workspace_path == ws
    assert (ws / "alpha" / "README.md").exists()
    assert (ws / "beta" / "README.md").exists()


def test_new_with_project_rejects_dir_source(isolated_xdg: Path, tmp_path: Path) -> None:
    alpha, _ = _two_projects(tmp_path)
    runner = CliRunner()
    res = runner.invoke(app, ["new", "--dir", str(alpha), "--with-project", "beta", "--no-launch"])
    assert res.exit_code != 0
    assert "not supported with --dir" in str(res.exception)


def test_add_repo_promotes_single_to_multi(isolated_xdg: Path, tmp_path: Path) -> None:
    _two_projects(tmp_path)
    runner = CliRunner()
    runner.invoke(app, ["new", "--project", "alpha", "--branch-name", "shared-feat", "--no-launch"])
    proj = state.get_project("alpha")
    [task] = state.list_tasks(proj)
    assert not task.is_multi_repo

    res = runner.invoke(app, ["task", "add-repo", task.id, "beta"])
    assert res.exit_code == 0, res.output

    [updated] = state.list_tasks(proj)
    assert updated.is_multi_repo
    assert updated.workspace_path is not None
    assert [r.project for r in updated.all_repos()] == ["alpha", "beta"]
    assert (updated.workspace_path / "beta" / "README.md").exists()
    # Primary worktree relocated into the workspace.
    assert updated.worktree_path == updated.workspace_path / "alpha"


def test_add_repo_rejects_duplicate(isolated_xdg: Path, tmp_path: Path) -> None:
    _two_projects(tmp_path)
    runner = CliRunner()
    runner.invoke(app, ["new", "--project", "alpha", "--branch-name", "shared-feat", "--no-launch"])
    proj = state.get_project("alpha")
    [task] = state.list_tasks(proj)
    res = runner.invoke(app, ["task", "add-repo", task.id, "alpha"])
    assert res.exit_code != 0
    assert "already includes" in str(res.exception)


def test_rm_tears_down_all_worktrees_and_workspace(isolated_xdg: Path, tmp_path: Path) -> None:
    _two_projects(tmp_path)
    runner = CliRunner()
    runner.invoke(
        app,
        [
            "new",
            "--project",
            "alpha",
            "--with-project",
            "beta",
            "--branch-name",
            "shared-feat",
            "--no-launch",
        ],
    )
    proj = state.get_project("alpha")
    [task] = state.list_tasks(proj)
    ws = task.workspace_path
    assert ws is not None and ws.exists()

    res = runner.invoke(app, ["task", "rm", task.id, "--force"])
    assert res.exit_code == 0, res.output
    assert state.list_tasks(proj) == []
    assert not ws.exists()


def test_task_show_lists_repos_for_multi_repo(isolated_xdg: Path, tmp_path: Path) -> None:
    _two_projects(tmp_path)
    runner = CliRunner()
    runner.invoke(
        app,
        [
            "new",
            "--project",
            "alpha",
            "--with-project",
            "beta",
            "--branch-name",
            "shared-feat",
            "--no-launch",
        ],
    )
    proj = state.get_project("alpha")
    [task] = state.list_tasks(proj)
    res = runner.invoke(app, ["task", "show", task.id])
    assert res.exit_code == 0, res.output
    assert "workspace" in res.output
    assert "alpha" in res.output
    assert "beta" in res.output
