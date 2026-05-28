"""Tests for the PR-body assembly used by `gw pr open`."""

import subprocess
from datetime import UTC, datetime
from pathlib import Path

from goblin_watcher.commands.pr import _pr_body
from goblin_watcher.models import LinearComment, LinearIssue, Task


def _init_repo_with_commits(repo: Path, branch: str) -> None:
    repo.mkdir(parents=True, exist_ok=True)
    subprocess.run(["git", "init", "-q", "-b", "main", str(repo)], check=True)
    subprocess.run(["git", "-C", str(repo), "config", "user.email", "t@t"], check=True)
    subprocess.run(["git", "-C", str(repo), "config", "user.name", "tester"], check=True)
    (repo / "README.md").write_text("hi")
    subprocess.run(["git", "-C", str(repo), "add", "."], check=True)
    subprocess.run(["git", "-C", str(repo), "commit", "-qm", "init"], check=True)
    subprocess.run(["git", "-C", str(repo), "checkout", "-q", "-b", branch], check=True)
    (repo / "foo.py").write_text("def foo():\n    return 1\n")
    subprocess.run(["git", "-C", str(repo), "add", "."], check=True)
    subprocess.run(
        ["git", "-C", str(repo), "commit", "-qm", "Add foo()\n\nImplements the foo function."],
        check=True,
    )
    (repo / "bar.py").write_text("def bar():\n    return 2\n")
    subprocess.run(["git", "-C", str(repo), "add", "."], check=True)
    subprocess.run(["git", "-C", str(repo), "commit", "-qm", "Add bar()"], check=True)


def _make_task(repo: Path, branch: str, linear: LinearIssue | None = None) -> Task:
    return Task(
        id="eng-1",
        project="alpha",
        linear=linear,
        branch=branch,
        worktree_path=repo,
        base_branch="main",
        created_at=datetime.now(UTC),
    )


def test_pr_body_includes_issue_and_commits(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    _init_repo_with_commits(repo, "feat/foo")
    issue = LinearIssue(
        id="uuid",
        identifier="ENG-1",
        title="Add foo and bar",
        description="We need foo and bar so things work.",
        state="In Progress",
        team_key="ENG",
        url="https://linear.app/x/issue/ENG-1",
        comments=[
            LinearComment(
                body="Make sure it handles the None case.",
                created_at=datetime(2025, 1, 10, 12, 0, tzinfo=UTC),
                author="Alice",
            )
        ],
    )
    task = _make_task(repo, "feat/foo", linear=issue)

    body = _pr_body(task, repo_root=repo)

    # Issue context.
    assert "Resolves [ENG-1]" in body
    assert "Add foo and bar" in body
    assert "We need foo and bar" in body
    assert "Discussion from Linear" in body
    assert "Alice" in body
    assert "None case" in body

    # Commits — both made on the branch.
    assert "## What changed" in body
    assert "**Add foo()**" in body
    assert "**Add bar()**" in body
    # Commit body is included indented under the subject.
    assert "Implements the foo function." in body

    # Diffstat is rendered as a fenced block.
    assert "### Files" in body
    assert "foo.py" in body and "bar.py" in body

    # Footer.
    assert "Branch `feat/foo` off `main`" in body


def test_pr_body_without_linear_falls_back_to_commits(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    _init_repo_with_commits(repo, "feat/foo")
    task = _make_task(repo, "feat/foo", linear=None)

    body = _pr_body(task, repo_root=repo)

    assert "Resolves" not in body  # no Linear → no resolves line
    assert "## Issue" not in body  # no Linear → no issue section
    assert "## What changed" in body
    assert "**Add foo()**" in body


def test_pr_body_empty_branch_still_emits_footer(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init", "-q", "-b", "main", str(repo)], check=True)
    subprocess.run(["git", "-C", str(repo), "config", "user.email", "t@t"], check=True)
    subprocess.run(["git", "-C", str(repo), "config", "user.name", "tester"], check=True)
    (repo / "README.md").write_text("hi")
    subprocess.run(["git", "-C", str(repo), "add", "."], check=True)
    subprocess.run(["git", "-C", str(repo), "commit", "-qm", "init"], check=True)
    subprocess.run(["git", "-C", str(repo), "checkout", "-q", "-b", "feat/empty"], check=True)

    task = _make_task(repo, "feat/empty", linear=None)
    body = _pr_body(task, repo_root=repo)

    # No commits → no "What changed" section.
    assert "## What changed" not in body
    # Footer still there.
    assert "Branch `feat/empty` off `main`" in body
