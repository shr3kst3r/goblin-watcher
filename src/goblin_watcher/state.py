"""Persistence: global registry + per-project state.

Global state (the project registry) lives under XDG data dir.
Per-project state (Project + tasks) lives under <project_root>/.goblin/.

All writes are atomic (tmp file + os.replace).
"""

from __future__ import annotations

import json
import os
import tempfile
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

from pydantic import ValidationError

from goblin_watcher import locks, paths
from goblin_watcher.errors import ProjectNotFoundError, TaskNotFoundError
from goblin_watcher.models import GlobalState, Project, Task


def _atomic_write_text(target: Path, text: str) -> None:
    target.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_str = tempfile.mkstemp(prefix=target.name + ".", dir=str(target.parent))
    tmp = Path(tmp_str)
    try:
        with os.fdopen(fd, "w") as f:
            f.write(text)
        tmp.replace(target)
    except Exception:
        tmp.unlink(missing_ok=True)
        raise


def _json_default(value: Any) -> Any:
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, Path):
        return str(value)
    raise TypeError(f"Unserializable type: {type(value).__name__}")


def _write_json(target: Path, payload: dict[str, Any]) -> None:
    _atomic_write_text(target, json.dumps(payload, indent=2, default=_json_default) + "\n")


def write_json_atomic(target: Path, payload: dict[str, Any]) -> None:
    """Atomically write `payload` as JSON to an arbitrary path.

    The public seam for modules that keep their own JSON files (e.g. the sync
    tier) and want the same tmp-file-plus-rename guarantee as project state,
    without reaching into this module's internals.
    """
    _write_json(target, payload)


def load_global() -> GlobalState:
    f = paths.state_file()
    if not f.exists():
        return GlobalState()
    raw = json.loads(f.read_text())
    # Drop legacy fields removed during the "no current project" migration so
    # users with an existing state.json from earlier releases still load.
    raw.pop("current_project", None)
    return GlobalState.model_validate(raw)


def save_global(state: GlobalState) -> None:
    _write_json(paths.state_file(), state.model_dump(mode="json"))


def project_file(project_root: Path) -> Path:
    return paths.project_meta_dir(project_root) / "project.json"


def load_project(project_root: Path) -> Project:
    f = project_file(project_root)
    if not f.exists():
        raise ProjectNotFoundError(
            f"No project metadata at {f}.",
            hint="Run `gw project new` to register this directory.",
        )
    return Project.model_validate_json(f.read_text())


def save_project(project: Project) -> None:
    _write_json(project_file(project.root), project.model_dump(mode="json"))


def project_root_for(name: str, state: GlobalState | None = None) -> Path:
    state = state or load_global()
    root = state.projects.get(name)
    if root is None:
        raise ProjectNotFoundError(
            f"No project named {name!r}.",
            hint="Run `gw project ls` to see registered projects.",
        )
    return root


def get_project(name: str, state: GlobalState | None = None) -> Project:
    return load_project(project_root_for(name, state))


def update_global(mutate: Callable[[GlobalState], GlobalState]) -> GlobalState:
    """Apply `mutate` to the registry under an exclusive lock (ADR 0004).

    The lock spans the *read*: the registry is re-loaded from disk inside it, so
    `mutate` always sees current state and a concurrent writer's entry can't be
    lost. `mutate` must be cheap and side-effect-free — no network, no
    subprocesses — so lock hold times stay in milliseconds.
    """
    with locks.exclusive(paths.global_lock_file()):
        updated = mutate(load_global())
        save_global(updated)
        return updated


def register_project(project: Project) -> None:
    """Persist the project record and add it to the global registry."""
    paths.project_meta_dir(project.root).mkdir(parents=True, exist_ok=True)
    paths.project_tasks_dir(project.root).mkdir(parents=True, exist_ok=True)
    save_project(project)

    def _add(state: GlobalState) -> GlobalState:
        state.projects[project.name] = project.root
        return state

    update_global(_add)


def unregister_project(name: str) -> None:
    def _remove(state: GlobalState) -> GlobalState:
        if name not in state.projects:
            raise ProjectNotFoundError(f"No project named {name!r}.")
        del state.projects[name]
        return state

    update_global(_remove)


# ---------------------------------------------------------------------------
# Task persistence (per-project)


def task_file(project: Project, task_id: str) -> Path:
    return paths.project_tasks_dir(project.root) / f"{task_id}.json"


def save_task(project: Project, task: Task) -> None:
    paths.project_tasks_dir(project.root).mkdir(parents=True, exist_ok=True)
    _write_json(task_file(project, task.id), task.model_dump(mode="json"))


def load_task(project: Project, task_id: str) -> Task:
    f = task_file(project, task_id)
    if not f.exists():
        raise TaskNotFoundError(
            f"No task {task_id!r} in project {project.name!r}.",
            hint="Run `gw task ls` to see this project's tasks.",
        )
    return Task.model_validate_json(f.read_text())


