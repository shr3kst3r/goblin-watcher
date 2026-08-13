from __future__ import annotations

import click
import typer

from goblin_watcher import config, review_feed, sessions, state
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
from goblin_watcher.windowing import WINDOWING_MODES, get_windower


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
        help="Where the agent runs. Overrides config. 'headless' detaches an "
        "unattended print-mode run and logs it to <project>/.goblin/logs/.",
        click_type=click.Choice(list(WINDOWING_MODES)),
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
    adversarial_review: bool = typer.Option(
        False,
        "--adversarial-review",
        help="Start a fresh session seeded with `/codex:adversarial-review`. "
        "Forces --agent claude.",
    ),
    research: bool = typer.Option(
        False,
        "--research",
        help="Start a fresh session seeded with a read-only research brief on the "
        "task's ticket: investigate and report findings in the session, don't "
        "implement, don't touch GitHub/Linear/Slack. Needs a Linear ticket or "
        "GitHub issue on the task.",
    ),
    address_review: bool = typer.Option(
        False,
        "--address-review",
        help="Start a fresh session seeded with the task's PR feedback: its "
        "unresolved review threads and the output of any failing checks, with a "
        "brief to work through them. Needs an open PR on the task.",
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
    # Hoisted above every mode block: when the caller asked for two modes at
    # once, that conflict is the operative error. Reporting --adversarial-review's
    # own checks first (--prompt, --agent) would send the user off changing a
    # flag that --research accepts perfectly well.
    modes = [
        name
        for name, enabled in (
            ("--research", research),
            ("--adversarial-review", adversarial_review),
            ("--address-review", address_review),
        )
        if enabled
    ]
    if len(modes) > 1:
        raise GoblinError(
            f"{' and '.join(modes)} are mutually exclusive.",
            hint="Pass one or the other." if len(modes) == 2 else "Pass exactly one.",
        )
    if adversarial_review:
        if session is not None:
            raise GoblinError(
                "--adversarial-review and --session are mutually exclusive "
                "(it always starts a fresh session).",
                hint="Drop --session.",
            )
        if prompt is not None:
            raise GoblinError(
                "--adversarial-review and --prompt are mutually exclusive.",
                hint="Pass one or the other.",
            )
        if agent is not None and agent != "claude":
            raise GoblinError(
                "--adversarial-review requires --agent claude "
                "(the skill is a Claude Code slash command).",
                hint=f"Drop --agent {agent}, or drop --adversarial-review.",
            )
        agent = "claude"
        new = True
    if research or address_review:
        # Covers `--session <id>` and the bare-`--session` picker sentinel alike,
        # so neither mode can reach a picker branch below.
        flag = "--research" if research else "--address-review"
        if session is not None:
            raise GoblinError(
                f"{flag} and --session are mutually exclusive (it always starts a fresh session).",
                hint="Drop --session.",
            )
        new = True
    project_filter: str | None = None
    if project is not None:
        normalized = project.strip().lower()
        # Validate up-front so a typo raises ProjectNotFoundError instead of
        # silently falling through to the picker chain.
        state.get_project(normalized)
        project_filter = normalized
    task = resolve_task(target, project_filter)
    # Needs the resolved task: only now do we know whether there's anything to
    # research (ADR 0006). Scratch tasks carry neither and land here too.
    if research and task.linear is None and task.github_issue is None:
        raise GoblinError(
            f"Task {task.id!r} has no Linear ticket or GitHub issue to research.",
            hint="--research needs a task created from --linear or --issue.",
        )
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
        # Re-bind/drop records whose transcript vanished (or never existed —
        # old tmux spawns stored placeholder ids) before offering them.
        plan = sessions.plan_reconciliation(task)
        refreshed_task = sessions.refresh_task_summaries(sessions.apply_reconciliation(task, plan))
        refreshed_task = sessions.persist_refresh(proj, refreshed_task, plan)
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
        # `/codex:adversarial-review` must be the entire user message — Claude
        # Code's slash-command parser ignores it if buried in the seed template.
        # `--wait` runs the review in the foreground.
        if adversarial_review:
            choice = Fresh(prompt="/codex:adversarial-review --wait")
        elif address_review:
            # Deliberately the last thing before dispatch: this is the only seed
            # path that hits the network, and every cheap validation above should
            # have had its chance to fail first.
            console.print("[muted]Reading the PR's review feedback…[/]")
            choice = Fresh(
                prompt=build_seed_prompt(task, user_prompt=prompt, review=review_feed.collect(task))
            )
        else:
            choice = Fresh(prompt=build_seed_prompt(task, user_prompt=prompt, research=research))
    else:
        plan = sessions.plan_reconciliation(task)
        refreshed_task = sessions.refresh_task_summaries(sessions.apply_reconciliation(task, plan))
        refreshed_task = sessions.persist_refresh(proj, refreshed_task, plan)
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
