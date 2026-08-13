from __future__ import annotations

from datetime import UTC, datetime

import typer
from rich.tree import Tree

from goblin_watcher import config, description, git, github_state, sessions, state
from goblin_watcher.completion_enumerators import complete_projects
from goblin_watcher.console import console
from goblin_watcher.errors import GoblinError, ProjectNotFoundError
from goblin_watcher.linear_state import LinearStateFetcher
from goblin_watcher.models import Project, SessionRecord, Task
from goblin_watcher.sync import store


def _from_cache(proj: Project, task: Task) -> str | None:
    """Render indicators from the background-sync cache, or None if unusable.

    Entries older than twice the configured sync interval are treated as absent:
    if the scheduled job stopped firing, stale numbers are worse than a live
    recompute. The age is shown so a cached reading is never mistaken for a
    live one (ADR 0005).
    """
    cfg = config.load()
    entry = store.get_indicators(proj.name, task.id, max_age_seconds=cfg.sync.interval_seconds * 2)
    if entry is None:
        return None
    parts: list[str] = []
    if entry.uncommitted:
        parts.append("[hint]● uncommitted[/]")
    if entry.ahead > 0:
        label = "unpushed" if entry.ahead_vs_remote else "unmerged"
        parts.append(f"[hint]↑{entry.ahead} {label}[/]")
    if entry.checks == "failing":
        parts.append("[red]✗ checks[/]")
    elif entry.checks == "pending":
        parts.append("[hint]● checks running[/]")
    if not parts:
        return ""
    age = _fmt_relative(entry.computed_at).removesuffix(" ago")
    return "  " + "  ".join(parts) + f" [muted]({age})[/]"


def _sync_indicators(proj: Project, task: Task, use_cache: bool = True) -> str:
    """Markup snippet flagging unpushed/uncommitted work on `task`. '' when clean.

    Projects with a remote (`proj.repo_url`) get a "↑N unpushed" tally relative
    to `origin/<branch>` when that ref exists, or relative to the base branch
    when the branch was never pushed. Local-only projects get "↑N unmerged"
    relative to the base branch instead.

    Prefers the background-sync cache when one is fresh; falls back to computing
    live (2-3 git subprocesses per task) so behaviour is unchanged when sync was
    never installed.
    """
    if task.kind == "scratch" or not task.worktree_path.exists():
        return ""
    if use_cache:
        cached = _from_cache(proj, task)
        if cached is not None:
            return cached
    parts: list[str] = []
    try:
        if git.has_uncommitted_changes(task.worktree_path):
            parts.append("[hint]● uncommitted[/]")
    except GoblinError:
        pass
    try:
        if proj.repo_url:
            if git.remote_branch_exists(proj.root, task.branch):
                n = git.rev_list_count(proj.root, f"origin/{task.branch}..{task.branch}")
            else:
                n = git.rev_list_count(proj.root, f"{task.base_branch}..{task.branch}")
            if n > 0:
                parts.append(f"[hint]↑{n} unpushed[/]")
        else:
            n = git.rev_list_count(proj.root, f"{task.base_branch}..{task.branch}")
            if n > 0:
                parts.append(f"[hint]↑{n} unmerged[/]")
    except GoblinError:
        pass
    return ("  " + "  ".join(parts)) if parts else ""


_LINEAR_STATE_STYLES: dict[str, str] = {
    "backlog": "blue",
    "triage": "bright_red",
    "todo": "cyan",
    "to do": "cyan",
    "in progress": "bold yellow",
    "started": "bold yellow",
    "in review": "bold magenta",
    "in-review": "bold magenta",
    "review": "bold magenta",
    "ready": "bold cyan",
    "done": "bold green",
    "completed": "bold green",
    "merged": "bold green",
    "canceled": "strike dim",
    "cancelled": "strike dim",
    "duplicate": "strike dim",
}


def _linear_state_style(state: str) -> str:
    """Rich style for a Linear workflow-state name. Falls back to white."""
    return _LINEAR_STATE_STYLES.get(state.strip().lower(), "white")


def _ticket_suffix(task: Task) -> str:
    """The `· <title> (linear: state)` fragment for a task's tracking item. '' when none."""
    if task.linear:
        style = _linear_state_style(task.linear.state)
        return (
            f" · {task.linear.title}  [muted](linear:[/] [{style}]{task.linear.state}[/][muted])[/]"
        )
    if task.github_issue:
        issue = task.github_issue
        state_name = issue.state.lower()
        style = "bold green" if state_name == "closed" else "cyan"
        return f" · {issue.title}  [muted]([/][{style}]{issue.reference} {state_name}[/][muted])[/]"
    return ""


def _stack_order(tasks: list[Task]) -> list[Task]:
    """`tasks` reordered so every parent comes before its children.

    Rendering nests a child under its parent's tree node, which only exists once
    the parent has been visited. A task whose `parent_task` isn't in this
    project — pruned, or hand-edited into a cycle — comes out as a root, so
    nothing is ever dropped from the tree.
    """
    ids = {t.id for t in tasks}
    pending = list(tasks)
    ordered: list[Task] = []
    placed: set[str] = set()
    while pending:
        deferred: list[Task] = []
        for t in pending:
            parent = t.parent_task
            if parent is None or parent not in ids or parent in placed:
                ordered.append(t)
                placed.add(t.id)
            else:
                deferred.append(t)
        if len(deferred) == len(pending):
            # Nothing moved, so the remainder is a parent cycle. Emit it flat
            # rather than looping forever.
            ordered.extend(deferred)
            break
        pending = deferred
    return ordered


