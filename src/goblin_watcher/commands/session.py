from __future__ import annotations

import sys
from datetime import UTC, datetime, timedelta

import click
import typer
from rich.table import Table

from goblin_watcher import config, description, git, sessions, state, usage
from goblin_watcher.agents import get_agent
from goblin_watcher.completion_enumerators import (
    complete_projects,
    complete_sessions,
    complete_tasks,
)
from goblin_watcher.console import console, print_success
from goblin_watcher.errors import GoblinError, ProjectNotFoundError
from goblin_watcher.models import Project, SessionRecord, Task
from goblin_watcher.task_resolver import resolve_task
from goblin_watcher.windowing import WINDOWING_MODES, get_windower

app = typer.Typer()


def _fmt_relative(ts: datetime) -> str:
    if ts.tzinfo is None:
        ts = ts.replace(tzinfo=UTC)
    seconds = int((datetime.now(UTC) - ts).total_seconds())
    if seconds < 60:
        return "just now"
    if seconds < 3600:
        return f"{seconds // 60}m ago"
    if seconds < 86400:
        return f"{seconds // 3600}h ago"
    return f"{seconds // 86400}d ago"


def _iter_all_sessions() -> list[tuple[Project, Task, SessionRecord]]:
    rows: list[tuple[Project, Task, SessionRecord]] = []
    for name in state.load_global().projects:
        try:
            proj = state.get_project(name)
        except ProjectNotFoundError:
            continue
        for task in state.list_tasks(proj):
            for s in task.sessions:
                rows.append((proj, task, s))
    return rows


def _find_session(session_id: str) -> tuple[Project, Task, SessionRecord]:
    for proj, task, s in _iter_all_sessions():
        if s.session_id == session_id:
            return proj, task, s
    raise GoblinError(f"No session with id {session_id!r} on any task.")


@app.command("ls")
def ls(
    task: str | None = typer.Option(
        None, "--task", help="Limit to a single task.", autocompletion=complete_tasks
    ),
    project: str | None = typer.Option(
        None,
        "--project",
        help="Limit to a single project.",
        autocompletion=complete_projects,
    ),
    agent: str | None = typer.Option(None, "--agent", help="Limit to a single agent."),
) -> None:
    """List sessions across projects."""
    rows = _iter_all_sessions()
    if project:
        normalized = project.strip().lower()
        # Validate up-front so a typo raises ProjectNotFoundError instead of
        # silently returning an empty list.
        state.get_project(normalized)
        rows = [r for r in rows if r[0].name == normalized]
    if task:
        rows = [r for r in rows if r[1].id == task]
    if agent:
        rows = [r for r in rows if r[2].agent == agent]
    if not rows:
        console.print("[muted]No sessions match the given filters.[/]")
        return

    table = Table(title="Sessions", show_header=True, header_style="bold")
    table.add_column("Project")
    table.add_column("Task")
    table.add_column("Agent")
    table.add_column("Summary")
    table.add_column("Turns", justify="right")
    table.add_column("Last used")
    table.add_column("Session id", overflow="fold")

    for proj, task_obj, s in rows:
        # Refresh stale summaries on the way through.
        s = sessions.refresh_if_stale(task_obj, s)
        description.schedule_if_stale(proj, task_obj, s)
        table.add_row(
            proj.name,
            task_obj.id,
            f"[agent.{s.agent}]{s.agent}[/]",
            description.display_text(s),
            str(s.turn_count),
            _fmt_relative(s.last_used_at),
            s.session_id,
        )
    console.print(table)


