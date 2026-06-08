from __future__ import annotations

import contextlib
import shutil

import typer
from rich.table import Table

from goblin_watcher import gh, git, state
from goblin_watcher.completion_enumerators import complete_projects, complete_tasks
from goblin_watcher.console import console, print_success
from goblin_watcher.errors import GoblinError, ProjectNotFoundError, TaskNotFoundError
from goblin_watcher.models import Project, Task, TaskStatus
from goblin_watcher.task_resolver import resolve_project

app = typer.Typer()


def _find_task_anywhere(task_id: str) -> tuple[Project, Task]:
    """Search every registered project for a task with this id."""
    for name in state.load_global().projects:
        try:
            proj = state.get_project(name)
        except ProjectNotFoundError:
            continue
        try:
            return proj, state.load_task(proj, task_id)
        except TaskNotFoundError:
            continue
    raise TaskNotFoundError(
        f"No task {task_id!r} in any registered project.",
        hint="Run `gw task ls` to see what's around.",
    )


def _find_task(task_id: str, project: str | None) -> tuple[Project, Task]:
    """Resolve a task by id, optionally scoped to a single project.

    With `project` set, the lookup is confined to that project — disambiguating
    a task id that exists in more than one. Without it, every registered project
    is searched and the first match wins.
    """
    if project is not None:
        proj = state.get_project(project.strip().lower())
        return proj, state.load_task(proj, task_id)
    return _find_task_anywhere(task_id)


@app.command("ls")
def ls(
    project: str | None = typer.Option(
        None,
        "--project",
        help="Limit to a single project (opens the project picker if omitted).",
        autocompletion=complete_projects,
    ),
    status: str | None = typer.Option(None, "--status", help="Limit to a single status."),
    refresh_prs: bool = typer.Option(
        True,
        "--refresh-prs/--no-refresh-prs",
        help="Query `gh` once and backfill PR URLs onto tasks (default on).",
    ),
) -> None:
    """List tasks in a project (picked interactively if --project is omitted)."""
    proj = resolve_project(project)
    tasks = state.list_tasks(proj)
    if status:
        tasks = [t for t in tasks if t.status == status]
    if not tasks:
        console.print(f"[muted]No tasks in project {proj.name!r}.[/]")
        return

    if refresh_prs:
        tasks = _backfill_prs(proj, tasks)

    table = Table(title=f"Tasks in {proj.name}", show_header=True, header_style="bold")
    table.add_column("ID")
    table.add_column("Branch")
    table.add_column("Status")
    table.add_column("Worktree")
    table.add_column("PR")
    for t in tasks:
        table.add_row(
            t.id,
            t.branch,
            t.status,
            str(t.worktree_path),
            t.pr_url or "",
        )
    console.print(table)


def _backfill_prs(proj: Project, tasks: list[Task]) -> list[Task]:
    """Look up PRs via `gh` and persist matches on tasks. Returns the updated list."""
    prs = gh.list_repo_prs(proj.root)
    if not prs:
        return tasks
    updated: list[Task] = []
    for t in tasks:
        info = _first_pr_for_task(prs, t)
        if not info or not info.get("url"):
            updated.append(t)
            continue
        new_status = _status_from_pr_state(info.get("state"), t.status)
        if t.pr_url == info["url"] and t.status == new_status:
            updated.append(t)
            continue
        merged = t.model_copy(update={"pr_url": info["url"], "status": new_status})
        state.save_task(proj, merged)
        updated.append(merged)
    return updated


def _first_pr_for_task(prs: list[dict[str, str]], task: Task) -> dict[str, str] | None:
    """Match a PR to a task. Exact branch wins; otherwise fall back to Linear-id basename.

    The fallback exists because (a) re-created tasks land on `<branch>-2/-3/...`
    while the original PR stays on the bare branch, and (b) teammates often
    open PRs from prefixed branches like `<user>/<id>-<slug>`. In both cases the
    Linear identifier is in the branch basename and that's the durable anchor.
    """
    for p in prs:
        if p["headRefName"] == task.branch:
            return p
    if not task.linear:
        return None
    needle = task.linear.identifier.lower()
    for p in prs:
        basename = p["headRefName"].rsplit("/", 1)[-1].lower()
        if basename == needle or basename.startswith(needle + "-"):
            return p
    return None


def _status_from_pr_state(pr_state: str | None, current: TaskStatus) -> TaskStatus:
    """Translate a GitHub PR state into our TaskStatus enum, preserving terminal states."""
    if pr_state == "MERGED":
        return "merged"
    if pr_state == "CLOSED":
        # Don't downgrade a previously-merged task if `gh` returns CLOSED.
        return current if current == "merged" else "closed"
    if pr_state == "OPEN":
        return "pr-open" if current in ("open", "pushed") else current
    return current


@app.command("show")
def show(
    task_id: str = typer.Argument(..., help="Task id.", autocompletion=complete_tasks),
    project: str | None = typer.Option(
        None,
        "--project",
        help="Limit the search to one project (disambiguates a task id shared across projects).",
        autocompletion=complete_projects,
    ),
) -> None:
    """Show a task's full details."""
    proj, task = _find_task(task_id, project)
    console.print(f"[bold]{task.id}[/] [muted](project: {proj.name})[/]")
    console.print(f"  branch        {task.branch}")
    console.print(f"  base_branch   {task.base_branch}")
    console.print(f"  worktree      {task.worktree_path}")
    console.print(f"  status        {task.status}")
    console.print(f"  pr_url        {task.pr_url or '(none)'}")
    console.print(f"  linear        {task.linear.identifier if task.linear else '(none)'}")
    console.print(f"  created_at    {task.created_at.isoformat()}")
    if task.sessions:
        console.print(f"  sessions      {len(task.sessions)} (run `gw session ls`)")
    else:
        console.print("  sessions      (none yet)")