def update_task(project: Project, task_id: str, mutate: Callable[[Task], Task]) -> Task:
    """Apply `mutate` to one task record under an exclusive lock (ADR 0004).

    The lock spans the *read*: the task is re-loaded from disk inside it, so
    `mutate` receives current state rather than a snapshot the caller may have
    been holding for minutes. Callers pass a narrow patch that touches only the
    fields they own — persisting a whole stale `Task` is what this exists to
    prevent.

    `mutate` must be cheap and side-effect-free (no network, no subprocesses):
    it runs with the lock held. Raises `TaskNotFoundError` if the record is gone
    by the time the lock is acquired.
    """
    with locks.exclusive(paths.task_lock_file(project.root, task_id)):
        updated = mutate(load_task(project, task_id))
        save_task(project, updated)
        return updated


@dataclass(frozen=True)
class UnreadableTask:
    """A task record on disk that this build of gw could not parse.

    `unknown_fields` is the tell that matters. `Task` is `extra="forbid"`, so a
    record carrying a field this process has never heard of was written by a
    *newer* gw — which is not corruption, it is version skew, and the fix is to
    restart rather than to repair anything. That distinction is the whole reason
    this type exists: silently skipping the file made a stale long-running
    `gw status --watch` render an empty tree with no way to tell why (gh-51).
    """

    path: Path
    error: str
    unknown_fields: tuple[str, ...] = field(default_factory=tuple)

    @property
    def task_id(self) -> str:
        return self.path.stem

    @property
    def newer_schema(self) -> bool:
        """True when the record looks like it came from a newer gw."""
        return bool(self.unknown_fields)


@dataclass(frozen=True)
class TaskScan:
    """Everything `<project>/.goblin/tasks/` had to say: what loaded, what didn't."""

    tasks: list[Task] = field(default_factory=list)
    unreadable: list[UnreadableTask] = field(default_factory=list)

    @property
    def newer_schema(self) -> list[UnreadableTask]:
        return [u for u in self.unreadable if u.newer_schema]


def _unknown_fields(error: Exception) -> tuple[str, ...]:
    """Field names pydantic rejected as unknown, in order. () for other errors."""
    if not isinstance(error, ValidationError):
        return ()
    names: list[str] = []
    for err in error.errors():
        if err.get("type") != "extra_forbidden":
            continue
        loc = err.get("loc") or ()
        name = str(loc[-1]) if loc else "?"
        if name not in names:
            names.append(name)
    return tuple(names)


def scan_tasks(project: Project) -> TaskScan:
    """Load every task record, keeping the failures instead of dropping them.

    Callers that only need the healthy records use `list_tasks`. Anything that
    reports to a human should use this, so "no tasks" and "gw could not read
    these tasks" don't render identically.
    """
    d = paths.project_tasks_dir(project.root)
    if not d.exists():
        return TaskScan()
    tasks: list[Task] = []
    unreadable: list[UnreadableTask] = []
    for f in sorted(d.glob("*.json")):
        try:
            tasks.append(Task.model_validate_json(f.read_text()))
        except Exception as e:  # a bad record must not break listing
            unreadable.append(
                UnreadableTask(path=f, error=str(e).strip(), unknown_fields=_unknown_fields(e))
            )
    return TaskScan(tasks=tasks, unreadable=unreadable)


def list_tasks(project: Project) -> list[Task]:
    """Every task record that parses. Unreadable ones are skipped silently.

    Kept for the many callers that act on tasks and have no way to report a
    broken record anyway. Use `scan_tasks` when a human is going to read the
    result.
    """
    return scan_tasks(project).tasks


def delete_task_record(project: Project, task_id: str) -> None:
    f = task_file(project, task_id)
    if not f.exists():
        raise TaskNotFoundError(f"No task {task_id!r} in project {project.name!r}.")
    f.unlink()


def find_parent_task(project: Project, task: Task) -> Task | None:
    """The task `task` is stacked on, or None when unset or the record is gone.

    A missing parent record is not an error: it is the normal end state, since a
    parent gets pruned once it lands. `Task.parent_task` is deliberately not a
    validated reference for exactly that reason.
    """
    if task.parent_task is None:
        return None
    try:
        return load_task(project, task.parent_task)
    except TaskNotFoundError:
        return None


def find_task_by_branch(project: Project, branch: str) -> Task | None:
    """The task whose *primary* branch is `branch`, or None if no task owns it.

    Primary only, deliberately: this answers "which task does this base branch
    belong to?" for stacking, and only a primary branch is something another
    task can be cut from and tracked against (`Task.parent_task`).
    """
    for t in list_tasks(project):
        if t.kind != "scratch" and t.branch == branch:
            return t
    return None


def find_task_by_worktree(project: Project, worktree_path: Path) -> Task | None:
    target = worktree_path.resolve()
    for t in list_tasks(project):
        if any(r.worktree_path.resolve() == target for r in t.all_repos()):
            return t
    return None
