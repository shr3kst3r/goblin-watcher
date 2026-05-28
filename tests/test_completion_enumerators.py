"""Tests for the enumeration helpers backing tab completion.

The same helpers drive `gw __complete` (called from the static zsh script)
and the Typer `autocompletion=` callbacks (bash/fish dynamic completion).
"""

from datetime import UTC, datetime
from pathlib import Path

from goblin_watcher import state
from goblin_watcher.completion_enumerators import (
    complete_projects,
    complete_sessions,
    complete_tasks,
    enumerate_projects,
    enumerate_sessions,
    enumerate_tasks,
)
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


def test_enumerate_projects_empty(isolated_xdg: Path) -> None:
    assert enumerate_projects() == []


def test_enumerate_projects_sorted(isolated_xdg: Path, tmp_path: Path) -> None:
    for name in ("zeta", "alpha", "mu"):
        root = tmp_path / name
        root.mkdir()
        state.register_project(_make_project(root, name))
    assert enumerate_projects() == ["alpha", "mu", "zeta"]


def test_enumerate_tasks_across_projects(isolated_xdg: Path, tmp_path: Path) -> None:
    for name in ("a", "b"):
        root = tmp_path / name
        root.mkdir()
        state.register_project(_make_project(root, name))
    state.save_task(state.get_project("a"), _make_task("a", "eng-1", tmp_path / "a-wt1"))
    state.save_task(state.get_project("b"), _make_task("b", "eng-2", tmp_path / "b-wt2"))
    assert enumerate_tasks() == ["eng-1", "eng-2"]


def test_enumerate_tasks_project_filter(isolated_xdg: Path, tmp_path: Path) -> None:
    for name in ("a", "b"):
        root = tmp_path / name
        root.mkdir()
        state.register_project(_make_project(root, name))
    state.save_task(state.get_project("a"), _make_task("a", "eng-1", tmp_path / "a-wt"))
    state.save_task(state.get_project("b"), _make_task("b", "eng-2", tmp_path / "b-wt"))
    assert enumerate_tasks("a") == ["eng-1"]


def test_enumerate_sessions_across_tasks(isolated_xdg: Path, tmp_path: Path) -> None:
    root = tmp_path / "alpha"
    root.mkdir()
    state.register_project(_make_project(root))
    proj = state.get_project("alpha")
    state.save_task(
        proj,
        _make_task("alpha", "t1", tmp_path / "wt1", sessions=[_make_session("s-aaa")]),
    )
    state.save_task(
        proj,
        _make_task("alpha", "t2", tmp_path / "wt2", sessions=[_make_session("s-bbb")]),
    )
    assert enumerate_sessions() == ["s-aaa", "s-bbb"]


def test_enumerate_sessions_task_filter(isolated_xdg: Path, tmp_path: Path) -> None:
    root = tmp_path / "alpha"
    root.mkdir()
    state.register_project(_make_project(root))
    proj = state.get_project("alpha")
    state.save_task(
        proj,
        _make_task("alpha", "t1", tmp_path / "wt1", sessions=[_make_session("s-aaa")]),
    )
    state.save_task(
        proj,
        _make_task("alpha", "t2", tmp_path / "wt2", sessions=[_make_session("s-bbb")]),
    )
    assert enumerate_sessions(task_id="t1") == ["s-aaa"]


def test_complete_projects_prefix_filter(isolated_xdg: Path, tmp_path: Path) -> None:
    for name in ("alpha", "alphine", "beta"):
        root = tmp_path / name
        root.mkdir()
        state.register_project(_make_project(root, name))
    assert complete_projects("alp") == ["alpha", "alphine"]


def test_complete_tasks_prefix_filter(isolated_xdg: Path, tmp_path: Path) -> None:
    root = tmp_path / "a"
    root.mkdir()
    state.register_project(_make_project(root, "a"))
    proj = state.get_project("a")
    state.save_task(proj, _make_task("a", "eng-1", tmp_path / "wt1"))
    state.save_task(proj, _make_task("a", "plat-2", tmp_path / "wt2"))
    assert complete_tasks("eng") == ["eng-1"]


def test_complete_sessions_prefix_filter(isolated_xdg: Path, tmp_path: Path) -> None:
    root = tmp_path / "a"
    root.mkdir()
    state.register_project(_make_project(root, "a"))
    proj = state.get_project("a")
    state.save_task(
        proj,
        _make_task(
            "a",
            "t1",
            tmp_path / "wt",
            sessions=[_make_session("abc-1"), _make_session("xyz-1")],
        ),
    )
    assert complete_sessions("ab") == ["abc-1"]
