from __future__ import annotations

from datetime import UTC, datetime, timedelta

import typer
from rich.table import Table

from goblin_watcher import description, git, sessions, state
from goblin_watcher.completion_enumerators import (
    complete_projects,
    complete_sessions,
    complete_tasks,
)
from goblin_watcher.console import console, print_success
from goblin_watcher.errors import GoblinError, ProjectNotFoundError
from goblin_watcher.models import Project, SessionRecord, Task

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


@app.command("show")
def show(
    session_id: str = typer.Argument(..., help="Session id.", autocompletion=complete_sessions),
) -> None:
    """Show a session's full details."""
    proj, task, s = _find_session(session_id)
    s = sessions.refresh_if_stale(task, s)
    sessions.persist(proj, sessions.upsert(task, s))
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
        sessions.persist(proj, sessions.upsert(task, refreshed))
        print_success(f"Refreshed {session_id!r}")
        return

    count = 0
    for proj, task, _s in _iter_all_sessions():
        refreshed_task = sessions.refresh_task_summaries(task)
        sessions.persist(proj, refreshed_task)
        sessions.schedule_descriptions(proj, refreshed_task)
        count += len(task.sessions)
    print_success(f"Refreshed {count} session(s)")


@app.command("rm")
def rm(
    session_id: str = typer.Argument(..., help="Session id.", autocompletion=complete_sessions),
) -> None:
    """Forget a session from gw's record. The agent's own session store is untouched."""
    proj, task, s = _find_session(session_id)
    new_sessions = [x for x in task.sessions if x.session_id != s.session_id]
    updated = task.model_copy(update={"sessions": new_sessions})
    state.save_task(proj, updated)
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
        t = state.load_task(proj, task_id)
        new_sessions = [x for x in t.sessions if x.session_id not in ids]
        state.save_task(proj, t.model_copy(update={"sessions": new_sessions}))
        removed += len(t.sessions) - len(new_sessions)
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
