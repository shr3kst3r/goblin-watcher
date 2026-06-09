"""Print a task's worktree path so a shell wrapper can act on it.

A subprocess can't change its parent shell's directory. So this command
prints only the resolved worktree path on stdout; everything else (errors,
hints, picker prompts) goes to stderr / `/dev/tty`. The companion `gwcd`
and `gwcode` shell functions (published from `spg.toml`) wrap this and
run `cd` / `code` on the result.
"""

from __future__ import annotations

import sys

import typer

from goblin_watcher import state
from goblin_watcher.completion_enumerators import complete_projects, complete_tasks
from goblin_watcher.task_resolver import resolve_task


def cd(
    target: str | None = typer.Argument(
        None,
        help="Task id or path. Defaults to cwd; opens a picker if no task is inferrable.",
        autocompletion=complete_tasks,
    ),
    project: str | None = typer.Option(
        None,
        "--project",
        help="Limit task lookup and the picker to a single project.",
        autocompletion=complete_projects,
    ),
) -> None:
    """Print the worktree path of a task (the workspace for a multi-repo task).

    Direct use: `cd "$(gw cd eng-123)"` or `code "$(gw cd eng-123)"`.

    For one-step interactive UX (`gwcd eng-123`, `gwcode eng-123`), install
    spg (`spg install` from the repo root) — it publishes those shell
    functions from this project's `spg.toml`.
    """
    project_filter: str | None = None
    if project is not None:
        normalized = project.strip().lower()
        state.get_project(normalized)
        project_filter = normalized
    task = resolve_task(target, project_filter)
    # Multi-repo tasks land in the workspace — the same directory the agent
    # runs in — so `gwcd` puts the user where all the repos are visible.
    sys.stdout.write(f"{task.agent_cwd}\n")
