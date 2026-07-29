"""Advisory locking (ADR 0004): the lock itself, and the update helpers on top."""

from __future__ import annotations

import multiprocessing
import time
from datetime import UTC, datetime
from pathlib import Path

import pytest

from goblin_watcher import locks, paths, state
from goblin_watcher.models import Project, SessionRecord, Task


def _make_project(root: Path, name: str = "alpha") -> Project:
    return Project(
        name=name,
        root=root,
        repo_url=None,
        default_branch="main",
        created_at=datetime.now(UTC),
    )


def _make_task(task_id: str = "alpha-1") -> Task:
    return Task(
        id=task_id,
        project="alpha",
        branch=task_id,
        worktree_path=Path("/tmp/nonexistent") / task_id,
        base_branch="main",
        created_at=datetime.now(UTC),
    )


def _session(session_id: str, agent: str = "claude") -> SessionRecord:
    now = datetime.now(UTC)
    return SessionRecord(
        agent=agent,  # type: ignore[arg-type]
        session_id=session_id,
        created_at=now,
        last_used_at=now,
    )


def test_exclusive_creates_lock_file_and_releases(tmp_path: Path) -> None:
    lock = tmp_path / "nested" / "thing.lock"
    with locks.exclusive(lock):
        assert lock.exists()
    # Released: a second acquisition with a zero timeout succeeds immediately.
    with locks.exclusive(lock, timeout=0):
        pass


def test_exclusive_is_reentrant_across_sequential_acquisitions(tmp_path: Path) -> None:
    lock = tmp_path / "seq.lock"
    for _ in range(3):
        with locks.exclusive(lock, timeout=0.1):
            pass


def _hold_lock(lock_path: str, hold_seconds: float, ready) -> None:  # type: ignore[no-untyped-def]
    """Child-process helper: take the lock, signal, then hold it."""
    with locks.exclusive(Path(lock_path)):
        ready.set()
        time.sleep(hold_seconds)


def test_exclusive_times_out_when_another_process_holds_it(tmp_path: Path) -> None:
    lock = tmp_path / "contended.lock"
    ctx = multiprocessing.get_context("spawn")
    ready = ctx.Event()
    child = ctx.Process(target=_hold_lock, args=(str(lock), 2.0, ready))
    child.start()
    try:
        assert ready.wait(timeout=10), "child never acquired the lock"
        with pytest.raises(locks.LockTimeoutError), locks.exclusive(lock, timeout=0.2):
            pass
    finally:
        child.terminate()
        child.join(timeout=5)


def test_exclusive_zero_timeout_is_the_single_instance_idiom(tmp_path: Path) -> None:
    lock = tmp_path / "pass.lock"
    ctx = multiprocessing.get_context("spawn")
    ready = ctx.Event()
    child = ctx.Process(target=_hold_lock, args=(str(lock), 2.0, ready))
    child.start()
    try:
        assert ready.wait(timeout=10), "child never acquired the lock"
        with pytest.raises(locks.LockTimeoutError), locks.exclusive(lock, timeout=0):
            pass
    finally:
        child.terminate()
        child.join(timeout=5)


# ---------------------------------------------------------------------------
# state.update_task / update_global


def test_update_task_rereads_inside_the_lock(isolated_xdg: Path, tmp_path: Path) -> None:
    """The whole point of ADR 0004: the mutate callback sees on-disk state.

    Simulates the launcher's race — a caller holding a stale snapshot while
    another process writes the record — and asserts the concurrent write
    survives.
    """
    root = tmp_path / "repo"
    root.mkdir()
    project = _make_project(root)
    state.register_project(project)
    task = _make_task()
    state.save_task(project, task)

    stale_snapshot = state.load_task(project, task.id)

    # Another process lands a change the snapshot-holder never saw.
    state.save_task(project, stale_snapshot.model_copy(update={"pr_url": "https://pr/1"}))

    # The snapshot-holder patches only the field it owns.
    updated = state.update_task(
        project,
        task.id,
        lambda latest: latest.model_copy(update={"status": "pr-open"}),
    )

    assert updated.status == "pr-open"
    assert updated.pr_url == "https://pr/1", "concurrent write was clobbered"
    assert state.load_task(project, task.id).pr_url == "https://pr/1"


def test_update_task_interleaved_narrow_patches_both_survive(
    isolated_xdg: Path, tmp_path: Path
) -> None:
    """Two writers holding the same stale snapshot must not lose each other's field."""
    root = tmp_path / "repo"
    root.mkdir()
    project = _make_project(root)
    state.register_project(project)
    task = _make_task()
    state.save_task(project, task)

    snapshot_a = state.load_task(project, task.id)
    snapshot_b = state.load_task(project, task.id)
    assert snapshot_a.pr_url is None and snapshot_b.pr_url is None

    state.update_task(
        project, task.id, lambda latest: latest.model_copy(update={"pr_url": "https://pr/7"})
    )
    state.update_task(
        project, task.id, lambda latest: latest.model_copy(update={"status": "merged"})
    )

    final = state.load_task(project, task.id)
    assert final.pr_url == "https://pr/7"
    assert final.status == "merged"


def test_update_task_missing_record_raises(isolated_xdg: Path, tmp_path: Path) -> None:
    from goblin_watcher.errors import TaskNotFoundError

    root = tmp_path / "repo"
    root.mkdir()
    project = _make_project(root)
    state.register_project(project)

    with pytest.raises(TaskNotFoundError):
        state.update_task(project, "no-such-task", lambda t: t)


def test_task_lock_file_is_excluded_from_task_listing(isolated_xdg: Path, tmp_path: Path) -> None:
    root = tmp_path / "repo"
    root.mkdir()
    project = _make_project(root)
    state.register_project(project)
    task = _make_task()
    state.save_task(project, task)
    state.update_task(project, task.id, lambda t: t)

    lock = paths.task_lock_file(root, task.id)
    assert lock.exists(), "lock sidecar should persist"
    assert [t.id for t in state.list_tasks(project)] == [task.id]


def test_update_global_rereads_inside_the_lock(isolated_xdg: Path, tmp_path: Path) -> None:
    """Two concurrent registrations must not lose one another (state.py race)."""
    root_a = tmp_path / "a"
    root_a.mkdir()
    root_b = tmp_path / "b"
    root_b.mkdir()

    state.register_project(_make_project(root_a, name="a"))
    state.register_project(_make_project(root_b, name="b"))

    registry = state.load_global()
    assert set(registry.projects) == {"a", "b"}


def test_sessions_persist_refresh_preserves_concurrent_description(
    isolated_xdg: Path, tmp_path: Path
) -> None:
    """A description written by `gw _describe` must survive a status-path persist."""
    from goblin_watcher import sessions

    root = tmp_path / "repo"
    root.mkdir()
    project = _make_project(root)
    state.register_project(project)
    task = _make_task()
    task = sessions.upsert(task, _session("sess-1"))
    state.save_task(project, task)

    snapshot = state.load_task(project, task.id)

    # Background describe subprocess lands a description.
    state.update_task(
        project,
        task.id,
        lambda latest: sessions.patch_session(
            latest,
            "claude",
            "sess-1",
            {"description": "did a thing", "description_updated_at": datetime.now(UTC)},
        ),
    )

    # Foreground persists summary fields from its older snapshot.
    refreshed = sessions.patch_session(
        snapshot, "claude", "sess-1", {"summary": "latest snippet", "turn_count": 5}
    )
    sessions.persist_refresh(project, refreshed)

    final = state.load_task(project, task.id)
    assert final.sessions[0].summary == "latest snippet"
    assert final.sessions[0].description == "did a thing", "description was clobbered"
