"""Cheap enumerations of project / task / session ids for shell completion.

These run on every tab press, so they must be fast and silent: read JSON state
directly, swallow filesystem errors, never touch the network or git. Used by
both the hidden `gw __complete` CLI (called from the static zsh script) and the
Typer `autocompletion=` callbacks (which power bash/fish dynamic completion).
"""

from __future__ import annotations

import contextlib

from goblin_watcher import state


def enumerate_projects() -> list[str]:
    """All registered project names, sorted."""
    with contextlib.suppress(Exception):
        return sorted(state.load_global().projects)
    return []


def enumerate_modes() -> list[str]:
    """Built-in work modes plus any the user defined under `[modes.*]`, sorted."""
    with contextlib.suppress(Exception):
        from goblin_watcher import config, modes

        return modes.mode_names(config.load().modes)
    return sorted(_builtin_mode_names())


def _builtin_mode_names() -> list[str]:
    from goblin_watcher.modes import BUILTIN_MODES

    return sorted(BUILTIN_MODES)


def enumerate_tasks(project: str | None = None) -> list[str]:
    """Task ids, optionally limited to one project. Deduped + sorted."""
    names = [project] if project else enumerate_projects()
    seen: set[str] = set()
    for name in names:
        try:
            proj = state.get_project(name)
            for task in state.list_tasks(proj):
                seen.add(task.id)
        except Exception:  # nosec B112 - completion must never error on a stale/corrupt project record
            continue
    return sorted(seen)


def enumerate_sessions(project: str | None = None, task_id: str | None = None) -> list[str]:
    """Session ids across tasks. Optional filters narrow the search."""
    names = [project] if project else enumerate_projects()
    seen: set[str] = set()
    for name in names:
        try:
            proj = state.get_project(name)
            tasks = state.list_tasks(proj)
        except Exception:  # nosec B112 - completion must never error on a stale/corrupt project record
            continue
        for task in tasks:
            if task_id and task.id != task_id:
                continue
            for s in task.sessions:
                seen.add(s.session_id)
    return sorted(seen)


# ---------- Typer autocompletion callbacks (used by bash/fish dynamic completion).
# Each takes a partial `incomplete` string and returns matching items.


def complete_projects(incomplete: str) -> list[str]:
    return [n for n in enumerate_projects() if n.startswith(incomplete)]


def complete_tasks(incomplete: str) -> list[str]:
    return [t for t in enumerate_tasks() if t.startswith(incomplete)]


def complete_sessions(incomplete: str) -> list[str]:
    return [s for s in enumerate_sessions() if s.startswith(incomplete)]


def complete_modes(incomplete: str) -> list[str]:
    return [m for m in enumerate_modes() if m.startswith(incomplete)]