@app.command("send")
def send(
    target: str = typer.Argument(
        ...,
        help="Task id, or a path inside its worktree ('.' for cwd).",
        autocompletion=complete_tasks,
    ),
    message: str = typer.Argument(..., help="Text to type into the running agent."),
    session: str | None = typer.Option(
        None,
        "--session",
        help="Which session's pane to send to. Required when a task has several live panes.",
        autocompletion=complete_sessions,
    ),
    project: str | None = typer.Option(
        None,
        "--project",
        help="Limit task lookup to a single project.",
        autocompletion=complete_projects,
    ),
    windowing: str | None = typer.Option(
        None,
        "--windowing",
        help="Overrides config.",
        click_type=click.Choice(list(WINDOWING_MODES)),
    ),
    enter: bool = typer.Option(
        True,
        "--enter/--no-enter",
        help="Submit the text. --no-enter leaves it in the agent's input box.",
    ),
) -> None:
    """Send input to a running agent session, as if typed at its keyboard.

    The point is supervising several agents at once: adding "also fix the
    tests" to a task in flight shouldn't require attaching to its pane.

    Needs a windower with an addressable pane (tmux). With one live pane on the
    task the session is unambiguous; with several, name one with `--session`.
    """
    if not message and not enter:
        raise GoblinError(
            "Nothing to send: empty message with --no-enter.",
            hint="Pass some text, or drop --no-enter to just press Enter.",
        )
    project_filter: str | None = None
    if project is not None:
        normalized = project.strip().lower()
        # Validate up-front so a typo raises ProjectNotFoundError instead of
        # silently falling through to the picker chain.
        state.get_project(normalized)
        project_filter = normalized
    task = resolve_task(target, project_filter)
    if session is not None and not any(s.session_id == session for s in task.sessions):
        raise GoblinError(
            f"Session {session!r} is not on task {task.id!r}.",
            hint=f"Run `gw session ls --task {task.id}` to see its sessions.",
        )
    windowing_mode = windowing or config.load().defaults.windowing
    where = get_windower(windowing_mode).send(
        task=task, text=message, session_id=session, enter=enter
    )
    print_success(f"Sent to {task.id} [muted]({where})[/]")


@app.command("show")
def show(
    session_id: str = typer.Argument(..., help="Session id.", autocompletion=complete_sessions),
) -> None:
    """Show a session's full details."""
    proj, task, s = _find_session(session_id)
    s = sessions.refresh_if_stale(task, s)
    sessions.persist_refresh(proj, sessions.upsert(task, s))
    description.schedule_if_stale(proj, task, s)

    console.print(f"[bold]{s.session_id}[/]")
    console.print(f"  agent          [agent.{s.agent}]{s.agent}[/]")
    console.print(f"  project        {proj.name}")
    console.print(f"  task           {task.id}")
    console.print(f"  branch         {task.branch}")
    desc_text = s.description or "(none yet)"
    desc_block = description.wrap_for_tree(desc_text, indent_cols=17, width=80)
    console.print(f"  description    {desc_block}")
    console.print(f"  summary        {s.summary or s.label or '(none yet)'}")
    console.print(f"  turns          {s.turn_count}")
    console.print(f"  created_at     {s.created_at.isoformat()}")
    console.print(f"  last_used_at   {s.last_used_at.isoformat()}")
    console.print(f"  transcript     {s.transcript_path or '(unknown)'}")
    _print_usage(s)


def _print_usage(s: SessionRecord) -> None:
    """Token and cost lines, when the agent's transcript carried usage.

    Agents whose transcripts gw can't read (gemini, antigravity) print a
    placeholder rather than a misleading zero.
    """
    rollup = usage.for_session(s)
    if rollup.is_empty:
        console.print("  tokens         [muted](none recorded)[/]")
        return
    console.print(f"  tokens         {usage.fmt_tokens_line(rollup)}")
    models = sorted({b.model for b in s.usage if b.model})
    model_note = f"  [muted]({', '.join(models)})[/]" if models else ""
    console.print(f"  cost           {usage.fmt_cost(rollup)}{model_note}")
    note = usage.unpriced_note(rollup)
    if note:
        console.print(f"  [muted]{note}[/]")


