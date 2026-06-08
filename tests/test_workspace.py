import subprocess
from pathlib import Path

import pytest
from typer.testing import CliRunner

from goblin_watcher import paths, state, workspace
from goblin_watcher.cli import app
from goblin_watcher.errors import GoblinError
from goblin_watcher.models import Task


def _init_repo(path: Path) -> None:
    path.mkdir(parents=True, exist_ok=True)
    subprocess.run(["git", "init", "-q", "-b", "main", str(path)], check=True)
    subprocess.run(["git", "-C", str(path), "config", "user.email", "t@t"], check=True)
    subprocess.run(["git", "-C", str(path), "config", "user.name", "tester"], check=True)
    (path / "README.md").write_text("hi")
    subprocess.run(["git", "-C", str(path), "add", "."], check=True)
    subprocess.run(["git", "-C", str(path), "commit", "-qm", "init"], check=True)


def _setup(tmp_path: Path) -> Task:
    """Register two projects (alpha, beta) and a single-repo task in alpha."""
    alpha = tmp_path / "alpha"
    beta = tmp_path / "beta"
    _init_repo(alpha)
    _init_repo(beta)
    runner = CliRunner()
    runner.invoke(app, ["project", "new", "alpha", "--dir", str(alpha)])
    runner.invoke(app, ["project", "new", "beta", "--dir", str(beta)])
    runner.invoke(app, ["new", "--project", "alpha", "--branch-name", "shared-feat", "--no-launch"])
    proj = state.get_project("alpha")
    [task] = state.list_tasks(proj)
    return task


def test_promote_moves_primary_into_workspace(isolated_xdg: Path, tmp_path: Path) -> None:
    task = _setup(tmp_path)
    assert not task.is_multi_repo
    promoted = workspace.promote_to_workspace(task)
    assert promoted.workspace_path == paths.task_workspace(task.id)
    assert promoted.worktree_path == paths.task_workspace(task.id) / "alpha"
    assert (promoted.worktree_path / "README.md").exists()
    assert not task.worktree_path.exists()  # old location gone


def test_promote_is_idempotent(isolated_xdg: Path, tmp_path: Path) -> None:
    task = _setup(tmp_path)
    once = workspace.promote_to_workspace(task)
    twice = workspace.promote_to_workspace(once)
    assert twice.workspace_path == once.workspace_path
    assert twice.worktree_path == once.worktree_path


def test_promote_refuses_dirty_primary(isolated_xdg: Path, tmp_path: Path) -> None:
    task = _setup(tmp_path)
    (task.worktree_path / "README.md").write_text("dirty")
    with pytest.raises(GoblinError, match="uncommitted changes"):
        workspace.promote_to_workspace(task)


def test_attach_repo_builds_multi_repo_task(isolated_xdg: Path, tmp_path: Path) -> None:
    task = _setup(tmp_path)
    beta = state.get_project("beta")
    updated = workspace.attach_repo(task, beta)
    assert updated.is_multi_repo
    assert [r.project for r in updated.all_repos()] == ["alpha", "beta"]
    beta_repo = updated.secondary_repos[0]
    assert beta_repo.worktree_path == updated.workspace_path / "beta"
    assert (beta_repo.worktree_path / "README.md").exists()
    assert beta_repo.base_branch == "main"


def test_attach_repo_rejects_duplicate_project(isolated_xdg: Path, tmp_path: Path) -> None:
    task = _setup(tmp_path)
    alpha = state.get_project("alpha")
    with pytest.raises(GoblinError, match="already includes"):
        workspace.attach_repo(task, alpha)


def test_derive_branch_honors_new_prefix_without_linear(isolated_xdg: Path, tmp_path: Path) -> None:
    task = _setup(tmp_path)
    beta = state.get_project("beta").model_copy(update={"branch_prefix": "wip/"})
    assert workspace.derive_branch(task, beta, None) == "wip/shared-feat"
    assert workspace.derive_branch(task, beta, "explicit") == "wip/explicit"
