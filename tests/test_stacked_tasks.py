"""Stacked branches: recording `Task.parent_task` and surfacing the stack (gh-20).

Covers the four places the link matters — resolution at `gw new` time, the
`gw status` tree, `gw task show`, and the PR body.
"""

from __future__ import annotations

import subprocess
from datetime import UTC, datetime
from pathlib import Path
from unittest.mock import patch

from typer.testing import CliRunner

from goblin_watcher import state
from goblin_watcher.cli import app
from goblin_watcher.commands.pr import _pr_body
from goblin_watcher.commands.status import _stack_order, _stack_suffix
from goblin_watcher.models import Task, TaskRepo


def _init_repo(path: Path) -> None:
    path.mkdir(parents=True, exist_ok=True)
    subprocess.run(["git", "init", "-q", "-b", "main", str(path)], check=True)
    subprocess.run(["git", "-C", str(path), "config", "user.email", "t@t"], check=True)
    subprocess.run(["git", "-C", str(path), "config", "user.name", "tester"], check=True)
    (path / "README.md").write_text("hi")
    subprocess.run(["git", "-C", str(path), "add", "."], check=True)
    subprocess.run(["git", "-C", str(path), "commit", "-qm", "init"], check=True)


def _project(tmp_path: Path) -> Path:
    repo = tmp_path / "alpha"
    _init_repo(repo)
    runner = CliRunner()
    res = runner.invoke(app, ["project", "new", "alpha", "--dir", str(repo)])
    assert res.exit_code == 0, res.output
    return repo


def _new(*args: str) -> str:
    res = CliRunner().invoke(app, ["new", *args, "--no-launch"])
    assert res.exit_code == 0, res.output
    return res.output


def _task(task_id: str) -> Task:
    return state.load_task(state.get_project("alpha"), task_id)


# ---------------------------------------------------------------------------
# Resolution at creation time


def test_from_a_tracked_branch_records_the_parent(isolated_xdg: Path, tmp_path: Path) -> None:
    _project(tmp_path)
    _new("--branch-name", "feat/base")
    parent = _task("feat-base")

    output = _new("--branch-name", "feat/child", "--from", parent.branch)

    child = _task("feat-child")
    assert child.base_branch == "feat/base"
    assert child.parent_task == "feat-base"
    assert "stacked on" in output


def test_from_the_default_branch_records_no_parent(isolated_xdg: Path, tmp_path: Path) -> None:
    """A task sitting on `main` is the ordinary case, not a stack — even when
    another task's branch happens to be the default branch."""
    _project(tmp_path)
    _new("--branch-name", "feat/child", "--from", "main")

    assert _task("feat-child").parent_task is None


def test_from_an_untracked_branch_records_no_parent(isolated_xdg: Path, tmp_path: Path) -> None:
    repo = _project(tmp_path)
    subprocess.run(["git", "-C", str(repo), "branch", "someone-elses-work"], check=True)

    _new("--branch-name", "feat/child", "--from", "someone-elses-work")

    child = _task("feat-child")
    assert child.base_branch == "someone-elses-work"
    assert child.parent_task is None


def test_issue_source_records_the_parent(isolated_xdg: Path, tmp_path: Path) -> None:
    from goblin_watcher.gh import IssueInfo

    _project(tmp_path)
    _new("--branch-name", "feat/base")

    info = IssueInfo(
        number=7,
        repo="org/repo",
        title="Stack on the base",
        body="",
        state="OPEN",
        url="https://github.com/org/repo/issues/7",
        labels=(),
        assignees=(),
    )
    with patch("goblin_watcher.commands.new.gh.issue_view", return_value=info):
        _new("--issue", "7", "--project", "alpha", "--from", "feat/base")

    assert _task("gh-7").parent_task == "feat-base"


