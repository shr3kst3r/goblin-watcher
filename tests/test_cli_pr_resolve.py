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


def test_pr_open_project_scopes_lookup_to_one_project(isolated_xdg: Path, tmp_path: Path) -> None:
    """--project disambiguates a task id shared across projects."""
    repo_a = tmp_path / "alpha"
    repo_b = tmp_path / "beta"
    _init_repo(repo_a)
    _init_repo(repo_b)
    runner = CliRunner()
    runner.invoke(app, ["project", "new", "alpha", "--dir", str(repo_a)])
    runner.invoke(app, ["project", "new", "beta", "--dir", str(repo_b)])
    runner.invoke(app, ["new", "--project", "alpha", "--branch-name", "spike/foo", "--no-launch"])
    runner.invoke(app, ["new", "--project", "beta", "--branch-name", "spike/foo", "--no-launch"])
    proj_b = state.get_project("beta")
    [task_b] = state.list_tasks(proj_b)

    with (
        patch("goblin_watcher.commands.pr.git.push") as push,
        patch(
            "goblin_watcher.commands.pr.gh.create_pr",
            return_value="https://github.com/x/y/pull/1",
        ),
    ):
        res = runner.invoke(app, ["pr", "open", task_b.id, "--project", "beta"])
    assert res.exit_code == 0, res.output
    # Pushed from beta's worktree, not alpha's identically-named one.
    push.assert_called_once()
    assert push.call_args.args[0] == task_b.worktree_path

    [persisted] = state.list_tasks(proj_b)
    assert persisted.pr_url == "https://github.com/x/y/pull/1"
    # Alpha's task is untouched.
    [alpha_task] = state.list_tasks(state.get_project("alpha"))
    assert alpha_task.pr_url is None


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


def test_pr_open_skips_create_when_pr_already_open(
    isolated_xdg: Path, tmp_path: Path, monkeypatch
) -> None:
    """Re-running `gw pr open` on a task with an open PR pushes the branch but
    doesn't call `gh pr create` again (which would fail)."""
    _bootstrap(tmp_path)
    proj = state.get_project("alpha")
    [task] = state.list_tasks(proj)

    monkeypatch.chdir(task.worktree_path)
    runner = CliRunner()
    with (
        patch("goblin_watcher.commands.pr.git.push") as push,
        patch(
            "goblin_watcher.commands.pr.gh.pr_for_branch",
            return_value={"url": "https://github.com/x/y/pull/9", "state": "OPEN", "number": "9"},
        ),
        patch("goblin_watcher.commands.pr.gh.create_pr") as create,
    ):
        res = runner.invoke(app, ["pr", "open"])
    assert res.exit_code == 0, res.output
    push.assert_called_once()
    create.assert_not_called()
    assert "already open" in res.output

    [persisted] = state.list_tasks(proj)
    assert persisted.pr_url == "https://github.com/x/y/pull/9"
    assert persisted.status == "pr-open"


def test_pr_open_closed_pr_does_not_block_fresh_create(
    isolated_xdg: Path, tmp_path: Path, monkeypatch
) -> None:
    _bootstrap(tmp_path)
    proj = state.get_project("alpha")
    [task] = state.list_tasks(proj)

    monkeypatch.chdir(task.worktree_path)
    runner = CliRunner()
    with (
        patch("goblin_watcher.commands.pr.git.push"),
        patch(
            "goblin_watcher.commands.pr.gh.pr_for_branch",
            return_value={"url": "https://github.com/x/y/pull/3", "state": "CLOSED", "number": "3"},
        ),
        patch(
            "goblin_watcher.commands.pr.gh.create_pr",
            return_value="https://github.com/x/y/pull/10",
        ) as create,
    ):
        res = runner.invoke(app, ["pr", "open"])
    assert res.exit_code == 0, res.output
    create.assert_called_once()
    [persisted] = state.list_tasks(proj)
    assert persisted.pr_url == "https://github.com/x/y/pull/10"


def test_pr_open_notify_linear_posts_comment(
    isolated_xdg: Path, tmp_path: Path, monkeypatch
) -> None:
    from goblin_watcher.models import LinearIssue

    _bootstrap(tmp_path)
    proj = state.get_project("alpha")
    [task] = state.list_tasks(proj)
    task = task.model_copy(
        update={
            "linear": LinearIssue(
                id="issue-uuid",
                identifier="ENG-9",
                title="Do it",
                state="In Progress",
                team_key="ENG",
                url="https://linear.app/x/issue/ENG-9",
            )
        }
    )
    state.save_task(proj, task)

    monkeypatch.chdir(task.worktree_path)
    runner = CliRunner()
    with (
        patch("goblin_watcher.commands.pr.git.push"),
        patch("goblin_watcher.commands.pr.gh.pr_for_branch", return_value=None),
        patch(
            "goblin_watcher.commands.pr.gh.create_pr",
            return_value="https://github.com/x/y/pull/12",
        ),
        patch("goblin_watcher.commands.pr.get_linear_api_key", return_value="fake-key"),
        patch("goblin_watcher.linear.client.LinearClient.create_comment") as comment,
    ):
        res = runner.invoke(app, ["pr", "open", "--notify-linear"])
    assert res.exit_code == 0, res.output
    comment.assert_called_once()
    issue_id, body = comment.call_args.args
    assert issue_id == "issue-uuid"
    assert "https://github.com/x/y/pull/12" in body
    assert "Posted PR link to Linear issue ENG-9" in res.output


def test_pr_open_notify_linear_failure_is_nonfatal(
    isolated_xdg: Path, tmp_path: Path, monkeypatch
) -> None:
    from goblin_watcher.errors import GoblinError
    from goblin_watcher.models import LinearIssue

    _bootstrap(tmp_path)
    proj = state.get_project("alpha")
    [task] = state.list_tasks(proj)
    task = task.model_copy(
        update={
            "linear": LinearIssue(
                id="issue-uuid",
                identifier="ENG-9",
                title="Do it",
                state="In Progress",
                team_key="ENG",
                url="https://linear.app/x/issue/ENG-9",
            )
        }
    )
    state.save_task(proj, task)

    monkeypatch.chdir(task.worktree_path)
    runner = CliRunner()
    with (
        patch("goblin_watcher.commands.pr.git.push"),
        patch("goblin_watcher.commands.pr.gh.pr_for_branch", return_value=None),
        patch(
            "goblin_watcher.commands.pr.gh.create_pr",
            return_value="https://github.com/x/y/pull/12",
        ),
        patch(
            "goblin_watcher.commands.pr.get_linear_api_key",
            side_effect=GoblinError("no key"),
        ),
    ):
        res = runner.invoke(app, ["pr", "open", "--notify-linear"])
    assert res.exit_code == 0, res.output
    assert "Skipped Linear notification" in res.output
    # The PR itself still landed on the task.
    [persisted] = state.list_tasks(proj)
    assert persisted.pr_url == "https://github.com/x/y/pull/12"
