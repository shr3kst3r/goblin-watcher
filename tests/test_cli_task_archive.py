"""`gw task archive` and the rematerialize-on-`gw run` path (gh-23).

Real git repos, as in tests/test_cli_task.py — the agent launcher and `gh` are
the only things patched.
"""

from __future__ import annotations

import subprocess
from pathlib import Path
from unittest.mock import patch

from typer.testing import CliRunner

from goblin_watcher import git, paths, state
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


def _bootstrap(tmp_path: Path) -> Path:
    """One project, one task on branch `spike/foo` with a materialized worktree."""
    repo = tmp_path / "alpha"
    _init_repo(repo)
    runner = CliRunner()
    runner.invoke(app, ["project", "new", "alpha", "--dir", str(repo)])
    runner.invoke(app, ["new", "--branch-name", "spike/foo", "--title", "Foo", "--no-launch"])
    return repo


def _task() -> Task:
    [task] = state.list_tasks(state.get_project("alpha"))
    return task


def _write_config(body: str) -> None:
    f = paths.config_file()
    f.parent.mkdir(parents=True, exist_ok=True)
    f.write_text(body)


def test_archive_drops_worktree_and_keeps_branch_and_record(
    isolated_xdg: Path, tmp_path: Path
) -> None:
    repo = _bootstrap(tmp_path)
    task = _task()
    worktree = task.worktree_path
    assert worktree.exists()

    res = CliRunner().invoke(app, ["task", "archive", task.id])
    assert res.exit_code == 0, res.output

    assert not worktree.exists()
    assert git.branch_exists(repo, task.branch)
    archived = _task()
    assert archived.archived is True
    assert archived.archived_at is not None
    # git no longer holds the path, so it can be re-added later.
    assert all(w.get("worktree") != str(worktree) for w in git.worktree_list(repo))


def test_archive_keeps_session_history(isolated_xdg: Path, tmp_path: Path) -> None:
    from datetime import UTC, datetime

    from goblin_watcher.models import SessionRecord

    _bootstrap(tmp_path)
    proj = state.get_project("alpha")
    task = _task()
    now = datetime.now(UTC)
    state.update_task(
        proj,
        task.id,
        lambda t: t.model_copy(
            update={
                "sessions": [
                    SessionRecord(
                        agent="claude", session_id="s-1", created_at=now, last_used_at=now
                    )
                ]
            }
        ),
    )

    res = CliRunner().invoke(app, ["task", "archive", task.id])
    assert res.exit_code == 0, res.output
    assert [s.session_id for s in _task().sessions] == ["s-1"]


def test_archive_refuses_dirty_worktree_without_force(isolated_xdg: Path, tmp_path: Path) -> None:
    _bootstrap(tmp_path)
    task = _task()
    (task.worktree_path / "README.md").write_text("local change")

    res = CliRunner().invoke(app, ["task", "archive", task.id])
    assert res.exit_code != 0
    assert isinstance(res.exception, GoblinError)
    assert "uncommitted" in res.exception.message.lower()
    assert task.worktree_path.exists()
    assert _task().archived is False


def test_archive_force_discards_a_dirty_worktree(isolated_xdg: Path, tmp_path: Path) -> None:
    _bootstrap(tmp_path)
    task = _task()
    (task.worktree_path / "untracked.txt").write_text("wip")

    res = CliRunner().invoke(app, ["task", "archive", task.id, "--force"])
    assert res.exit_code == 0, res.output
    assert not task.worktree_path.exists()
    assert _task().archived is True


def test_archive_refuses_a_scratch_task(isolated_xdg: Path) -> None:
    runner = CliRunner()
    assert runner.invoke(app, ["scratch", "spike", "--no-launch"]).exit_code == 0
    [task] = state.list_tasks(state.get_project("scratch"))

    res = runner.invoke(app, ["task", "archive", task.id])
    assert res.exit_code != 0
    assert isinstance(res.exception, GoblinError)
    assert "scratch" in res.exception.message.lower()
    assert task.worktree_path.exists()


def test_archive_is_idempotent(isolated_xdg: Path, tmp_path: Path) -> None:
    _bootstrap(tmp_path)
    task = _task()
    runner = CliRunner()
    assert runner.invoke(app, ["task", "archive", task.id]).exit_code == 0

    res = runner.invoke(app, ["task", "archive", task.id])
    assert res.exit_code == 0, res.output
    assert "already archived" in res.output
    assert _task().archived is True


def test_archive_marks_a_task_whose_worktree_vanished(isolated_xdg: Path, tmp_path: Path) -> None:
    """A worktree deleted behind gw's back still gets the record flagged, so
    `gw run` knows to rematerialize instead of launching into nothing."""
    import shutil

    repo = _bootstrap(tmp_path)
    task = _task()
    shutil.rmtree(task.worktree_path)

    res = CliRunner().invoke(app, ["task", "archive", task.id])
    assert res.exit_code == 0, res.output
    assert _task().archived is True
    assert git.branch_exists(repo, task.branch)


def test_task_show_reports_archived(isolated_xdg: Path, tmp_path: Path) -> None:
    _bootstrap(tmp_path)
    task = _task()
    runner = CliRunner()
    assert runner.invoke(app, ["task", "archive", task.id]).exit_code == 0

    res = runner.invoke(app, ["task", "show", task.id])
    assert res.exit_code == 0, res.output
    assert "archived" in res.output


def test_task_rm_still_works_on_an_archived_task(isolated_xdg: Path, tmp_path: Path) -> None:
    repo = _bootstrap(tmp_path)
    proj = state.get_project("alpha")
    task = _task()
    runner = CliRunner()
    assert runner.invoke(app, ["task", "archive", task.id]).exit_code == 0

    res = runner.invoke(app, ["task", "rm", task.id, "--force"])
    assert res.exit_code == 0, res.output
    assert not git.branch_exists(repo, task.branch)
    assert state.list_tasks(proj) == []


