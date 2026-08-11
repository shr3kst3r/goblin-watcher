"""Tests for the PR-body assembly used by `gw pr open`."""

import subprocess
from datetime import UTC, datetime
from pathlib import Path

from goblin_watcher.commands.pr import _pr_body
from goblin_watcher.models import GhIssue, LinearComment, LinearIssue, Task, TaskRepo


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

    body = _pr_body(task, task.primary_repo(), repo, [])

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

    body = _pr_body(task, task.primary_repo(), repo, [])

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
    body = _pr_body(task, task.primary_repo(), repo, [])

    # No commits → no "What changed" section.
    assert "## What changed" not in body
    # Footer still there.
    assert "Branch `feat/empty` off `main`" in body


def test_pr_body_cross_references_sibling_repos(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    _init_repo_with_commits(repo, "feat/foo")
    task = _make_task(repo, "feat/foo", linear=None)
    sibling = TaskRepo(
        project="web",
        branch="feat/foo-web",
        worktree_path=tmp_path / "web",
        base_branch="develop",
    )
    body = _pr_body(task, task.primary_repo(), repo, [sibling])

    assert "Part of a multi-repo change" in body
    assert "`web`" in body
    assert "`feat/foo-web`" in body


def _github_task(repo: Path, branch: str, issue: GhIssue) -> Task:
    return Task(
        id=f"gh-{issue.number}",
        project="alpha",
        github_issue=issue,
        branch=branch,
        worktree_path=repo,
        base_branch="main",
        created_at=datetime.now(UTC),
    )


def _gh_issue(number: int = 42, repo: str = "org/repo") -> GhIssue:
    return GhIssue(
        number=number,
        repo=repo,
        title="Add rate limit",
        body="We need a token bucket.",
        state="OPEN",
        url=f"https://github.com/{repo}/issues/{number}",
    )


def test_pr_body_closes_same_repo_issue_with_bare_number(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    _init_repo_with_commits(repo, "gh-42-add-rate-limit")
    task = _github_task(repo, "gh-42-add-rate-limit", _gh_issue())

    body = _pr_body(
        task,
        task.primary_repo(),
        repo,
        [],
        project_repo_url="git@github.com:org/repo.git",
    )

    assert "Closes #42 — Add rate limit" in body
    assert "org/repo#42" not in body
    # The issue body isn't duplicated — GitHub renders the link inline.
    assert "token bucket" not in body
    assert "## What changed" in body


def test_pr_body_closes_cross_repo_issue_with_qualified_reference(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    _init_repo_with_commits(repo, "gh-3-ship-it")
    task = _github_task(repo, "gh-3-ship-it", _gh_issue(number=3, repo="org/tracker"))

    body = _pr_body(
        task,
        task.primary_repo(),
        repo,
        [],
        project_repo_url="https://github.com/org/repo",
    )

    assert "Closes org/tracker#3 — Add rate limit" in body
    assert "Closes #3" not in body


def test_pr_body_unknown_pr_repo_falls_back_to_qualified_reference(tmp_path: Path) -> None:
    """With no GitHub remote to compare against we can't claim same-repo, so we
    emit the form that is correct either way."""
    repo = tmp_path / "repo"
    _init_repo_with_commits(repo, "gh-42-add-rate-limit")
    task = _github_task(repo, "gh-42-add-rate-limit", _gh_issue())

    body = _pr_body(task, task.primary_repo(), repo, [], project_repo_url=None)

    assert "Closes org/repo#42" in body
