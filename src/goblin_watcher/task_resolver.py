"""Resolve a Project or Task from a user-supplied id, path, or interactive pickers.

Shared by `gw run`, `gw cd`, and every command that needs a project but didn't
get `--project NAME`. Pickers write to `/dev/tty` via questionary, and the
"Cancelled." line is routed to stderr so command substitution
(`$(gw cd ...)`) stays clean.
"""

from __future__ import annotations

from datetime import datetime
from pathlib import Path

import typer

from goblin_watcher import state
from goblin_watcher.console import err_console
from goblin_watcher.errors import GoblinError, TaskNotFoundError
from goblin_watcher.models import Project, Task
from goblin_watcher.picker import choose_project, choose_task


def resolve_project(name: str | None) -> Project:
    """Resolve a project by explicit name, or open the project picker.

    Single-project case auto-picks. Zero registered → `GoblinError`.
    User cancel → `typer.Exit(1)`.
    """
    if name is not None:
        return state.get_project(name.strip().lower())

    rows = _project_rows()
    if not rows:
        raise GoblinError(
            "No projects registered yet.",
            hint="Run `gw project new <name> --repo <url>` or `gw <LINEAR-ID> --repo <url>`.",
        )
    if len(rows) == 1:
        return rows[0][0]
    picked = choose_project(rows)
    if picked is None:
        err_console.print("[muted]Cancelled.[/]")
        raise typer.Exit(code=1)
    return picked


def _project_rows() -> list[tuple[Project, int, datetime | None]]:
    rows: list[tuple[Project, int, datetime | None]] = []
    for name in state.load_global().projects:
        try:
            proj = state.get_project(name)
        except GoblinError:
            continue
        tasks = state.list_tasks(proj)
        last = max(
            (s.last_used_at for t in tasks for s in t.sessions),
            default=None,
        )
        rows.append((proj, len(tasks), last))
    return rows


def resolve_task(target: str | None, project_filter: str | None) -> Task:
    """Resolve a task from explicit id, path, cwd, or interactive pickers.

    When `project_filter` is set, scoping is restricted to that single project
    for both task-id lookups and the picker chain.
    """
    project_names = (
        [project_filter] if project_filter is not None else list(state.load_global().projects)
    )

    if target:
        as_path = Path(target).expanduser()
        if as_path.exists():
            return _task_for_path(as_path, project_names)
        matches: list[Task] = []
        for name in project_names:
            try:
                proj = state.get_project(name)
                matches.append(state.load_task(proj, target))
            except (GoblinError, TaskNotFoundError):
                continue
        if len(matches) == 1:
            return matches[0]
        if len(matches) > 1:
            names = ", ".join(t.project for t in matches)
            raise GoblinError(
                f"Task {target!r} exists in more than one project: {names}.",
                hint=f"Disambiguate with --project, e.g. `--project {matches[0].project}`.",
            )
        scope = f" in project {project_filter!r}" if project_filter else ""
        raise GoblinError(
            f"No task or path matches {target!r}{scope}.",
            hint="Pass a task id (e.g. `eng-123`) or a path inside a worktree.",
        )

    if project_filter is None:
        try:
            return _task_for_path(Path.cwd(), project_names)
        except GoblinError:
            pass
    return _pick_task_via_chain(project_filter)


def _pick_task_via_chain(project_filter: str | None) -> Task:
    """Project picker → task picker. Skips a level when there's only one option."""
    proj = resolve_project(project_filter)
    tasks = state.list_tasks(proj)
    if not tasks:
        raise GoblinError(
            f"Project {proj.name!r} has no tasks yet.",
            hint=(
                "Run `gw <LINEAR-ID>` to start work on a Linear ticket, or "
                "`gw new --branch-name <name>` for a fresh branch."
            ),
        )
    if len(tasks) == 1:
        return tasks[0]
    picked_task = choose_task(tasks)
    if picked_task is None:
        err_console.print("[muted]Cancelled.[/]")
        raise typer.Exit(code=1)
    return picked_task


def _task_for_path(path: Path, project_names: list[str] | None = None) -> Task:
    resolved = path.resolve()
    names = project_names if project_names is not None else list(state.load_global().projects)
    for name in names:
        try:
            proj = state.get_project(name)
        except GoblinError:
            continue
        for task in state.list_tasks(proj):
            for repo in task.all_repos():
                try:
                    resolved.relative_to(repo.worktree_path.resolve())
                    return task
                except ValueError:
                    continue
    raise GoblinError(
        f"{path} is not inside any known task's worktree.",
        hint="Run from a task's worktree, or pass a TASK-ID.",
    )
