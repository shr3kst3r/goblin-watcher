from __future__ import annotations

import time
from datetime import UTC, datetime

import typer
from rich.console import Group, RenderableType
from rich.live import Live
from rich.tree import Tree

from goblin_watcher import config, description, git, github_state, sessions, state, usage
from goblin_watcher.completion_enumerators import complete_projects
from goblin_watcher.console import console
from goblin_watcher.errors import GoblinError, ProjectNotFoundError
from goblin_watcher.linear_state import LinearStateFetcher
from goblin_watcher.models import Project, SessionRecord, Task
from goblin_watcher.sync import store
from goblin_watcher.windowing import headless


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


def _headless_live(task: Task) -> bool:
    """Whether a detached `--windowing headless` run for this task is still alive.

    A pid-file check (`os.kill(pid, 0)`), so it stays honest for an agent that
    writes nothing to its transcript for minutes at a stretch — the case the
    mtime-derived activity badge is blind to.
    """
    try:
        return headless.has_live_run(task)
    except GoblinError:
        return False


def _in_flight(task: Task, *, grace_seconds: float, headless_live: bool) -> bool:
    """Whether `task` has work actually going on right now.

    Two independent signals, either of which is enough:

    * a session whose transcript was touched within `grace_seconds`. The window
      is wider than the `● active` threshold on purpose — an agent that stops to
      ask a question goes quiet in two minutes, and that's precisely when you
      want it still on screen;
    * a live detached process from a headless run, which has no terminal and may
      go a long time between transcript writes.

    A task with no sessions and no live pid has nothing in flight to watch.
    """
    if headless_live:
        return True
    now = datetime.now(UTC)
    return any(
        (mtime := description.transcript_mtime(s)) is not None
        and (now - mtime).total_seconds() <= grace_seconds
        for s in task.sessions
    )


