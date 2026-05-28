"""Persistence: global registry + per-project state.

Global state (the project registry) lives under XDG data dir.
Per-project state (Project + tasks) lives under <project_root>/.goblin/.

All writes are atomic (tmp file + os.replace).
"""

from __future__ import annotations

import json
import os
import tempfile
from datetime import datetime
from pathlib import Path
from typing import Any

from goblin_watcher import paths
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


def register_project(project: Project) -> None:
    """Persist the project record and add it to the global registry."""
    paths.project_meta_dir(project.root).mkdir(parents=True, exist_ok=True)
    paths.project_tasks_dir(project.root).mkdir(parents=True, exist_ok=True)
    save_project(project)
    state = load_global()
    state.projects[project.name] = project.root
    save_global(state)


def unregister_project(name: str) -> None:
    state = load_global()
    if name not in state.projects:
        raise ProjectNotFoundError(f"No project named {name!r}.")
    del state.projects[name]
    save_global(state)


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


def list_tasks(project: Project) -> list[Task]:
    d = paths.project_tasks_dir(project.root)
    if not d.exists():
        return []
    tasks: list[Task] = []
    for f in sorted(d.glob("*.json")):
        try:
            tasks.append(Task.model_validate_json(f.read_text()))
        except Exception:  # nosec B112 - skip unreadable task files rather than crash listing
            continue
    return tasks


def delete_task_record(project: Project, task_id: str) -> None:
    f = task_file(project, task_id)
    if not f.exists():
        raise TaskNotFoundError(f"No task {task_id!r} in project {project.name!r}.")
    f.unlink()


def find_task_by_worktree(project: Project, worktree_path: Path) -> Task | None:
    target = worktree_path.resolve()
    for t in list_tasks(project):
        if t.worktree_path.resolve() == target:
            return t
    return None
