from datetime import UTC, datetime
from pathlib import Path

from goblin_watcher.models import Task, TaskRepo


def _task(**overrides: object) -> Task:
    base = {
        "id": "eng-123",
        "project": "eng",
        "branch": "eng-123-fix",
        "worktree_path": Path("/repo/.worktrees/eng-123"),
        "base_branch": "main",
        "created_at": datetime(2026, 1, 1, tzinfo=UTC),
    }
    base.update(overrides)
    return Task.model_validate(base)


def test_single_repo_defaults() -> None:
    task = _task()
    assert task.is_multi_repo is False
    assert task.secondary_repos == []
    assert task.workspace_path is None
    assert [r.project for r in task.all_repos()] == ["eng"]


def test_legacy_json_without_new_fields_validates() -> None:
    """Task JSON written before multi-repo support must still load."""
    raw = {
        "id": "eng-1",
        "project": "eng",
        "branch": "eng-1",
        "worktree_path": "/repo/.worktrees/eng-1",
        "base_branch": "main",
        "created_at": "2026-01-01T00:00:00+00:00",
    }
    task = Task.model_validate(raw)
    assert task.is_multi_repo is False
    assert task.all_repos()[0].branch == "eng-1"


def test_all_repos_primary_first_then_secondaries() -> None:
    secondary = TaskRepo(
        project="web",
        branch="eng-123-fix",
        worktree_path=Path("/ws/web"),
        base_branch="develop",
    )
    task = _task(
        worktree_path=Path("/ws/eng"),
        workspace_path=Path("/ws"),
        secondary_repos=[secondary],
    )
    assert task.is_multi_repo is True
    repos = task.all_repos()
    assert [r.project for r in repos] == ["eng", "web"]
    assert repos[0].worktree_path == Path("/ws/eng")
    assert repos[1].base_branch == "develop"


def test_primary_repo_mirrors_scalar_fields() -> None:
    task = _task(pr_url="https://example/pr/1")
    primary = task.primary_repo()
    assert primary.project == "eng"
    assert primary.branch == "eng-123-fix"
    assert primary.pr_url == "https://example/pr/1"
