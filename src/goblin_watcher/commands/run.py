from __future__ import annotations

import click
import typer

from goblin_watcher import config, sessions, state
from goblin_watcher.agents import AGENT_NAMES, get_agent, validate_agent_for_project
from goblin_watcher.agents.launcher import Fresh, Resume, build_seed_prompt
from goblin_watcher.agents.launcher import launch as launch_agent
from goblin_watcher.completion_enumerators import (
    complete_projects,
    complete_sessions,
    complete_tasks,
)
from goblin_watcher.console import agent_badge, console, print_settings
from goblin_watcher.errors import GoblinError
from goblin_watcher.picker import (
    SESSION_PICK_SENTINEL,
    CancelChoice,
    FreshChoice,
    ResumeChoice,
    choose_session,
)
from goblin_watcher.task_resolver import resolve_task
from goblin_watcher.windowing import get_windower


def run(
    target: str | None = typer.Argument(
        None, help="Task id or path. Defaults to cwd.", autocompletion=complete_tasks
    ),
    agent: str | None = typer.Option(
        None,
        "--agent",
        help="Agent to launch.",
        click_type=click.Choice(list(AGENT_NAMES)),
    ),
    session: str | None = typer.Option(
        None,
        "--session",
        help="Resume a specific session id. Pass `--session` with no value to open the picker.",
        autocompletion=complete_sessions,
    ),
    new: bool = typer.Option(False, "--new", help="Force a new session, skipping the picker."),
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
    project: str | None = typer.Option(
        None,
        "--project",
        help="Limit task lookup and the picker to a single project.",
        autocompletion=complete_projects,
    ),
    prompt: str | None = typer.Option(
        None,
        "--prompt",
        help="Initial prompt for a fresh session. Implies a new session "
        "(cannot be combined with --session).",
    ),
) -> None:
    """Pick a session for an existing task and spawn the agent."""
    if new and session is not None:
        raise GoblinError(
            "--new and --session are mutually exclusive.",
            hint="Drop --new to resume, or drop --session to start fresh.",
        )
    if prompt is not None and session is not None:
        raise GoblinError(
            "--prompt requires a fresh session; --session resumes an existing one.",
            hint="Drop --session, or drop --prompt.",
        )
    if prompt is not None:
        new = True
    project_filter: str | None = None
    if project is not None:
        normalized = project.strip().lower()
        # Validate up-front so a typo raises ProjectNotFoundError instead of
        # silently falling through to the picker chain.
        state.get_project(normalized)
        project_filter = normalized
    task = resolve_task(target, project_filter)
    proj = state.get_project(task.project)
    cfg = config.load()
    agent_name = agent or cfg.defaults.agent or "claude"
    validate_agent_for_project(agent_name, proj)
    agent_obj = get_agent(agent_name)
    windowing_mode = windowing or cfg.defaults.windowing
    windower = get_windower(windowing_mode)
    unsafe_mode = cfg.defaults.unsafe if unsafe is None else unsafe

    # Decide which session to spawn.
    if session == SESSION_PICK_SENTINEL:
        refreshed_task = sessions.refresh_task_summaries(task)
        sessions.persist(proj, refreshed_task)
        sessions.schedule_descriptions(proj, refreshed_task)
        if not refreshed_task.sessions:
            raise GoblinError(
                f"No sessions on task {task.id!r} to pick from.",
                hint="Drop --session to start a fresh session, "
                "or pass an explicit id (`--session <id>`).",
            )
        picked = choose_session(refreshed_task.sessions)
        if isinstance(picked, CancelChoice):
            console.print("[muted]Cancelled.[/]")
            raise typer.Exit(code=1)
        if isinstance(picked, FreshChoice):
            choice = Fresh(prompt=build_seed_prompt(task))
        elif isinstance(picked, ResumeChoice):
            choice = Resume(session_id=picked.session_id)
            if picked.agent != agent_name:
                agent_name = picked.agent
                agent_obj = get_agent(agent_name)
        else:
            raise GoblinError("Unexpected picker result.")
        task = refreshed_task
    elif session:
        choice = Resume(session_id=session)
        for s in task.sessions:
            if s.session_id == session and s.agent != agent_name:
                agent_name = s.agent
                agent_obj = get_agent(agent_name)
                break
    elif new:
        choice = Fresh(prompt=build_seed_prompt(task, user_prompt=prompt))
    else:
        refreshed_task = sessions.refresh_task_summaries(task)
        sessions.persist(proj, refreshed_task)
        sessions.schedule_descriptions(proj, refreshed_task)
        if not refreshed_task.sessions:
            choice = Fresh(prompt=build_seed_prompt(task))
        else:
            picked = choose_session(refreshed_task.sessions)
            if isinstance(picked, CancelChoice):
                console.print("[muted]Cancelled.[/]")
                raise typer.Exit(code=1)
            if isinstance(picked, FreshChoice):
                choice = Fresh(prompt=build_seed_prompt(task))
            elif isinstance(picked, ResumeChoice):
                choice = Resume(session_id=picked.session_id)
                if picked.agent != agent_name:
                    agent_name = picked.agent
                    agent_obj = get_agent(agent_name)
            else:
                raise GoblinError("Unexpected picker result.")
        task = refreshed_task

    session_label = f"resume {choice.session_id}" if isinstance(choice, Resume) else "fresh"
    print_settings(
        [
            ("task", task.id),
            ("project", proj.name),
            ("session", session_label),
            ("agent", agent_name),
            ("windowing", windowing_mode),
            ("unsafe", str(unsafe_mode).lower()),
        ]
    )
    console.print(
        f"Launching {agent_badge(agent_name)} "
        f"({'resume' if isinstance(choice, Resume) else 'fresh'}) in [muted]{windowing_mode}[/]…"
    )
    exit_code, _ = launch_agent(
        project=proj,
        task=task,
        agent=agent_obj,
        choice=choice,
        windower=windower,
        unsafe=unsafe_mode,
    )
    if exit_code != 0:
        raise typer.Exit(code=exit_code)
