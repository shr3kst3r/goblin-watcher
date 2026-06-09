from __future__ import annotations

import contextlib
import shutil
from pathlib import Path

import typer
from rich.table import Table

from goblin_watcher import gh, git, state, workspace
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
    if task.is_multi_repo:
        console.print(f"  workspace     {task.workspace_path}")
        for r in task.all_repos():
            console.print(
                f"  repo          {r.project}: {r.branch} (off {r.base_branch}) → {r.worktree_path}"
            )
    else:
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


@app.command("add-repo")
def add_repo(
    task_id: str = typer.Argument(..., help="Task id.", autocompletion=complete_tasks),
    project_to_add: str = typer.Argument(
        ..., help="Registered project to add to the task.", autocompletion=complete_projects
    ),
    task_project: str | None = typer.Option(
        None,
        "--task-project",
        help="Limit the task lookup to one project (disambiguates a shared task id).",
        autocompletion=complete_projects,
    ),
    branch_name: str | None = typer.Option(
        None, "--branch-name", help="Branch name for the added repo (defaults to the task's slug)."
    ),
    from_: str | None = typer.Option(
        None, "--from", help="Base branch for the added repo (defaults to its project's default)."
    ),
) -> None:
    """Add another repository to an existing task (creating a multi-repo workspace)."""
    proj, task = _find_task(task_id, task_project)
    new_proj = state.get_project(project_to_add.strip().lower())

    task = workspace.attach_repo(task, new_proj, branch_name=branch_name, from_=from_)
    state.save_task(proj, task)

    added = task.secondary_repos[-1]
    print_success(f"Added {new_proj.name!r} to task {task.id!r} on branch {added.branch!r}")
    console.print(f"  workspace   {task.workspace_path}")
    for r in task.all_repos():
        console.print(f"  {r.project:<10}  {r.worktree_path}  [muted]({r.branch})[/]")
    console.print(
        "[muted]Relaunch the agent (`gw run "
        f"{task.id}`) so it picks up the new repo in its workspace.[/]"
    )


def _destroy_task(proj: Project, task: Task, *, force: bool) -> None:
    """Delete every repo's worktree + branch, the workspace dir, and the record.

    No prompts; caller handles confirmation. The `shutil.rmtree` fallback only
    kicks in when `force=True`. Without that guard, a non-force run could
    silently nuke untracked work after `git worktree remove` (sans `--force`)
    refused.
    """
    for repo in task.all_repos():
        repo_root = _repo_root(proj, repo.project)
        if repo.worktree_path.exists():
            if repo_root is None:
                shutil.rmtree(repo.worktree_path, ignore_errors=True)
            else:
                try:
                    git.worktree_remove(repo_root, repo.worktree_path, force=force)
                except GoblinError:
                    if not force:
                        raise
                    shutil.rmtree(repo.worktree_path, ignore_errors=True)
        if repo_root is not None and git.branch_exists(repo_root, repo.branch):
            with contextlib.suppress(GoblinError):
                git.delete_branch(repo_root, repo.branch, force=True)

    if task.workspace_path is not None and task.workspace_path.exists():
        shutil.rmtree(task.workspace_path, ignore_errors=True)

    state.delete_task_record(proj, task.id)


def _dirty_worktrees(task: Task) -> list[Path]:
    """Worktree paths on `task` that have uncommitted changes (across all repos)."""
    dirty: list[Path] = []
    for repo in task.all_repos():
        if repo.worktree_path.exists():
            with contextlib.suppress(GoblinError):
                if git.has_uncommitted_changes(repo.worktree_path):
                    dirty.append(repo.worktree_path)
    return dirty


def _repo_root(primary: Project, project_name: str) -> Path | None:
    """Project root for a repo on a task, or None if the project is unregistered."""
    if project_name == primary.name:
        return primary.root
    try:
        return state.get_project(project_name).root
    except ProjectNotFoundError:
        return None


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

    if not force:
        dirty = _dirty_worktrees(task)
        if dirty:
            raise GoblinError(
                f"Worktree {dirty[0]} has uncommitted changes.",
                hint="Commit/stash first, or re-run with --force.",
            )
        n = len(task.all_repos())
        scope = f"{n} worktrees" if task.is_multi_repo else f"worktree {task.worktree_path}"
        if not typer.confirm(
            f"Remove task {task.id!r}? This deletes {scope} and their branches.",
            default=False,
        ):
            console.print("[muted]Cancelled.[/]")
            raise typer.Exit(code=1)

    _destroy_task(proj, task, force=force)
    print_success(f"Removed task {task.id!r}")


def _merge_detection(proj: Project, task: Task) -> str | None:
    """How the task's branch was detected as merged: "PR", "ancestry", or None.

    PR state wins when available; falls back to ancestry. Returning the method
    (rather than a bool) lets the prune table render it without a second
    `gh pr view` round-trip per task.
    """
    if task.pr_url:
        pr = gh.pr_state(task.pr_url)
        if pr == "MERGED":
            return "PR"
        if pr in {"OPEN", "CLOSED"}:
            return None
        # pr is None: gh missing or PR unreadable; fall through to ancestry.
    if git.is_branch_merged(proj.root, task.branch, task.base_branch):
        return "ancestry"
    return None


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
        # Skip stale registrations (directory deleted, metadata gone) rather
        # than letting one bad project abort pruning for all of them.
        projects = []
        for n in state.load_global().projects:
            try:
                projects.append(state.get_project(n))
            except ProjectNotFoundError:
                console.print(f"[hint]Skipped project {n!r}: metadata missing.[/]")

    merged: list[tuple[Project, Task, str]] = []
    for proj in projects:
        if fetch:
            with contextlib.suppress(GoblinError):
                git.fetch(proj.root)
        for task in state.list_tasks(proj):
            detected = _merge_detection(proj, task)
            if detected is not None:
                merged.append((proj, task, detected))

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
    for proj, task, detected in merged:
        table.add_row(proj.name, task.id, task.branch, detected)
    console.print(table)

    if dry_run:
        return

    if not force and not typer.confirm(f"Remove {len(merged)} task(s)?", default=False):
        console.print("[muted]Cancelled.[/]")
        raise typer.Exit(code=1)

    removed = 0
    skipped: list[tuple[Project, Task, str]] = []
    for proj, task, _detected in merged:
        if not force and _dirty_worktrees(task):
            skipped.append((proj, task, "uncommitted changes"))
            continue
        _destroy_task(proj, task, force=force)
        removed += 1

    print_success(f"Removed {removed} task(s)")
    for proj, task, reason in skipped:
        console.print(f"[hint]Skipped {proj.name}/{task.id}: {reason}[/]")