@app.command("refresh")
def refresh(
    session_id: str | None = typer.Argument(
        None,
        help="Session id; omit to refresh every session.",
        autocompletion=complete_sessions,
    ),
) -> None:
    """Refresh one session, or all sessions if no id is given."""
    if session_id:
        proj, task, s = _find_session(session_id)
        refreshed = sessions.refresh_summary(task, s)
        sessions.persist_refresh(proj, sessions.upsert(task, refreshed))
        print_success(f"Refreshed {session_id!r}")
        return

    # Deduplicate to distinct tasks: `_iter_all_sessions` yields one row per
    # session, but each refresh pass covers the whole task.
    seen_tasks: set[tuple[str, str]] = set()
    count = 0
    for proj, task, _s in _iter_all_sessions():
        key = (proj.name, task.id)
        if key in seen_tasks:
            continue
        seen_tasks.add(key)
        refreshed_task = sessions.persist_refresh(proj, sessions.refresh_task_summaries(task))
        sessions.schedule_descriptions(proj, refreshed_task)
        count += len(task.sessions)
    print_success(f"Refreshed {count} session(s)")


@app.command("transcript")
def transcript(
    session_id: str = typer.Argument(..., help="Session id.", autocompletion=complete_sessions),
    raw: bool = typer.Option(
        False, "--raw", help="Print the transcript file path instead of rendered text."
    ),
) -> None:
    """Print a session's transcript as labeled [user] / [assistant] blocks.

    `--raw` prints the on-disk transcript path (e.g. the claude jsonl) for
    tools that want the source file. Output goes to stdout unstyled so it
    pipes cleanly.
    """
    _proj, task, s = _find_session(session_id)
    if raw:
        path = s.transcript_path
        if path is None:
            raise GoblinError(
                f"No transcript path recorded for session {session_id!r}.",
                hint="Run `gw session refresh` first, or drop --raw for rendered text.",
            )
        sys.stdout.write(f"{path}\n")
        return
    agent = get_agent(s.agent)
    rendered = agent.render_transcript(s.session_id, task.agent_cwd)
    if rendered is None:
        raise GoblinError(
            f"No transcript available for session {session_id!r}.",
            hint="The agent may not persist transcripts (gemini), or the file is gone.",
        )
    sys.stdout.write(rendered + "\n")


@app.command("rm")
def rm(
    session_id: str = typer.Argument(..., help="Session id.", autocompletion=complete_sessions),
) -> None:
    """Forget a session from gw's record. The agent's own session store is untouched."""
    proj, task, s = _find_session(session_id)
    # Remove under the task lock (ADR 0004) so a session added concurrently —
    # e.g. an agent the launcher just registered — isn't silently deleted.
    state.update_task(
        proj,
        task.id,
        lambda latest: latest.model_copy(
            update={"sessions": [x for x in latest.sessions if x.session_id != s.session_id]}
        ),
    )
    print_success(f"Removed session {session_id!r} from {task.id}")