def _destroy_task(proj: Project, task: Task, *, force: bool) -> None:
    """Delete worktree + branch + record. No prompts; caller handles confirmation.

    The `shutil.rmtree` fallback only kicks in when `force=True`. Without that
    guard, a non-force run could silently nuke untracked work after `git worktree
    remove` (sans `--force`) refused.
    """
    if task.worktree_path.exists():
        try:
            git.worktree_remove(proj.root, task.worktree_path, force=force)
        except GoblinError:
            if not force:
                raise
            shutil.rmtree(task.worktree_path, ignore_errors=True)

    if git.branch_exists(proj.root, task.branch):
        with contextlib.suppress(GoblinError):
            git.delete_branch(proj.root, task.branch, force=True)

    state.delete_task_record(proj, task.id)


@app.command("rm")
def rm(
    task_id: str = typer.Argument(..., help="Task id.", autocompletion=complete_tasks),
    project: str | None = typer.Option(
        None,
        "--project",
        help="Limit the search to one project (disambiguates a task id shared across projects).",
        autocompletion=complete_projects,
    ),
    force: bool = typer.Option(
        False, "--force", help="Skip the confirmation prompt and safety checks."
    ),
) -> None:
    """Remove a task: deletes the worktree, deletes the branch, removes the record."""
    proj, task = _find_task(task_id, project)

    if task.worktree_path.exists() and not force:
        if git.has_uncommitted_changes(task.worktree_path):
            raise GoblinError(
                f"Worktree {task.worktree_path} has uncommitted changes.",
                hint="Commit/stash first, or re-run with --force.",
            )
        if not typer.confirm(
            f"Remove task {task.id!r}? "
            f"This deletes worktree {task.worktree_path} and branch {task.branch!r}.",
            default=False,
        ):
            console.print("[muted]Cancelled.[/]")
            raise typer.Exit(code=1)

    _destroy_task(proj, task, force=force)
    print_success(f"Removed task {task.id!r}")


def _is_task_merged(proj: Project, task: Task) -> bool:
    """True if the task's branch is merged. PR state wins when available; falls back to ancestry."""
    if task.pr_url:
        pr = gh.pr_state(task.pr_url)
        if pr == "MERGED":
            return True
        if pr in {"OPEN", "CLOSED"}:
            return False
        # pr is None: gh missing or PR unreadable; fall through to ancestry.
    return git.is_branch_merged(proj.root, task.branch, task.base_branch)


@app.command("prune")
def prune(
    project: str | None = typer.Option(
        None,
        "--project",
        help="Limit to a single project (defaults to all registered projects).",
        autocompletion=complete_projects,
    ),
    dry_run: bool = typer.Option(
        False, "--dry-run", help="Print what would be removed; do nothing."
    ),
    force: bool = typer.Option(
        False,
        "--force",
        help="Skip the confirmation prompt; also remove tasks with uncommitted changes.",
    ),
    fetch: bool = typer.Option(True, "--fetch/--no-fetch", help="Run `git fetch` before checking."),
) -> None:
    """Remove tasks whose branch is merged into the base branch.

    Detection: if the task has a PR URL, `gh pr view` decides. Otherwise, falls
    back to `git merge-base --is-ancestor <branch> origin/<base>`. Squash- and
    rebase-merged branches without a recorded PR may go undetected.
    """
    if project:
        projects = [state.get_project(project.strip().lower())]
    else:
        projects = [state.get_project(n) for n in state.load_global().projects]

    merged: list[tuple[Project, Task]] = []
    for proj in projects:
        if fetch:
            with contextlib.suppress(GoblinError):
                git.fetch(proj.root)
        for task in state.list_tasks(proj):
            if _is_task_merged(proj, task):
                merged.append((proj, task))

    if not merged:
        console.print("[muted]Nothing to prune.[/]")
        return

    table = Table(
        title=f"Merged tasks{' (dry run)' if dry_run else ''}",
        show_header=True,
        header_style="bold",
    )
    table.add_column("Project")
    table.add_column("Task")
    table.add_column("Branch")
    table.add_column("Detected via")
    for proj, task in merged:
        detected = "PR" if task.pr_url and gh.pr_state(task.pr_url) == "MERGED" else "ancestry"
        table.add_row(proj.name, task.id, task.branch, detected)
    console.print(table)

    if dry_run:
        return

    if not force and not typer.confirm(f"Remove {len(merged)} task(s)?", default=False):
        console.print("[muted]Cancelled.[/]")
        raise typer.Exit(code=1)

    removed = 0
    skipped: list[tuple[Project, Task, str]] = []
    for proj, task in merged:
        if (
            task.worktree_path.exists()
            and git.has_uncommitted_changes(task.worktree_path)
            and not force
        ):
            skipped.append((proj, task, "uncommitted changes"))
            continue
        _destroy_task(proj, task, force=force)
        removed += 1

    print_success(f"Removed {removed} task(s)")
    for proj, task, reason in skipped:
        console.print(f"[hint]Skipped {proj.name}/{task.id}: {reason}[/]")