def test_pr_targeting_a_tracked_branch_records_the_parent(
    isolated_xdg: Path, tmp_path: Path
) -> None:
    from goblin_watcher.gh import PrInfo

    repo = _project(tmp_path)
    _new("--branch-name", "feat/base")
    # The PR's head branch has to exist for the worktree add to succeed.
    subprocess.run(["git", "-C", str(repo), "branch", "feat/pr-9", "feat/base"], check=True)

    info = PrInfo(
        number=9,
        head_ref="feat/pr-9",
        base_ref="feat/base",
        url="https://github.com/org/repo/pull/9",
        title="Stacked PR",
        state="OPEN",
        is_cross_repository=False,
    )
    with patch("goblin_watcher.commands.new.gh.pr_view", return_value=info):
        _new("--pr", "9", "--project", "alpha")

    assert _task("feat-pr-9").parent_task == "feat-base"


# ---------------------------------------------------------------------------
# `gw status` tree


def _indent_of(output: str, needle: str) -> int:
    """Column the tree renders `needle` at — the nesting depth, as text."""
    for line in output.splitlines():
        if needle in line:
            return line.index(needle)
    raise AssertionError(f"{needle!r} not in status output:\n{output}")


def test_status_nests_a_child_under_its_parent(isolated_xdg: Path, tmp_path: Path) -> None:
    _project(tmp_path)
    _new("--branch-name", "base")
    _new("--branch-name", "mid", "--from", "base")
    _new("--branch-name", "tip", "--from", "mid")

    res = CliRunner().invoke(app, ["status"])
    assert res.exit_code == 0, res.output

    assert _indent_of(res.output, "base") < _indent_of(res.output, "mid")
    assert _indent_of(res.output, "mid") < _indent_of(res.output, "tip")


def test_status_flags_a_restack_once_the_parent_merged(isolated_xdg: Path, tmp_path: Path) -> None:
    _project(tmp_path)
    _new("--branch-name", "base")
    _new("--branch-name", "child", "--from", "base")
    proj = state.get_project("alpha")
    state.save_task(proj, _task("base").model_copy(update={"status": "merged"}))

    res = CliRunner().invoke(app, ["status"])
    assert res.exit_code == 0, res.output
    assert "restack: base merged" in res.output


def test_status_notes_a_parent_that_is_no_longer_tracked(
    isolated_xdg: Path, tmp_path: Path
) -> None:
    _project(tmp_path)
    _new("--branch-name", "base")
    _new("--branch-name", "child", "--from", "base")
    state.delete_task_record(state.get_project("alpha"), "base")

    res = CliRunner().invoke(app, ["status"])
    assert res.exit_code == 0, res.output
    assert "no longer tracked" in res.output


def _bare_task(task_id: str, parent: str | None, status: str = "open") -> Task:
    return Task(
        id=task_id,
        project="alpha",
        branch=task_id,
        worktree_path=Path("/nope") / task_id,
        base_branch="main",
        parent_task=parent,
        status=status,  # type: ignore[arg-type]
        created_at=datetime.now(UTC),
    )


def test_stack_order_puts_parents_first_regardless_of_input_order() -> None:
    tasks = [
        _bare_task("tip", "mid"),
        _bare_task("base", None),
        _bare_task("mid", "base"),
    ]
    assert [t.id for t in _stack_order(tasks)] == ["base", "mid", "tip"]


def test_stack_order_survives_a_parent_cycle() -> None:
    """Only reachable via hand-edited records, but it must not spin forever."""
    tasks = [_bare_task("a", "b"), _bare_task("b", "a"), _bare_task("c", None)]
    ordered = _stack_order(tasks)
    assert sorted(t.id for t in ordered) == ["a", "b", "c"]


def test_stack_suffix_is_quiet_for_a_live_parent() -> None:
    parent = _bare_task("base", None)
    child = _bare_task("child", "base")
    assert _stack_suffix(child, {"base": parent, "child": child}) == ""
    assert _stack_suffix(parent, {"base": parent}) == ""


# ---------------------------------------------------------------------------
# `gw task show`


def test_task_show_reports_the_parent(isolated_xdg: Path, tmp_path: Path) -> None:
    _project(tmp_path)
    _new("--branch-name", "base")
    _new("--branch-name", "child", "--from", "base")

    res = CliRunner().invoke(app, ["task", "show", "child"])
    assert res.exit_code == 0, res.output
    assert "stacked on" in res.output
    assert "base" in res.output


