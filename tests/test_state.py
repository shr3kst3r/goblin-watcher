from datetime import UTC, datetime
from pathlib import Path

import pytest

from goblin_watcher import state
from goblin_watcher.errors import ProjectNotFoundError
from goblin_watcher.models import Project


def _make_project(root: Path, name: str = "alpha") -> Project:
    return Project(
        name=name,
        root=root,
        repo_url=None,
        default_branch="main",
        branch_prefix="",
        created_at=datetime.now(UTC),
    )


def test_load_global_returns_empty_when_no_state_file(isolated_xdg: Path) -> None:
    g = state.load_global()
    assert g.projects == {}


def test_register_project_persists_and_round_trips(isolated_xdg: Path, tmp_path: Path) -> None:
    root = tmp_path / "alpha-repo"
    root.mkdir()
    state.register_project(_make_project(root))

    g = state.load_global()
    assert g.projects == {"alpha": root}

    loaded = state.load_project(root)
    assert loaded.name == "alpha"
    assert loaded.root == root


def test_register_two_projects_both_appear(isolated_xdg: Path, tmp_path: Path) -> None:
    a = tmp_path / "a"
    b = tmp_path / "b"
    a.mkdir()
    b.mkdir()
    state.register_project(_make_project(a, "a"))
    state.register_project(_make_project(b, "b"))

    g = state.load_global()
    assert set(g.projects) == {"a", "b"}


def test_unregister_removes_from_registry(isolated_xdg: Path, tmp_path: Path) -> None:
    root = tmp_path / "alpha-repo"
    root.mkdir()
    state.register_project(_make_project(root))
    state.unregister_project("alpha")

    g = state.load_global()
    assert g.projects == {}


def test_unregister_unknown_project_raises(isolated_xdg: Path, tmp_path: Path) -> None:
    with pytest.raises(ProjectNotFoundError):
        state.unregister_project("nope")


def test_atomic_write_does_not_leave_temp_files(isolated_xdg: Path, tmp_path: Path) -> None:
    root = tmp_path / "alpha-repo"
    root.mkdir()
    state.register_project(_make_project(root))

    state_dir = isolated_xdg / "data" / "goblin-watcher"
    # `state.lock` is the advisory-lock sidecar (ADR 0004) and is expected to
    # persist; what must never survive a write is a `state.json.*` temp file.
    leftovers = [p for p in state_dir.iterdir() if p.name not in {"state.json", "state.lock"}]
    assert leftovers == []