@app.command("prune")
def prune(
    older_than: int = typer.Option(
        30,
        "--older-than",
        help="Remove sessions whose last_used_at is more than N days ago (default 30).",
    ),
    project: str | None = typer.Option(
        None,
        "--project",
        help="Limit to a single project.",
        autocompletion=complete_projects,
    ),
    task: str | None = typer.Option(
        None, "--task", help="Limit to a single task.", autocompletion=complete_tasks
    ),
    agent: str | None = typer.Option(None, "--agent", help="Limit to a single agent."),
    include_dirty: bool = typer.Option(
        False,
        "--include-dirty",
        help="Also prune sessions whose task worktree has uncommitted changes.",
    ),
    dry_run: bool = typer.Option(
        False, "--dry-run", help="Print what would be removed; do nothing."
    ),
    force: bool = typer.Option(False, "--force", help="Skip the confirmation prompt."),
) -> None:
    """Forget sessions older than N days. The agents' transcripts on disk are untouched.

    By default, sessions whose task worktree has uncommitted changes are preserved
    so an in-flight conversation isn't dropped from `gw`'s view while its diff is
    still on disk. Pass `--include-dirty` to prune those too.
    """
    if older_than < 0:
        raise GoblinError("--older-than must be non-negative.")
    cutoff = datetime.now(UTC) - timedelta(days=older_than)

    rows = _iter_all_sessions()
    if project:
        normalized = project.strip().lower()
        # Validate up-front so a typo raises ProjectNotFoundError instead of
        # silently returning an empty list.
        state.get_project(normalized)
        rows = [r for r in rows if r[0].name == normalized]
    if task:
        rows = [r for r in rows if r[1].id == task]
    if agent:
        rows = [r for r in rows if r[2].agent == agent]
    stale = [(p, t, s) for p, t, s in rows if _last_used(s) < cutoff]

    skipped_dirty = 0
    if not include_dirty:
        # Cache the dirty check per task so a task with many old sessions only
        # shells out once.
        dirty_cache: dict[tuple[str, str], bool] = {}

        def _is_dirty(p: Project, t: Task) -> bool:
            key = (p.name, t.id)
            if key not in dirty_cache:
                dirty_cache[key] = _task_worktree_dirty(t)
            return dirty_cache[key]

        kept: list[tuple[Project, Task, SessionRecord]] = []
        for p, t, s in stale:
            if _is_dirty(p, t):
                skipped_dirty += 1
            else:
                kept.append((p, t, s))
        stale = kept

    if not stale:
        if skipped_dirty:
            console.print(
                f"[muted]No prunable sessions older than {older_than}d "
                f"({skipped_dirty} skipped: uncommitted changes).[/]"
            )
        else:
            console.print(f"[muted]No sessions older than {older_than}d.[/]")
        return

    table = Table(
        title=f"Sessions older than {older_than}d{' (dry run)' if dry_run else ''}",
        show_header=True,
        header_style="bold",
    )
    table.add_column("Project")
    table.add_column("Task")
    table.add_column("Agent")
    table.add_column("Last used")
    table.add_column("Session id", overflow="fold")
    for p, t, s in stale:
        table.add_row(p.name, t.id, s.agent, _fmt_relative(s.last_used_at), s.session_id)
    console.print(table)
    if skipped_dirty:
        console.print(
            f"[muted]Skipped {skipped_dirty} session(s) on tasks with uncommitted changes "
            f"(pass --include-dirty to include).[/]"
        )

    if dry_run:
        return
    if not force and not typer.confirm(f"Forget {len(stale)} session(s)?", default=False):
        console.print("[muted]Cancelled.[/]")
        raise typer.Exit(code=1)

    # Group by task to write each task file once.
    by_task: dict[tuple[str, str], set[str]] = {}
    for p, t, s in stale:
        by_task.setdefault((p.name, t.id), set()).add(s.session_id)

    removed = 0
    for (proj_name, task_id), ids in by_task.items():
        proj = state.get_project(proj_name)
        before = 0

        def _drop(latest: Task, ids: set[str] = ids) -> Task:
            nonlocal before
            before = len(latest.sessions)
            return latest.model_copy(
                update={"sessions": [x for x in latest.sessions if x.session_id not in ids]}
            )

        after = state.update_task(proj, task_id, _drop)
        removed += before - len(after.sessions)
    print_success(f"Forgot {removed} session(s)")


def _task_worktree_dirty(task: Task) -> bool:
    """True if `task`'s worktree exists and has uncommitted changes.

    A missing worktree counts as clean: there's no in-flight diff to protect,
    so any lingering session records for that task are fair game to prune.
    """
    if not task.worktree_path.exists():
        return False
    try:
        return git.has_uncommitted_changes(task.worktree_path)
    except GoblinError:
        # Conservative: if we can't inspect, assume dirty so we don't drop
        # records for a task with in-progress work we can't see.
        return True


def _last_used(s: SessionRecord) -> datetime:
    ts = s.last_used_at
    return ts if ts.tzinfo else ts.replace(tzinfo=UTC)