def _stack_suffix(task: Task, by_id: dict[str, Task]) -> str:
    """Markup for this task's place in a stack. '' for an unstacked task.

    A child sitting under a live parent needs no words — the indentation says
    it. What it does need is a nudge once the parent lands, because the base
    branch it was cut from is now history and the diff won't shrink until it's
    rebased. A parent that's no longer tracked (pruned after merging) renders
    flat, so it cites the id instead of silently losing the link.
    """
    parent_id = task.parent_task
    if parent_id is None:
        return ""
    parent = by_id.get(parent_id)
    if parent is None:
        return f"  [muted](stacked on {parent_id}, no longer tracked)[/]"
    if parent.status in {"merged", "closed"}:
        return f"  [hint]⤴ restack: {parent_id} {parent.status}[/]"
    return ""


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


def _activity_badge(s: SessionRecord) -> str:
    """Markup snippet showing whether a session's agent is producing output.

    Derived from the transcript file's mtime: modified within
    `defaults.activity_active_seconds` → `● active`; older → `idle <age>`;
    no transcript on disk → '' (nothing to infer from).
    """
    mtime = description.transcript_mtime(s)
    if mtime is None:
        return ""
    try:
        threshold = int(config.load().defaults.activity_active_seconds)
    except Exception:
        threshold = 120
    age = (datetime.now(UTC) - mtime).total_seconds()
    if age <= threshold:
        return " · [bold green]● active[/]"
    return f" · idle {_fmt_relative(mtime).removesuffix(' ago')}"


def status(
    project: str | None = typer.Option(
        None,
        "--project",
        help="Limit to a single project.",
        autocompletion=complete_projects,
    ),
    no_linear: bool = typer.Option(
        False,
        "--no-linear",
        "--no-tickets",
        help="Skip upstream ticket-state refresh — Linear and GitHub issues both "
        "(no network; cached states still shown).",
    ),
    no_cache: bool = typer.Option(
        False,
        "--no-cache",
        help="Recompute git indicators live instead of reading the `gw sync` cache.",
    ),
) -> None:
    """Tree view: projects → tasks → sessions, with agent badges."""
    global_state = state.load_global()
    if not global_state.projects:
        console.print(
            "[muted]No projects registered. Try `gw project new` or `gw <LINEAR-ID> --repo ...`.[/]"
        )
        return

    if project is not None:
        # Validate up-front so a typo raises ProjectNotFoundError instead of
        # silently rendering an empty tree.
        state.get_project(project.strip().lower(), global_state)
        names = [project.strip().lower()]
    else:
        names = sorted(global_state.projects)

    root = Tree("gw")
    linear = LinearStateFetcher()

    try:
        for name in names:
            try:
                proj = state.get_project(name)
            except ProjectNotFoundError:
                root.add(f"[red]{name}[/] [muted](project metadata missing)[/]")
                continue
            proj_node = root.add(f"[bold]{proj.name}[/]")

            tasks = state.list_tasks(proj)
            if not tasks:
                proj_node.add("[muted](no tasks)[/]")
                continue

            # Stacked tasks nest under their parent so a four-deep chain reads
            # as one chain instead of four unrelated tasks (gh-20). Statuses come
            # from this snapshot: the refreshes below touch ticket state, not
            # `Task.status`, so a parent's is the same before and after its turn.
            by_id = {t.id: t for t in tasks}
            nodes: dict[str, Tree] = {}

            for task in _stack_order(tasks):
                if not no_linear:
                    task = linear.refresh(proj, task)
                    task = github_state.refresh(proj, task)
                # Discovery walks each agent's on-disk session store, so it runs
                # outside the task lock; only the resulting plan is applied under
                # it (ADR 0004).
                plan = sessions.plan_reconciliation(task)
                if not plan.is_empty:
                    task = sessions.persist_refresh(proj, task, plan)
                sync_suffix = _sync_indicators(proj, task, use_cache=not no_cache)
                task_label = (
                    f"[bold]{task.id}[/]"
                    + _ticket_suffix(task)
                    + f"  [muted][{task.status}][/]"
                    + sync_suffix
                    + _stack_suffix(task, by_id)
                )
                # Task ids are non-empty slugs, so "" can never match one.
                task_node = nodes.get(task.parent_task or "", proj_node).add(task_label)
                nodes[task.id] = task_node
                if not task.sessions:
                    task_node.add("[muted](no sessions yet)[/]")
                    continue
                # Lazy refresh stale summaries on the way through.
                refreshed = sessions.persist_refresh(proj, sessions.refresh_task_summaries(task))
                sessions.schedule_descriptions(proj, refreshed)
                for s in refreshed.sessions:
                    summary = description.wrap_for_tree(
                        description.display_text(s), indent_cols=8, width=72
                    )
                    turns = f"{s.turn_count} turns" if s.turn_count else "0 turns"
                    task_node.add(
                        f"[agent.{s.agent}]{s.agent}[/]  {summary}  "
                        f"[muted]{turns} · {_fmt_relative(s.last_used_at)}"
                        f"{_activity_badge(s)}[/]"
                    )
    finally:
        linear.close()
    console.print(root)
