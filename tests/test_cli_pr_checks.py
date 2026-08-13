"""`gw pr checks` — per-check detail behind the status badge (gh-18).

Never calls the real `gh`: both the PR lookup and the rollup are patched.
"""

from __future__ import annotations

import subprocess
from pathlib import Path
from unittest.mock import patch

from typer.testing import CliRunner

from goblin_watcher import gh, state
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


_PR = {
    "url": "https://github.com/o/r/pull/7",
    "state": "OPEN",
    "number": "7",
    "title": "Do it",
}


def _invoke(runs: list[gh.CheckRun] | None, args: list[str]):  # type: ignore[no-untyped-def]
    with (
        patch("goblin_watcher.commands.pr.gh.pr_status", return_value=_PR),
        patch("goblin_watcher.commands.pr.gh.pr_check_runs", return_value=runs) as check_runs,
    ):
        res = CliRunner().invoke(app, args)
    return res, check_runs


def test_checks_lists_every_check_with_state_and_url(isolated_xdg: Path, tmp_path: Path) -> None:
    _bootstrap(tmp_path)
    [task] = state.list_tasks(state.get_project("alpha"))
    runs = [
        gh.CheckRun(name="lint", state="passing", detail="SUCCESS", url="https://ci/1"),
        gh.CheckRun(
            name="test", state="failing", detail="FAILURE", url="https://ci/2", workflow="verify"
        ),
    ]
    res, check_runs = _invoke(runs, ["pr", "checks", task.id])
    assert res.exit_code == 0, res.output
    check_runs.assert_called_once_with(_PR["url"])
    assert "PR #7" in res.output
    assert "verify / test" in res.output
    assert "FAILURE" in res.output
    assert "https://ci/2" in res.output
    assert "lint" in res.output
    assert "https://ci/1" in res.output


def test_failing_checks_are_listed_first(isolated_xdg: Path, tmp_path: Path) -> None:
    """The check you came here to find shouldn't be buried under the green ones."""
    _bootstrap(tmp_path)
    [task] = state.list_tasks(state.get_project("alpha"))
    runs = [
        gh.CheckRun(name="lint", state="passing", detail="SUCCESS"),
        gh.CheckRun(name="docs", state="pending", detail="IN_PROGRESS"),
        gh.CheckRun(name="test", state="failing", detail="FAILURE"),
    ]
    res, _ = _invoke(runs, ["pr", "checks", task.id])
    assert res.exit_code == 0, res.output
    order = [res.output.index(name) for name in ("test", "docs", "lint")]
    assert order == sorted(order)


def test_checks_resolves_task_from_cwd(isolated_xdg: Path, tmp_path: Path, monkeypatch) -> None:
    _bootstrap(tmp_path)
    [task] = state.list_tasks(state.get_project("alpha"))
    monkeypatch.chdir(task.worktree_path)
    runs = [gh.CheckRun(name="lint", state="passing", detail="SUCCESS")]
    res, _ = _invoke(runs, ["pr", "checks"])
    assert res.exit_code == 0, res.output
    assert task.id in res.output


def test_no_checks_configured_says_so(isolated_xdg: Path, tmp_path: Path) -> None:
    """A repo without CI must not render as an empty (implicitly green) list."""
    _bootstrap(tmp_path)
    [task] = state.list_tasks(state.get_project("alpha"))
    res, _ = _invoke(None, ["pr", "checks", task.id])
    assert res.exit_code == 0, res.output
    assert "No checks reported" in res.output


def test_no_pr_exits_nonzero(isolated_xdg: Path, tmp_path: Path) -> None:
    from goblin_watcher.errors import GoblinError

    _bootstrap(tmp_path)
    [task] = state.list_tasks(state.get_project("alpha"))
    with (
        patch(
            "goblin_watcher.commands.pr.gh.pr_status",
            side_effect=GoblinError("No PR found for the current branch."),
        ),
        patch("goblin_watcher.commands.pr.gh.pr_check_runs") as check_runs,
    ):
        res = CliRunner().invoke(app, ["pr", "checks", task.id])
    assert res.exit_code == 1
    assert "No PR found" in res.output
    check_runs.assert_not_called()


def test_checks_rejects_a_scratch_task(isolated_xdg: Path, tmp_path: Path) -> None:
    from goblin_watcher.errors import GoblinError

    runner = CliRunner()
    runner.invoke(app, ["scratch", "pad", "--no-launch"])
    res = runner.invoke(app, ["pr", "checks", "pad"])
    assert res.exit_code != 0
    assert isinstance(res.exception, GoblinError)
    assert "scratch space" in res.exception.message
