"""Hidden enumerator commands called by the static zsh completion script.

Output contract (stable; shell scripts depend on it):
    - One id per line on stdout.
    - No headers, no decoration, no stderr.
    - Exit 0 even when nothing matches.

Lives under the `__complete` namespace so it never collides with a real
subcommand and stays invisible from `gw --help`.
"""

from __future__ import annotations

import sys

import typer

from goblin_watcher.completion_enumerators import (
    enumerate_projects,
    enumerate_sessions,
    enumerate_tasks,
)
from goblin_watcher.completion_spg import run as run_spg_hook

app = typer.Typer(hidden=True)


def _emit(items: list[str]) -> None:
    for item in items:
        sys.stdout.write(f"{item}\n")


@app.command("projects")
def projects() -> None:
    _emit(enumerate_projects())


@app.command("tasks")
def tasks(
    project: str | None = typer.Option(None, "--project", help="Limit to one project."),
) -> None:
    _emit(enumerate_tasks(project))


@app.command("sessions")
def sessions(
    project: str | None = typer.Option(None, "--project", help="Limit to one project."),
    task: str | None = typer.Option(None, "--task", help="Limit to one task id."),
) -> None:
    _emit(enumerate_sessions(project, task))


# `ignore_unknown_options` so that user-typed flags like `--li` pass through
# as raw positional args instead of being parsed by Click.
@app.command(
    "spg",
    context_settings={"ignore_unknown_options": True, "allow_extra_args": True},
)
def spg(ctx: typer.Context) -> None:
    """Completion hook for spg's per-command dispatcher.

    Invoke as `gw __complete spg <cursor-index> [words-after-gw...]`.
    """
    args = list(ctx.args)
    if not args:
        return
    try:
        index = int(args[0])
    except ValueError:
        return
    run_spg_hook(index, args[1:])


@app.command(
    "spg-cd",
    context_settings={"ignore_unknown_options": True, "allow_extra_args": True},
)
def spg_cd(ctx: typer.Context) -> None:
    """Completion hook for the `gwcd` / `gwcode` shell wrappers.

    Same protocol as `spg`, but walks the gw tree starting at `cd` so the
    positional offers task ids and `--project` offers project names.
    """
    args = list(ctx.args)
    if not args:
        return
    try:
        index = int(args[0])
    except ValueError:
        return
    run_spg_hook(index, args[1:], subcommand=("cd",))
