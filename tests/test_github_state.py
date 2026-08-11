"""Tests for the TTL-cached GitHub issue-state refresh."""

import subprocess
from datetime import UTC, datetime, timedelta
from pathlib import Path
from unittest.mock import patch

from typer.testing import CliRunner

from goblin_watcher import github_state, state
from goblin_watcher.cli import app
from goblin_watcher.models import GhIssue, Task


def _init_repo(path: Path) -> None:
    path.mkdir(parents=True, exist_ok=True)
    subprocess.run(["git", "init", "-q", "-b", "main", str(path)], check=True)
    subprocess.run(["git", "-C", str(path), "config", "user.email", "t@t"], check=True)
    subprocess.run(["git", "-C", str(path), "config", "user.name", "tester"], check=True)
    (path / "README.md").write_text("hi")
    subprocess.run(["git", "-C", str(path), "add", "."], check=True)
    subprocess.run(["git", "-C", str(path), "commit", "-qm", "init"], check=True)


def _bootstrap(tmp_path: Path, *, updated_at: datetime | None, issue_state: str = "OPEN") -> Task:
    repo = tmp_path / "alpha"
    _init_repo(repo)
    runner = CliRunner()
    runner.invoke(app, ["project", "new", "alpha", "--dir", str(repo)])
    runner.invoke(app, ["new", "--branch-name", "gh-42-add-rate-limit", "--no-launch"])
    proj = state.get_project("alpha")
    [task] = state.list_tasks(proj)
    task = task.model_copy(
        update={
            "github_issue": GhIssue(
                number=42,
                repo="org/repo",
                title="Add rate limit",
                state=issue_state,
                url="https://github.com/org/repo/issues/42",
            ),
            "github_issue_state_updated_at": updated_at,
        }
    )
    state.save_task(proj, task)
    return task


def test_refresh_persists_new_state(isolated_xdg: Path, tmp_path: Path) -> None:
    task = _bootstrap(tmp_path, updated_at=None)
    proj = state.get_project("alpha")

    with patch("goblin_watcher.github_state.gh.issue_state", return_value="CLOSED") as lookup:
        out = github_state.refresh(proj, task)

    lookup.assert_called_once_with("org/repo", 42)
    assert out.github_issue is not None and out.github_issue.state == "CLOSED"
    assert out.github_issue_state_updated_at is not None
    # And it's on disk, not just in memory.
    reloaded = state.load_task(proj, task.id)
    assert reloaded.github_issue is not None and reloaded.github_issue.state == "CLOSED"


def test_refresh_skips_inside_the_ttl(isolated_xdg: Path, tmp_path: Path) -> None:
    task = _bootstrap(tmp_path, updated_at=datetime.now(UTC))
    proj = state.get_project("alpha")

    with patch("goblin_watcher.github_state.gh.issue_state") as lookup:
        out = github_state.refresh(proj, task)

    lookup.assert_not_called()
    assert out.github_issue is not None and out.github_issue.state == "OPEN"


def test_refresh_runs_once_the_ttl_expires(isolated_xdg: Path, tmp_path: Path) -> None:
    stale = datetime.now(UTC) - timedelta(seconds=github_state.DEFAULT_TTL_SECONDS + 1)
    task = _bootstrap(tmp_path, updated_at=stale)
    proj = state.get_project("alpha")

    with patch("goblin_watcher.github_state.gh.issue_state", return_value="CLOSED"):
        out = github_state.refresh(proj, task)

    assert out.github_issue is not None and out.github_issue.state == "CLOSED"


def test_refresh_keeps_cached_state_when_lookup_fails(isolated_xdg: Path, tmp_path: Path) -> None:
    """A missing `gh` or failed lookup leaves the timestamp alone so the next
    pass retries instead of treating the failure as fresh data."""
    task = _bootstrap(tmp_path, updated_at=None)
    proj = state.get_project("alpha")

    with patch("goblin_watcher.github_state.gh.issue_state", return_value=None):
        out = github_state.refresh(proj, task)

    assert out.github_issue is not None and out.github_issue.state == "OPEN"
    assert out.github_issue_state_updated_at is None


def test_refresh_is_a_noop_without_an_issue(isolated_xdg: Path, tmp_path: Path) -> None:
    task = _bootstrap(tmp_path, updated_at=None).model_copy(update={"github_issue": None})
    proj = state.get_project("alpha")

    with patch("goblin_watcher.github_state.gh.issue_state") as lookup:
        assert github_state.refresh(proj, task) is task

    lookup.assert_not_called()


def test_status_renders_github_issue_state(isolated_xdg: Path, tmp_path: Path) -> None:
    _bootstrap(tmp_path, updated_at=datetime.now(UTC), issue_state="OPEN")

    res = CliRunner().invoke(app, ["status", "--no-linear"])
    assert res.exit_code == 0, res.output
    assert "Add rate limit" in res.output
    assert "org/repo#42 open" in res.output


def test_status_no_linear_flag_skips_the_github_lookup(isolated_xdg: Path, tmp_path: Path) -> None:
    _bootstrap(tmp_path, updated_at=None, issue_state="OPEN")

    with patch("goblin_watcher.github_state.gh.issue_state") as lookup:
        res = CliRunner().invoke(app, ["status", "--no-linear"])
    assert res.exit_code == 0, res.output
    lookup.assert_not_called()
