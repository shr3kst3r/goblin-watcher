"""`gw scratch` — standalone scratch spaces, not associated with any project.

A scratch space is a plain directory (no git repo) where an agent is launched
and its sessions are tracked and resumable like any other task. All scratch
tasks live under the single reserved "scratch" project, registered lazily on
first use with its root at `~/goblin/scratch/`; each space is a subdirectory.
"""

from __future__ import annotations

from datetime import UTC, datetime

import click
import typer

from goblin_watcher import config, paths, state
from goblin_watcher.agents import AGENT_NAMES, get_agent, validate_agent_for_project
from goblin_watcher.agents.launcher import Fresh, build_seed_prompt, launch
from goblin_watcher.console import agent_badge, console, print_settings, print_success
from goblin_watcher.errors import GoblinError, ProjectNotFoundError, TaskNotFoundError
from goblin_watcher.models import Project, Task
from goblin_watcher.slug import random_scratch_name, slugify
from goblin_watcher.windowing import get_windower

SCRATCH_PROJECT_NAME = "scratch"


def _now() -> datetime:
    return datetime.now(UTC)


def ensure_scratch_project() -> Project:
    """Return the reserved scratch container project, registering it on first use."""
    try:
        proj = state.get_project(SCRATCH_PROJECT_NAME)
    except ProjectNotFoundError:
        proj = None
    if proj is not None:
        if proj.kind != "scratch":
            raise GoblinError(
                f"A regular project named {SCRATCH_PROJECT_NAME!r} is already "
                f"registered at {proj.root}.",
                hint=(
                    "The name is reserved for scratch spaces. Unregister it "
                    "(`gw project rm scratch`) and re-register it under another name."
                ),
            )
        return proj
    root = paths.scratch_root()
    root.mkdir(parents=True, exist_ok=True)
    proj = Project(
        name=SCRATCH_PROJECT_NAME,
        kind="scratch",
        root=root,
        created_at=_now(),
    )
    state.register_project(proj)
    return proj


def _is_taken(proj: Project, name: str) -> bool:
    if (proj.root / name).exists():
        return True
    try:
        state.load_task(proj, name)
    except TaskNotFoundError:
        return False
    return True


def _unique_name(proj: Project, base: str) -> str:
    """If `base` is taken (dir or task record), append -2, -3, ..."""
    if not _is_taken(proj, base):
        return base
    n = 2
    while _is_taken(proj, f"{base}-{n}"):
        n += 1
    return f"{base}-{n}"


def scratch(
    name: str | None = typer.Argument(
        None, help="Name for the scratch space (auto-generated when omitted)."
    ),
    agent: str | None = typer.Option(
        None,
        "--agent",
        help="Agent to launch.",
        click_type=click.Choice(list(AGENT_NAMES)),
    ),
    no_launch: bool = typer.Option(
        False, "--no-launch", help="Create the scratch space but do not launch."
    ),
    windowing: str | None = typer.Option(
        None,
        "--windowing",
        help="Overrides config.",
        click_type=click.Choice(["inline", "tmux"]),
    ),
    unsafe: bool | None = typer.Option(
        None,
        "--unsafe/--no-unsafe",
        help="Run the agent with its bypass-permission flag (e.g. claude's "
        "--dangerously-skip-permissions). Overrides defaults.unsafe in config.",
    ),
    prompt: str | None = typer.Option(
        None,
        "--prompt",
        help="Initial prompt to seed the fresh session with. The agent begins "
        "work on this immediately instead of waiting for the next message.",
    ),
) -> None:
    """Create a scratch space: a plain directory not associated with any project."""
    if prompt is not None and no_launch:
        raise GoblinError(
            "--prompt has no effect with --no-launch (no session is started).",
            hint="Drop --no-launch, or drop --prompt.",
        )

    proj = ensure_scratch_project()
    base = slugify(name) if name else random_scratch_name()
    final = _unique_name(proj, base)
    directory = proj.root / final
    directory.mkdir(parents=True)

    task = Task(
        id=final,
        kind="scratch",
        project=proj.name,
        branch=final,
        worktree_path=directory,
        base_branch="",
        created_at=_now(),
    )
    state.save_task(proj, task)

    cfg = config.load()
    agent_name = agent or cfg.defaults.agent or "claude"
    windowing_mode = windowing or cfg.defaults.windowing
    unsafe_mode = cfg.defaults.unsafe if unsafe is None else unsafe

    print_success(f"Created scratch space {final!r}")
    print_settings(
        [
            ("directory", str(directory)),
            ("agent", agent_name),
            ("windowing", windowing_mode),
            ("unsafe", str(unsafe_mode).lower()),
            ("no_launch", str(no_launch).lower()),
        ]
    )

    if no_launch:
        return

    validate_agent_for_project(agent_name, proj)
    agent_obj = get_agent(agent_name)
    windower = get_windower(windowing_mode)
    choice = Fresh(prompt=build_seed_prompt(task, user_prompt=prompt))
    console.print(f"Launching {agent_badge(agent_name)} (fresh) in [muted]{windowing_mode}[/]…")
    exit_code, _ = launch(
        project=proj,
        task=task,
        agent=agent_obj,
        choice=choice,
        windower=windower,
        unsafe=unsafe_mode,
    )
    if exit_code != 0:
        raise typer.Exit(code=exit_code)
