from __future__ import annotations

from datetime import UTC, datetime

import typer
from rich.tree import Tree

from goblin_watcher import config, description, git, secrets, sessions, state
from goblin_watcher.completion_enumerators import complete_projects
from goblin_watcher.console import console
from goblin_watcher.errors import GoblinError, ProjectNotFoundError
from goblin_watcher.linear import LinearClient
from goblin_watcher.models import Project, SessionRecord, Task


def _sync_indicators(proj: Project, task: Task) -> str:
    """Markup snippet flagging unpushed/uncommitted work on `task`. '' when clean.

    Projects with a remote (`proj.repo_url`) get a "↑N unpushed" tally relative
    to `origin/<branch>` when that ref exists, or relative to the base branch
    when the branch was never pushed. Local-only projects get "↑N unmerged"
    relative to the base branch instead.
    """
    if not task.worktree_path.exists():
        return ""
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


class _LinearStateFetcher:
    """Lazy-constructed Linear client that fetches state with cached fallback.

    The client is built on first use; if API-key resolution fails, the fetcher
    permanently disables itself for this run so the rest of `gw status` still
    renders. Per-issue fetch errors fall back to the cached state silently.
    """

    def __init__(self) -> None:
        self._client: LinearClient | None = None
        self._disabled = False

    def _client_or_none(self) -> LinearClient | None:
        if self._disabled:
            return None
        if self._client is None:
            try:
                api_key = secrets.get_linear_api_key()
                self._client = LinearClient(api_key)
            except GoblinError:
                self._disabled = True
                return None
        return self._client

    def refresh(self, project: Project, task: Task) -> Task:
        """Fetch the latest Linear state for `task` and persist it. No-op on failure.

        Skips the API round-trip while the cached state is younger than
        `defaults.linear_state_ttl_seconds` — one fetch per task per status
        run adds up fast.
        """
        if task.linear is None:
            return task
        if not self._cache_expired(task):
            return task
        client = self._client_or_none()
        if client is None:
            return task
        try:
            fresh = client.fetch_issue_state(task.linear.identifier)
        except GoblinError:
            return task
        # Persist even when the state is unchanged: the timestamp is what
        # keeps the next status run inside the TTL window.
        updated = task.model_copy(
            update={
                "linear": task.linear.model_copy(update={"state": fresh}),
                "linear_state_updated_at": datetime.now(UTC),
            }
        )
        state.save_task(project, updated)
        return updated

    @staticmethod
    def _cache_expired(task: Task) -> bool:
        ts = task.linear_state_updated_at
        if ts is None:
            return True
        if ts.tzinfo is None:
            ts = ts.replace(tzinfo=UTC)
        try:
            ttl = int(config.load().defaults.linear_state_ttl_seconds)
        except Exception:
            ttl = 300
        return (datetime.now(UTC) - ts).total_seconds() >= ttl

    def close(self) -> None:
        if self._client is not None:
            self._client.close()


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
        help="Skip Linear state refresh (no network; cached states still shown).",
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
    linear = _LinearStateFetcher()

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

            for task in tasks:
                if not no_linear:
                    task = linear.refresh(proj, task)
                adopted = sessions.adopt_orphan_sessions(task)
                if adopted is not task:
                    sessions.persist(proj, adopted)
                    task = adopted
                linear_suffix = ""
                if task.linear:
                    state_style = _linear_state_style(task.linear.state)
                    linear_suffix = (
                        f" · {task.linear.title}  [muted](linear:[/] "
                        f"[{state_style}]{task.linear.state}[/][muted])[/]"
                    )
                sync_suffix = _sync_indicators(proj, task)
                task_label = (
                    f"[bold]{task.id}[/]"
                    + linear_suffix
                    + f"  [muted][{task.status}][/]"
                    + sync_suffix
                )
                task_node = proj_node.add(task_label)
                if not task.sessions:
                    task_node.add("[muted](no sessions yet)[/]")
                    continue
                # Lazy refresh stale summaries on the way through.
                refreshed = sessions.refresh_task_summaries(task)
                sessions.persist(proj, refreshed)
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