def test_task_show_omits_the_parent_line_when_unstacked(isolated_xdg: Path, tmp_path: Path) -> None:
    _project(tmp_path)
    _new("--branch-name", "solo")

    res = CliRunner().invoke(app, ["task", "show", "solo"])
    assert res.exit_code == 0, res.output
    assert "stacked on" not in res.output


def test_rename_repoints_stacked_children(isolated_xdg: Path, tmp_path: Path) -> None:
    """A rename is record-only, but `parent_task` stores an id — the children
    have to follow, or they'd look orphaned by a no-op."""
    _project(tmp_path)
    _new("--branch-name", "base")
    _new("--branch-name", "child", "--from", "base")

    res = CliRunner().invoke(app, ["task", "rename", "base", "renamed-base"])
    assert res.exit_code == 0, res.output

    assert _task("child").parent_task == "renamed-base"
    assert "Repointed 1 stacked task(s)" in res.output


# ---------------------------------------------------------------------------
# PR body


def _repo_with_branch(repo: Path, base: str, head: str) -> None:
    _init_repo(repo)
    subprocess.run(["git", "-C", str(repo), "checkout", "-q", "-b", base], check=True)
    (repo / "base.py").write_text("base\n")
    subprocess.run(["git", "-C", str(repo), "add", "."], check=True)
    subprocess.run(["git", "-C", str(repo), "commit", "-qm", "Base work"], check=True)
    subprocess.run(["git", "-C", str(repo), "checkout", "-q", "-b", head], check=True)
    (repo / "head.py").write_text("head\n")
    subprocess.run(["git", "-C", str(repo), "add", "."], check=True)
    subprocess.run(["git", "-C", str(repo), "commit", "-qm", "Child work"], check=True)


def _stacked_pair(repo: Path) -> tuple[Task, Task]:
    parent = Task(
        id="eng-1",
        project="alpha",
        branch="feat/base",
        worktree_path=repo,
        base_branch="main",
        pr_url="https://github.com/org/repo/pull/1",
        created_at=datetime.now(UTC),
    )
    child = Task(
        id="eng-2",
        project="alpha",
        branch="feat/child",
        worktree_path=repo,
        base_branch="feat/base",
        parent_task="eng-1",
        created_at=datetime.now(UTC),
    )
    return parent, child


def test_pr_body_notes_the_branch_it_is_stacked_on(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    _repo_with_branch(repo, "feat/base", "feat/child")
    parent, child = _stacked_pair(repo)

    body = _pr_body(child, child.primary_repo(), repo, [], parent=parent)

    assert "## Stacked on `feat/base`" in body
    assert "task `eng-1`" in body
    assert "https://github.com/org/repo/pull/1" in body
    assert "targets `feat/base`" in body


def test_pr_body_omits_the_parent_pr_link_when_there_is_none(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    _repo_with_branch(repo, "feat/base", "feat/child")
    parent, child = _stacked_pair(repo)
    parent = parent.model_copy(update={"pr_url": None})

    body = _pr_body(child, child.primary_repo(), repo, [], parent=parent)

    assert "## Stacked on `feat/base`" in body
    assert "github.com" not in body


def test_pr_body_skips_the_stack_note_for_a_secondary_repo(tmp_path: Path) -> None:
    """`parent_task` describes the primary branch; a secondary repo sits on its
    own project's default branch, so claiming it is stacked would be wrong."""
    repo = tmp_path / "repo"
    _repo_with_branch(repo, "feat/base", "feat/child")
    parent, child = _stacked_pair(repo)
    secondary = TaskRepo(
        project="web",
        branch="feat/child-web",
        worktree_path=repo,
        base_branch="main",
    )

    body = _pr_body(child, secondary, repo, [], parent=parent)

    assert "Stacked on" not in body


def test_pr_body_has_no_stack_section_without_a_parent(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    _repo_with_branch(repo, "feat/base", "feat/child")
    _parent, child = _stacked_pair(repo)

    body = _pr_body(child, child.primary_repo(), repo, [])

    assert "Stacked on" not in body
