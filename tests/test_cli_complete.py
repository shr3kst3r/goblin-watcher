"""Tests for the hidden `gw __complete` enumerator CLI.

The static zsh script depends on `gw __complete projects|tasks|sessions`
emitting one id per line on stdout, exit 0 even on empty state.
"""

from datetime import UTC, datetime
from pathlib import Path

from typer.testing import CliRunner

from goblin_watcher import state
from goblin_watcher.cli import app
from goblin_watcher.models import Project, SessionRecord, Task


def _make_project(root: Path, name: str = "alpha") -> Project:
    return Project(
        name=name,
        root=root,
        repo_url=None,
        default_branch="main",
        branch_prefix="",
        created_at=datetime.now(UTC),
    )


def _make_task(
    project_name: str,
    task_id: str,
    worktree: Path,
    sessions: list[SessionRecord] | None = None,
) -> Task:
    return Task(
        id=task_id,
        project=project_name,
        branch=task_id,
        worktree_path=worktree,
        base_branch="main",
        created_at=datetime.now(UTC),
        sessions=sessions or [],
    )


def _make_session(session_id: str, agent: str = "claude") -> SessionRecord:
    now = datetime.now(UTC)
    return SessionRecord(agent=agent, session_id=session_id, created_at=now, last_used_at=now)


def test_complete_projects_lists_registered(isolated_xdg: Path, tmp_path: Path) -> None:
    for name in ("alpha", "beta"):
        root = tmp_path / name
        root.mkdir()
        state.register_project(_make_project(root, name))
    res = CliRunner().invoke(app, ["__complete", "projects"])
    assert res.exit_code == 0, res.output
    assert res.stdout.splitlines() == ["alpha", "beta"]


def test_complete_projects_empty_state_exits_zero(isolated_xdg: Path) -> None:
    res = CliRunner().invoke(app, ["__complete", "projects"])
    assert res.exit_code == 0
    assert res.stdout == ""


def test_complete_tasks_lists_ids(isolated_xdg: Path, tmp_path: Path) -> None:
    root = tmp_path / "alpha"
    root.mkdir()
    state.register_project(_make_project(root, "alpha"))
    proj = state.get_project("alpha")
    state.save_task(proj, _make_task("alpha", "eng-1", tmp_path / "wt1"))
    state.save_task(proj, _make_task("alpha", "eng-2", tmp_path / "wt2"))
    res = CliRunner().invoke(app, ["__complete", "tasks"])
    assert res.exit_code == 0, res.output
    assert res.stdout.splitlines() == ["eng-1", "eng-2"]


def test_complete_tasks_project_filter(isolated_xdg: Path, tmp_path: Path) -> None:
    for name in ("a", "b"):
        root = tmp_path / name
        root.mkdir()
        state.register_project(_make_project(root, name))
    state.save_task(state.get_project("a"), _make_task("a", "eng-1", tmp_path / "wt1"))
    state.save_task(state.get_project("b"), _make_task("b", "eng-2", tmp_path / "wt2"))
    res = CliRunner().invoke(app, ["__complete", "tasks", "--project", "a"])
    assert res.exit_code == 0, res.output
    assert res.stdout.splitlines() == ["eng-1"]


def test_complete_sessions_lists_ids(isolated_xdg: Path, tmp_path: Path) -> None:
    root = tmp_path / "alpha"
    root.mkdir()
    state.register_project(_make_project(root, "alpha"))
    proj = state.get_project("alpha")
    state.save_task(
        proj,
        _make_task("alpha", "t1", tmp_path / "wt", sessions=[_make_session("s-aaa")]),
    )
    res = CliRunner().invoke(app, ["__complete", "sessions"])
    assert res.exit_code == 0, res.output
    assert res.stdout.splitlines() == ["s-aaa"]


def test_complete_sessions_task_filter(isolated_xdg: Path, tmp_path: Path) -> None:
    root = tmp_path / "alpha"
    root.mkdir()
    state.register_project(_make_project(root, "alpha"))
    proj = state.get_project("alpha")
    state.save_task(
        proj,
        _make_task("alpha", "t1", tmp_path / "wt1", sessions=[_make_session("s-aaa")]),
    )
    state.save_task(
        proj,
        _make_task("alpha", "t2", tmp_path / "wt2", sessions=[_make_session("s-bbb")]),
    )
    res = CliRunner().invoke(app, ["__complete", "sessions", "--task", "t2"])
    assert res.exit_code == 0, res.output
    assert res.stdout.splitlines() == ["s-bbb"]
