import subprocess
from pathlib import Path

import pytest

from goblin_watcher import git
from goblin_watcher.errors import GitCommandError


def _init_repo(path: Path, branch: str = "main") -> None:
    path.mkdir(parents=True, exist_ok=True)
    subprocess.run(["git", "init", "-q", "-b", branch, str(path)], check=True)
    subprocess.run(["git", "-C", str(path), "config", "user.email", "t@t"], check=True)
    subprocess.run(["git", "-C", str(path), "config", "user.name", "tester"], check=True)
    (path / "README.md").write_text("hi")
    subprocess.run(["git", "-C", str(path), "add", "."], check=True)
    subprocess.run(["git", "-C", str(path), "commit", "-qm", "init"], check=True)


def test_worktree_add_creates_new_branch_and_dir(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    _init_repo(repo)
    dest = repo / ".worktrees" / "feat-new"
    git.worktree_add(repo, dest, "feat-new", base="main")
    assert (dest / "README.md").exists()
    assert git.current_branch(dest) == "feat-new"
    assert git.branch_exists(repo, "feat-new")


def test_worktree_add_checks_out_existing_branch(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    _init_repo(repo)
    subprocess.run(["git", "-C", str(repo), "branch", "feat-existing"], check=True)
    dest = repo / ".worktrees" / "feat-existing"
    git.worktree_add(repo, dest, "feat-existing")
    assert git.current_branch(dest) == "feat-existing"


def test_worktree_add_clear_error_when_base_has_no_commits(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init", "-q", "-b", "main", str(repo)], check=True)
    dest = repo / ".worktrees" / "feat-new"
    with pytest.raises(GitCommandError, match="does not resolve to a commit"):
        git.worktree_add(repo, dest, "feat-new", base="main")


def test_worktree_remove(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    _init_repo(repo)
    dest = repo / ".worktrees" / "feat-new"
    git.worktree_add(repo, dest, "feat-new", base="main")
    git.worktree_remove(repo, dest)
    assert not dest.exists()


def test_worktree_move_relocates_and_keeps_link(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    _init_repo(repo)
    src = repo / ".worktrees" / "feat-new"
    git.worktree_add(repo, src, "feat-new", base="main")
    dest = tmp_path / "workspace" / "repo"
    git.worktree_move(repo, src, dest)
    assert not src.exists()
    assert (dest / "README.md").exists()
    assert git.current_branch(dest) == "feat-new"
    # Still registered as a worktree of the same repo.
    target = str(dest.resolve())
    assert any(entry.get("worktree", "") == target for entry in git.worktree_list(repo))


def test_worktree_list_returns_entries(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    _init_repo(repo)
    dest = repo / ".worktrees" / "feat-new"
    git.worktree_add(repo, dest, "feat-new", base="main")
    wts = git.worktree_list(repo)
    assert any(entry.get("worktree", "").endswith("feat-new") for entry in wts)
    assert any(entry.get("worktree", "").endswith("repo") for entry in wts)


def test_branch_exists(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    _init_repo(repo)
    assert git.branch_exists(repo, "main")
    assert not git.branch_exists(repo, "nope")


def test_has_uncommitted_changes_initially_false(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    _init_repo(repo)
    assert not git.has_uncommitted_changes(repo)


def test_has_uncommitted_changes_after_edit(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    _init_repo(repo)
    (repo / "README.md").write_text("changed")
    assert git.has_uncommitted_changes(repo)


def test_delete_branch(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    _init_repo(repo)
    subprocess.run(["git", "-C", str(repo), "branch", "throwaway"], check=True)
    assert git.branch_exists(repo, "throwaway")
    git.delete_branch(repo, "throwaway")
    assert not git.branch_exists(repo, "throwaway")