def _build_tree(
    names: list[str],
    *,
    cfg: config.Config,
    linear: LinearStateFetcher | None,
    no_linear: bool,
    no_cache: bool,
    cost: bool,
    active_only: bool,
    live: bool,
) -> tuple[Tree, usage.Rollup, int]:
    """Render the project → task → session tree. Returns (tree, cost total, tasks shown).

    `live` is the watch-mode budget: no network (Linear and GitHub issue state
    are read from their caches), no LLM description spawns, no session
    reconciliation, and a state write only when a summary refresh actually
    changed something. Those are the parts that cost a round-trip or a
    subprocess; a watch loop ticking every couple of seconds can't afford them,
    and `gw sync` is already doing all four in the background (ADR 0005).
    """
    root = Tree("gw")
    grand_total = usage.EMPTY
    shown = 0
    grace = float(cfg.defaults.activity_grace_seconds)

    for name in names:
        try:
            proj = state.get_project(name)
        except ProjectNotFoundError:
            root.add(f"[red]{name}[/] [muted](project metadata missing)[/]")
            continue
        # Built detached so a project with nothing in flight can be dropped
        # entirely under `--active` rather than rendering an empty heading.
        proj_node = Tree(f"[bold]{proj.name}[/]")
        project_total = usage.EMPTY
        project_shown = 0

        tasks = state.list_tasks(proj)
        if not tasks:
            if not active_only:
                proj_node.add("[muted](no tasks)[/]")
                root.children.append(proj_node)
            continue

        # Stacked tasks nest under their parent so a four-deep chain reads
        # as one chain instead of four unrelated tasks (gh-20). Statuses come
        # from this snapshot: the refreshes below touch ticket state, not
        # `Task.status`, so a parent's is the same before and after its turn.
        by_id = {t.id: t for t in tasks}
        nodes: dict[str, Tree] = {}

        for task in _stack_order(tasks):
            if not live:
                # Discovery walks each agent's on-disk session store, so it runs
                # outside the task lock; only the resulting plan is applied under
                # it (ADR 0004).
                plan = sessions.plan_reconciliation(task)
                if not plan.is_empty:
                    task = sessions.persist_refresh(proj, task, plan)
            headless_live = _headless_live(task)
            # Filter before the network refresh, not after: skipping a quiet
            # task's Linear round-trip is most of what makes `--active` fast.
            if active_only and not _in_flight(
                task, grace_seconds=grace, headless_live=headless_live
            ):
                continue
            if not no_linear and linear is not None:
                task = linear.refresh(proj, task)
                task = github_state.refresh(proj, task)
            # Lazy refresh stale summaries (and the token counts read off
            # the same transcript pass) before anything is rendered — the
            # task's cost line is a sum over its refreshed sessions.
            refreshed = task
            if task.sessions:
                candidate = sessions.refresh_task_summaries(task)
                # In watch mode only write when the refresh moved something;
                # otherwise every tick would take the task lock and rewrite
                # identical JSON.
                if not live or candidate.sessions != task.sessions:
                    refreshed = sessions.persist_refresh(proj, candidate)
                else:
                    refreshed = candidate
                if not live:
                    sessions.schedule_descriptions(proj, refreshed)
            task_total = usage.for_task(refreshed, cfg) if cost else usage.EMPTY
            project_total = project_total + task_total
            sync_suffix = _sync_indicators(proj, refreshed, use_cache=not no_cache)
            task_label = (
                f"[bold]{refreshed.id}[/]"
                + _ticket_suffix(refreshed)
                # Parens, not brackets: Rich reads `[open]` as a markup tag
                # and swallows it, so bracketed statuses never rendered.
                + f"  [muted]({refreshed.status})[/]"
                + ("  [bold cyan]⚡ headless[/]" if headless_live else "")
                + sync_suffix
                + _stack_suffix(refreshed, by_id)
                + (usage.badge(task_total) if cost else "")
            )
            # Archived: worktree dropped, record and branch kept (gh-23). Dimmed
            # so it reads as parked rather than live.
            if refreshed.archived:
                task_label = f"[dim]{task_label}  (archived)[/]"
            # Task ids are non-empty slugs, so "" can never match one. Under
            # `--active` a filtered-out parent leaves no node, so its child
            # falls back to the project and renders flat.
            task_node = nodes.get(refreshed.parent_task or "", proj_node).add(task_label)
            nodes[refreshed.id] = task_node
            project_shown += 1
            if not refreshed.sessions:
                task_node.add("[muted](no sessions yet)[/]")
                continue
            for s in refreshed.sessions:
                summary = description.wrap_for_tree(
                    description.display_text(s), indent_cols=8, width=72
                )
                turns = f"{s.turn_count} turns" if s.turn_count else "0 turns"
                cost_suffix = usage.compact_badge(usage.for_session(s, cfg)) if cost else ""
                task_node.add(
                    f"[agent.{s.agent}]{s.agent}[/]  {summary}  "
                    f"[muted]{turns} · {_fmt_relative(s.last_used_at)}"
                    f"{_activity_badge(s)}" + (f" · {cost_suffix}" if cost_suffix else "") + "[/]"
                )
        if active_only and project_shown == 0:
            continue
        if cost:
            proj_node.label = f"[bold]{proj.name}[/]{usage.badge(project_total)}"
            grand_total = grand_total + project_total
        root.children.append(proj_node)
        shown += project_shown

    return root, grand_total, shown


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
    cost: bool = typer.Option(
        False,
        "--cost",
        help="Annotate every session, task, and project with token usage and estimated cost.",
    ),
    active: bool = typer.Option(
        False,
        "--active",
        help="Only tasks with work in flight: a session whose transcript moved recently "
        "(defaults.activity_grace_seconds, default 900) or a live headless run.",
    ),
    watch: bool = typer.Option(
        False,
        "--watch",
        "-w",
        help="Re-render continuously instead of printing once. Ctrl-C to stop.",
    ),
    interval: float = typer.Option(
        2.0,
        "--interval",
        help="Seconds between redraws in --watch mode.",
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

    cfg = config.load()

    if watch:
        if interval < _MIN_WATCH_INTERVAL:
            raise GoblinError(
                f"--interval must be at least {_MIN_WATCH_INTERVAL}s.",
                hint="Each tick stats every tracked transcript; polling faster than that "
                "buys nothing but I/O.",
                exit_code=2,
            )
        _watch(
            names,
            cfg=cfg,
            no_cache=no_cache,
            cost=cost,
            active_only=active,
            interval=interval,
        )
        return

    linear = LinearStateFetcher()
    try:
        root, grand_total, shown = _build_tree(
            names,
            cfg=cfg,
            linear=linear,
            no_linear=no_linear,
            no_cache=no_cache,
            cost=cost,
            active_only=active,
            live=False,
        )
    finally:
        linear.close()
    if active and shown == 0:
        console.print(_nothing_in_flight(cfg))
        return
    console.print(root)
    if cost:
        _print_cost_total(grand_total)


# Below this a tick costs more in stat() calls than it returns in freshness.
_MIN_WATCH_INTERVAL = 0.5


def _nothing_in_flight(cfg: config.Config) -> str:
    minutes = max(1, int(cfg.defaults.activity_grace_seconds // 60))
    return (
        f"[muted]Nothing in flight — no session has written a transcript in the last "
        f"{minutes}m and no headless run is alive. Drop `--active` for the full tree.[/]"
    )


def _watch_frame(
    root: Tree,
    total: usage.Rollup,
    shown: int,
    *,
    cfg: config.Config,
    cost: bool,
    active_only: bool,
) -> RenderableType:
    """One frame of the live dashboard: the tree, plus a footer that proves it's ticking."""
    parts: list[RenderableType] = []
    if active_only and shown == 0:
        parts.append(_nothing_in_flight(cfg))
    else:
        parts.append(root)
        if cost:
            parts.extend(_cost_total_lines(total))
    scope = "in flight" if active_only else "tracked"
    stamp = datetime.now().strftime("%H:%M:%S")
    parts.append(
        f"[muted]watching · {shown} task(s) {scope} · {stamp} · Ctrl-C to stop[/]",
    )
    return Group(*parts)


def _watch(
    names: list[str],
    *,
    cfg: config.Config,
    no_cache: bool,
    cost: bool,
    active_only: bool,
    interval: float,
) -> None:
    """Redraw the tree in place until interrupted.

    Every tick reads state off disk and re-derives the same indicators `gw
    status` shows, with `live=True` holding it to local work — the sync cache,
    task JSON, transcript mtimes, and headless pid files. That's the whole point
    of the indicator cache: a dashboard you can leave open costs no network.
    """

    def frame() -> RenderableType:
        root, total, shown = _build_tree(
            names,
            cfg=cfg,
            linear=None,
            no_linear=True,
            no_cache=no_cache,
            cost=cost,
            active_only=active_only,
            live=True,
        )
        return _watch_frame(root, total, shown, cfg=cfg, cost=cost, active_only=active_only)

    try:
        with Live(frame(), console=console, refresh_per_second=4, transient=False) as live:
            while True:
                time.sleep(interval)
                live.update(frame())
    except KeyboardInterrupt:
        # Ctrl-C is how you leave a watch; it isn't a failure.
        console.print("[muted]Stopped.[/]")


def _cost_total_lines(total: usage.Rollup) -> list[str]:
    """The `--cost` footer as markup lines, so `--watch` can fold it into a frame."""
    if total.is_empty:
        return [
            "[muted]No token usage recorded yet. Only claude and codex report it; "
            "run `gw session refresh` if sessions look stale.[/]"
        ]
    lines = [f"[bold]total[/]  {usage.fmt_cost(total)}  [muted]{usage.fmt_tokens_line(total)}[/]"]
    note = usage.unpriced_note(total)
    if note:
        lines.append(f"[muted]{note}[/]")
    lines.append("[muted]Estimated at public list prices; subscription plans differ.[/]")
    return lines


def _print_cost_total(total: usage.Rollup) -> None:
    for line in _cost_total_lines(total):
        console.print(line)
