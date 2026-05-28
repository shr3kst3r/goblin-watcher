"""Internal `gw _describe` subcommand.

Invoked by `description.schedule_if_stale` in a detached subprocess. Not
listed in `gw --help`. Stable enough for end users to call by hand, but the
contract is internal and may change without notice.
"""

from __future__ import annotations

import typer

from goblin_watcher import description


def describe(
    project: str = typer.Argument(..., help="Project name."),
    task_id: str = typer.Argument(..., help="Task id."),
    session_id: str = typer.Argument(..., help="Session id."),
) -> None:
    """Generate and persist an LLM description for one session."""
    code = description.apply(project, task_id, session_id)
    if code != 0:
        raise typer.Exit(code=code)
