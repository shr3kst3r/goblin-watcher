"""CRUD for the user-configured addition appended to fresh-spawn seed prompts.

Two scopes (`--global`, `--project [NAME]`); project overrides global when the
project file exists. Write commands default to `--global` when neither flag is
given. `--project` may be passed alone (opens the project picker) or with a
project name (`--project foo`) to target a specific registered project.

`--project` accepts an optional value: cli._inject_project_sentinel rewrites a
bare `--project` (followed by another flag or end-of-args) into
`--project <PROJECT_PICK_SENTINEL>`, which this module resolves via the project
picker (single-project auto-pick).
"""

from __future__ import annotations

import sys
from dataclasses import dataclass
from pathlib import Path

import click
import typer

from goblin_watcher import prompt_addition, state
from goblin_watcher.completion_enumerators import complete_projects
from goblin_watcher.console import console, print_success
from goblin_watcher.errors import GoblinError
from goblin_watcher.models import Project
from goblin_watcher.task_resolver import resolve_project as _pick_project

app = typer.Typer()


# Sentinel spliced into argv by `cli._inject_project_sentinel` when `--project`
# is passed without a value. Resolved here via the project picker.
PROJECT_PICK_SENTINEL = "\0gw-pick-project"


@dataclass
class _Global:
    pass


@dataclass
class _ProjectScope:
    project: Project


_Scope = _Global | _ProjectScope


def _resolve_project(project: str) -> Project:
    if project == PROJECT_PICK_SENTINEL:
        return _pick_project(None)
    return state.get_project(project)


def _project_option_help(verb: str) -> str:
    return (
        f"{verb} the project's addition. "
        "Pass `--project` alone to open the project picker, or `--project NAME` "
        "to target a specific registered project."
    )


def _resolve_scope(*, is_global: bool, project: str | None, default_global: bool) -> _Scope:
    if is_global and project is not None:
        raise GoblinError("Pass either --global or --project, not both.")
    if project is not None:
        return _ProjectScope(_resolve_project(project))
    if is_global or default_global:
        return _Global()
    raise GoblinError(
        "Specify --global or --project.",
        hint="Use --global for the user-wide addition, --project for a project's.",
    )


def _scope_label(scope: _Scope) -> str:
    return "global" if isinstance(scope, _Global) else f"project '{scope.project.name}'"


def _scope_path(scope: _Scope) -> Path:
    if isinstance(scope, _Global):
        return prompt_addition.global_file()
    return prompt_addition.project_file(scope.project)


def _load(scope: _Scope) -> str:
    if isinstance(scope, _Global):
        return prompt_addition.load_global()
    return prompt_addition.load_project(scope.project)


def _save(scope: _Scope, text: str) -> None:
    if isinstance(scope, _Global):
        prompt_addition.save_global(text)
    else:
        prompt_addition.save_project(scope.project, text)


def _clear(scope: _Scope) -> bool:
    if isinstance(scope, _Global):
        return prompt_addition.clear_global()
    return prompt_addition.clear_project(scope.project)


def _render_text(text: str) -> None:
    if text == "":
        console.print("[muted](empty)[/]")
        return
    console.print(text, end="" if text.endswith("\n") else "\n")


@app.command("show")
def show(
    is_global: bool = typer.Option(False, "--global", help="Show only the global addition."),
    project: str | None = typer.Option(
        None, "--project", help=_project_option_help("Show"), autocompletion=complete_projects
    ),
) -> None:
    """Show the prompt addition.

    With no flag, prints the resolved addition (what a fresh spawn would receive)
    plus its source. `--global` and `--project` print one scope each.
    """
    if is_global and project is not None:
        raise GoblinError("Pass either --global or --project, not both.")

    if is_global:
        text = prompt_addition.load_global()
        console.print(f"[muted]source: global ({prompt_addition.global_file()})[/]")
        console.print()
        _render_text(text)
        return

    if project is not None:
        proj = _resolve_project(project)
        if not prompt_addition.has_project_override(proj):
            console.print(
                f"[muted]project '{proj.name}' has no addition file "
                f"({prompt_addition.project_file(proj)})[/]"
            )
            return
        text = prompt_addition.load_project(proj)
        console.print(
            f"[muted]source: project '{proj.name}' ({prompt_addition.project_file(proj)})[/]"
        )
        console.print()
        _render_text(text)
        return

    # Default: show the resolved addition + source. Pick a project (single
    # auto-picks); fall back to global when nothing is registered.
    proj = _pick_project(None) if state.load_global().projects else None
    if proj is not None and prompt_addition.has_project_override(proj):
        source = f"project '{proj.name}' (overrides global)"
        text = prompt_addition.load_project(proj)
    elif prompt_addition.global_file().exists():
        source = "global"
        text = prompt_addition.load_global()
    else:
        console.print("[muted](no prompt addition configured)[/]")
        console.print('[hint]Hint:[/] set one with `gw prompt set "…"` or `gw prompt edit`.')
        return

    console.print(f"[muted]source: {source}[/]")
    console.print()
    _render_text(text)


@app.command("set")
def set_cmd(
    text: str | None = typer.Argument(None, help="Text to store. If omitted, reads from stdin."),
    is_global: bool = typer.Option(
        False, "--global", help="Write to the global addition (default)."
    ),
    project: str | None = typer.Option(
        None,
        "--project",
        help=_project_option_help("Write to"),
        autocompletion=complete_projects,
    ),
) -> None:
    """Set the prompt addition text.

    Saving an empty string at --project scope suppresses the global addition
    for that project (file presence is the override signal, not content).
    """
    scope = _resolve_scope(is_global=is_global, project=project, default_global=True)

    if text is None:
        if sys.stdin.isatty():
            raise GoblinError(
                "No text provided.",
                hint=(
                    'Pass TEXT (e.g. `gw prompt set "…"`), pipe via stdin, or use `gw prompt edit`.'
                ),
            )
        text = sys.stdin.read()

    _save(scope, text)
    print_success(f"Saved {_scope_label(scope)} prompt addition ({_scope_path(scope)})")


@app.command("edit")
def edit(
    is_global: bool = typer.Option(False, "--global", help="Edit the global addition (default)."),
    project: str | None = typer.Option(
        None, "--project", help=_project_option_help("Edit"), autocompletion=complete_projects
    ),
) -> None:
    """Open $EDITOR to edit the prompt addition."""
    scope = _resolve_scope(is_global=is_global, project=project, default_global=True)
    current = _load(scope)
    edited = click.edit(text=current, extension=".md", require_save=True)
    if edited is None:
        console.print("[muted]No changes saved.[/]")
        return
    _save(scope, edited)
    print_success(f"Saved {_scope_label(scope)} prompt addition ({_scope_path(scope)})")


@app.command("clear")
def clear(
    is_global: bool = typer.Option(False, "--global", help="Clear the global addition (default)."),
    project: str | None = typer.Option(
        None, "--project", help=_project_option_help("Clear"), autocompletion=complete_projects
    ),
    force: bool = typer.Option(False, "--force", help="Skip the confirmation prompt."),
) -> None:
    """Delete the prompt addition file."""
    scope = _resolve_scope(is_global=is_global, project=project, default_global=True)
    path = _scope_path(scope)
    if not path.exists():
        console.print(f"[muted]No {_scope_label(scope)} prompt addition to clear.[/]")
        return
    if not force and not typer.confirm(
        f"Delete {_scope_label(scope)} prompt addition at {path}?", default=False
    ):
        console.print("[muted]Cancelled.[/]")
        raise typer.Exit(code=1)
    _clear(scope)
    print_success(f"Cleared {_scope_label(scope)} prompt addition")