def test_archive_removes_every_worktree_of_a_multi_repo_task(
    isolated_xdg: Path, tmp_path: Path
) -> None:
    alpha = tmp_path / "alpha"
    beta = tmp_path / "beta"
    _init_repo(alpha)
    _init_repo(beta)
    runner = CliRunner()
    runner.invoke(app, ["project", "new", "alpha", "--dir", str(alpha)])
    runner.invoke(app, ["project", "new", "beta", "--dir", str(beta)])
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
    task = _task()
    paths_before = [r.worktree_path for r in task.all_repos()]
    assert task.workspace_path is not None
    assert all(p.exists() for p in paths_before)

    res = runner.invoke(app, ["task", "archive", task.id])
    assert res.exit_code == 0, res.output
    assert not any(p.exists() for p in paths_before)
    # Emptied by the removals, so the container goes too.
    assert not task.workspace_path.exists()
    assert git.branch_exists(alpha, task.branch)
    assert git.branch_exists(beta, task.secondary_repos[0].branch)


# --------------------------------------------------------------------------
# Rematerialize on `gw run`


def test_run_rematerializes_an_archived_task(isolated_xdg: Path, tmp_path: Path) -> None:
    _bootstrap(tmp_path)
    task = _task()
    # A commit on the branch, so we can prove the restored checkout is the
    # branch's content and not a fresh cut of `main`.
    (task.worktree_path / "feature.txt").write_text("work\n")
    subprocess.run(["git", "-C", str(task.worktree_path), "add", "-A"], check=True)
    subprocess.run(["git", "-C", str(task.worktree_path), "commit", "-qm", "feat"], check=True)

    runner = CliRunner()
    assert runner.invoke(app, ["task", "archive", task.id]).exit_code == 0
    assert not task.worktree_path.exists()

    with patch("goblin_watcher.commands.run.launch_agent", return_value=(0, task)) as launch:
        res = runner.invoke(app, ["run", task.id, "--new"])
    assert res.exit_code == 0, res.output

    assert task.worktree_path.exists()
    assert (task.worktree_path / "feature.txt").read_text() == "work\n"
    assert git.current_branch(task.worktree_path) == task.branch
    assert _task().archived is False
    assert _task().archived_at is None
    launch.assert_called_once()


def test_rematerialize_reapplies_setup(isolated_xdg: Path, tmp_path: Path) -> None:
    """A restored worktree is a bare checkout again, so the [setup] steps rerun."""
    repo = _bootstrap(tmp_path)
    task = _task()
    (repo / ".env").write_text("TOKEN=1")
    _write_config('[setup]\ncopy = [".env"]\nrun = ["touch bootstrapped"]\n')

    runner = CliRunner()
    assert runner.invoke(app, ["task", "archive", task.id]).exit_code == 0

    with patch("goblin_watcher.commands.run.launch_agent", return_value=(0, task)):
        res = runner.invoke(app, ["run", task.id, "--new"])
    assert res.exit_code == 0, res.output
    assert (task.worktree_path / ".env").read_text() == "TOKEN=1"
    assert (task.worktree_path / "bootstrapped").exists()


def test_run_errors_when_the_archived_branch_is_gone(isolated_xdg: Path, tmp_path: Path) -> None:
    """Rematerializing off the base branch would hand back an empty worktree
    wearing the task's name, so this raises instead."""
    repo = _bootstrap(tmp_path)
    task = _task()
    runner = CliRunner()
    assert runner.invoke(app, ["task", "archive", task.id]).exit_code == 0
    git.delete_branch(repo, task.branch, force=True)

    with patch("goblin_watcher.commands.run.launch_agent") as launch:
        res = runner.invoke(app, ["run", task.id, "--new"])
    assert res.exit_code != 0
    assert isinstance(res.exception, GoblinError)
    assert task.branch in res.exception.message
    assert res.exception.hint is not None
    assert "gw task rm" in res.exception.hint
    launch.assert_not_called()
    assert _task().archived is True


def test_run_on_a_live_task_does_not_touch_the_worktree(isolated_xdg: Path, tmp_path: Path) -> None:
    """The rematerialize hook is gated on `archived`; an ordinary run skips it."""
    _bootstrap(tmp_path)
    task = _task()
    (task.worktree_path / "untracked.txt").write_text("wip")

    with (
        patch("goblin_watcher.commands.run.launch_agent", return_value=(0, task)),
        patch("goblin_watcher.commands.run.rematerialize_task") as remat,
    ):
        res = CliRunner().invoke(app, ["run", task.id, "--new"])
    assert res.exit_code == 0, res.output
    remat.assert_not_called()
    assert (task.worktree_path / "untracked.txt").exists()


def test_archive_then_run_round_trips_a_multi_repo_task(isolated_xdg: Path, tmp_path: Path) -> None:
    alpha = tmp_path / "alpha"
    beta = tmp_path / "beta"
    _init_repo(alpha)
    _init_repo(beta)
    runner = CliRunner()
    runner.invoke(app, ["project", "new", "alpha", "--dir", str(alpha)])
    runner.invoke(app, ["project", "new", "beta", "--dir", str(beta)])
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
    task = _task()
    worktrees = [r.worktree_path for r in task.all_repos()]

    assert runner.invoke(app, ["task", "archive", task.id]).exit_code == 0
    with patch("goblin_watcher.commands.run.launch_agent", return_value=(0, task)):
        res = runner.invoke(app, ["run", task.id, "--new"])
    assert res.exit_code == 0, res.output
    assert all(p.exists() for p in worktrees)
    assert _task().archived is False
