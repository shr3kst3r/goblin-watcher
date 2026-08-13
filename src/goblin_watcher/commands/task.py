from __future__ import annotations

import contextlib
import shutil
from datetime import UTC, datetime, timedelta
from pathlib import Path

import typer
from rich.table import Table

from goblin_watcher import activity, config, gh, git, paths, state, workspace, worktree_setup
from goblin_watcher.completion_enumerators import complete_projects, complete_tasks
from goblin_watcher.console import console, print_success
from goblin_watcher.errors import GoblinError, ProjectNotFoundError, TaskNotFoundError
from goblin_watcher.models import Project, Task, TaskRepo, TaskStatus
from goblin_watcher.slug import slugify
from goblin_watcher.task_resolver import resolve_project
from goblin_watcher.windowing.headless import has_live_run, live_run_pids, remove_run_files
from goblin_watcher.windowing.tmux import TmuxWindower

app = typer.Typer()


def _find_task_anywhere(task_id: str) -> tuple[Project, Task]:
    """Search every registered project for a task with this id.

    The id must be unique across projects: two tasks sharing an id (e.g. the
    same ticket started in two repos) would otherwise resolve to whichever
    project registered first, silently operating on the wrong worktree.
    """
    matches: list[tuple[Project, Task]] = []
    for name in state.load_global().projects:
        try:
            proj = state.get_project(name)
        except ProjectNotFoundError:
            continue
        try:
            matches.append((proj, state.load_task(proj, task_id)))
        except TaskNotFoundError:
            continue
    if not matches:
        raise TaskNotFoundError(
            f"No task {task_id!r} in any registered project.",
            hint="Run `gw task ls` to see what's around.",
        )
    if len(matches) > 1:
        names = ", ".join(proj.name for proj, _ in matches)
        raise GoblinError(
            f"Task {task_id!r} exists in more than one project: {names}.",
            hint=f"Disambiguate with --project, e.g. `--project {matches[0][0].name}`.",
        )
    return matches[0]


def _find_task(task_id: str, project: str | None) -> tuple[Project, Task]:
    """Resolve a task by id, optionally scoped to a single project.

    With `project` set, the lookup is confined to that project — disambiguating
    a task id that exists in more than one. Without it, every registered project
    is searched and the id must match exactly one task.
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
            str(t.worktree_path) + ("  (archived)" if t.archived else ""),
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
        merged = state.update_task(
            proj,
            t.id,
            lambda latest, u=info["url"], s=new_status: latest.model_copy(
                update={"pr_url": u, "status": s}
            ),
        )
        updated.append(merged)
    return updated


def _first_pr_for_task(prs: list[dict[str, str]], task: Task) -> dict[str, str] | None:
    """Match a PR to a task. Exact branch wins; otherwise fall back to ticket-id basename.

    The fallback exists because (a) re-created tasks land on `<branch>-2/-3/...`
    while the original PR stays on the bare branch, and (b) teammates often
    open PRs from prefixed branches like `<user>/<id>-<slug>`. In both cases the
    ticket identifier (`eng-123`, or `gh-42` for a GitHub issue) is in the branch
    basename and that's the durable anchor.
    """
    for p in prs:
        if p["headRefName"] == task.branch:
            return p
    if task.linear:
        needle = task.linear.identifier.lower()
    elif task.github_issue:
        needle = f"gh-{task.github_issue.number}"
    else:
        return None
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
    if task.kind == "scratch":
        console.print(f"  scratch dir   {task.worktree_path}")
    elif task.is_multi_repo:
        console.print(f"  workspace     {task.workspace_path}")
        for r in task.all_repos():
            console.print(
                f"  repo          {r.project}: {r.branch} (off {r.base_branch}) → {r.worktree_path}"
            )
    else:
        console.print(f"  branch        {task.branch}")
        console.print(f"  base_branch   {task.base_branch}")
        console.print(f"  worktree      {task.worktree_path}")
    if task.parent_task is not None:
        parent = state.find_parent_task(proj, task)
        where = f"branch {parent.branch}" if parent is not None else "no longer tracked"
        console.print(f"  stacked on    {task.parent_task} ({where})")
    console.print(f"  status        {task.status}")
    if task.archived:
        when = task.archived_at.isoformat() if task.archived_at else "unknown"
        console.print(f"  archived      yes ({when}) — `gw run` restores the worktree")
    console.print(f"  pr_url        {task.pr_url or '(none)'}")
    console.print(f"  linear        {task.linear.identifier if task.linear else '(none)'}")
    if task.github_issue is not None:
        issue = task.github_issue
        # Parens, not brackets: Rich would read `[open]` as a markup tag and eat it.
        console.print(f"  github issue  {issue.reference} ({issue.state.lower()}) {issue.url}")
    console.print(f"  created_at    {task.created_at.isoformat()}")
    if task.sessions:
        console.print(f"  sessions      {len(task.sessions)} (run `gw session ls`)")
    else:
        console.print("  sessions      (none yet)")


def _ensure_task_id_free(new_id: str) -> None:
    """Refuse a task id already in use by ANY registered project.

    Same-project collisions would overwrite a record; cross-project ones would
    make the id ambiguous, forcing --project on every later command. A rename
    is a deliberate act, so the collision is cheap to avoid up front.
    """
    for name in state.load_global().projects:
        try:
            other = state.get_project(name)
        except ProjectNotFoundError:
            continue
        if state.task_file(other, new_id).exists():
            raise GoblinError(
                f"Task {new_id!r} already exists in project {other.name!r}.",
                hint="Pick a different id, or remove that task first (`gw task rm`).",
            )


def _repoint_children(proj: Project, old_id: str, new_id: str) -> list[str]:
    """Point every task stacked on `old_id` at `new_id`; returns the ids moved.

    A rename touches only the record, but `Task.parent_task` stores an id — so
    without this the children would render as "stacked on <no longer tracked>"
    even though nothing was removed.
    """
    moved: list[str] = []
    for child in state.list_tasks(proj):
        if child.parent_task != old_id:
            continue
        state.update_task(
            proj, child.id, lambda latest, n=new_id: latest.model_copy(update={"parent_task": n})
        )
        moved.append(child.id)
    return moved


@app.command("rename")
def rename(
    task_id: str = typer.Argument(..., help="Current task id.", autocompletion=complete_tasks),
    new_id: str = typer.Argument(..., help="New task id."),
    project: str | None = typer.Option(
        None,
        "--project",
        help="Limit the search to one project (disambiguates a task id shared across projects).",
        autocompletion=complete_projects,
    ),
) -> None:
    """Rename a task's id (record + tmux window only).

    The branch, worktree path, and agent sessions are untouched — sessions are
    keyed on the worktree's cwd, so leaving it in place keeps resume working.
    """
    proj, task = _find_task(task_id, project)

    new_id = new_id.strip().lower()
    slugged = slugify(new_id, max_len=60)
    if new_id != slugged:
        raise GoblinError(
            f"{new_id!r} is not a valid task id.",
            hint=f"Ids are lowercase slugs — try {slugged!r}.",
        )
    if new_id == task.id:
        raise GoblinError(f"Task is already named {new_id!r}.")
    _ensure_task_id_free(new_id)

    # New record first, then drop the old one: a crash in between leaves a
    # recoverable duplicate rather than no record at all.
    state.save_task(proj, task.model_copy(update={"id": new_id}))
    state.delete_task_record(proj, task.id)
    restacked = _repoint_children(proj, task.id, new_id)

    if restacked:
        console.print(
            f"[muted]Repointed {len(restacked)} stacked task(s) at the new id: "
            f"{', '.join(restacked)}.[/]"
        )

    if TmuxWindower().rename_window(task.id, new_id):
        console.print(f"[muted]Renamed tmux window {task.id!r} → {new_id!r}.[/]")

    print_success(f"Renamed task {task.id!r} → {new_id!r}")
    if task.kind == "scratch":
        console.print(f"[muted]The scratch directory is unchanged: {task.worktree_path}[/]")
    else:
        console.print(f"[muted]Branch ({task.branch}), worktree, and sessions are unchanged.[/]")


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
    no_setup: bool = typer.Option(
        False,
        "--no-setup",
        help="Skip the added repo's configured setup (the [setup] copy/link/run steps).",
    ),
) -> None:
    """Add another repository to an existing task (creating a multi-repo workspace)."""
    proj, task = _find_task(task_id, task_project)
    new_proj = state.get_project(project_to_add.strip().lower())

    task = workspace.attach_repo(task, new_proj, branch_name=branch_name, from_=from_)
    # Narrow patch under the task lock (ADR 0004): `attach_repo` just spent
    # several git subprocesses creating a branch and worktree, so the snapshot
    # loaded before it is stale. Only the three workspace-shaped fields it owns
    # are carried across.
    task = state.update_task(
        proj,
        task.id,
        lambda latest, t=task: latest.model_copy(
            update={
                "workspace_path": t.workspace_path,
                "worktree_path": t.worktree_path,
                "secondary_repos": t.secondary_repos,
            }
        ),
    )

    added = task.secondary_repos[-1]
    print_success(f"Added {new_proj.name!r} to task {task.id!r} on branch {added.branch!r}")
    console.print(f"  workspace   {task.workspace_path}")
    for r in task.all_repos():
        console.print(f"  {r.project:<10}  {r.worktree_path}  [muted]({r.branch})[/]")

    if not no_setup:
        result = worktree_setup.setup_task_repos(task, [added])
        if not result.ok:
            raise worktree_setup.setup_failure(task.id, result)

    console.print(
        "[muted]Relaunch the agent (`gw run "
        f"{task.id}`) so it picks up the new repo in its workspace.[/]"
    )


@app.command("setup")
def setup(
    task_id: str = typer.Argument(..., help="Task id.", autocompletion=complete_tasks),
    project: str | None = typer.Option(
        None,
        "--project",
        help="Limit the search to one project (disambiguates a task id shared across projects).",
        autocompletion=complete_projects,
    ),
    repo: str | None = typer.Option(
        None,
        "--repo",
        help="Only set up this project's checkout on a multi-repo task.",
        autocompletion=complete_projects,
    ),
) -> None:
    """Re-run the configured setup steps against a task's worktree(s).

    The same steps `gw new` applies when it materializes a worktree: copy the
    gitignored files a bare checkout lacks, link what shouldn't be duplicated,
    then run the project's bootstrap commands.
    """
    proj, task = _find_task(task_id, project)
    repos = task.all_repos()
    if repo is not None:
        wanted = repo.strip().lower()
        repos = [r for r in repos if r.project == wanted]
        if not repos:
            joined = ", ".join(r.project for r in task.all_repos())
            raise GoblinError(
                f"Task {task.id!r} has no repo for project {wanted!r}.",
                hint=f"It spans: {joined}.",
            )

    missing = [r.worktree_path for r in repos if not r.worktree_path.exists()]
    if missing:
        raise GoblinError(
            f"Worktree {missing[0]} does not exist.",
            hint=f"Recreate the task (`gw new --rm …`) or remove it (`gw task rm {task.id}`).",
        )

    result = worktree_setup.setup_task_repos(task, repos)
    if not result.ok:
        raise worktree_setup.setup_failure(task.id, result)
    if not result.ran_anything:
        console.print(
            f"[muted]No setup configured for {proj.name!r} — add a [setup] table to "
            f"{paths.config_file()} or {paths.project_setup_file(proj.root)}.[/]"
        )
        return
    print_success(f"Setup complete for task {task.id!r}")


def destroy_task(
    proj: Project,
    task: Task,
    *,
    force: bool,
    delete_branches: bool = True,
    delete_worktrees: bool = True,
) -> None:
    """Delete a task's record and, by default, every repo's worktree + branch.

    No prompts; caller handles confirmation. `delete_worktrees=False` leaves the
    checkouts on disk (used by `gw new --rm` on `--dir`, where the worktree is
    the user's in-place directory); `delete_branches=False` leaves the branches
    (used when a source adopts an existing branch — `--branch`/`--pr` — so --rm
    doesn't destroy pre-existing work). The `shutil.rmtree` fallback only kicks
    in when `force=True`. Without that guard, a non-force run could silently
    nuke untracked work after `git worktree remove` (sans `--force`) refused.
    """
    # Headless-run logs are named after the task record; once that's gone
    # nothing would reference or clean them again.
    remove_run_files(task)
    if task.kind == "scratch":
        # No git worktree or branch to clean up — the directory is the task.
        if delete_worktrees and task.worktree_path.exists():
            shutil.rmtree(task.worktree_path, ignore_errors=True)
        state.delete_task_record(proj, task.id)
        return
    for repo in task.all_repos():
        repo_root = _repo_root(proj, repo.project)
        if delete_worktrees and repo.worktree_path.exists():
            if repo_root is None:
                shutil.rmtree(repo.worktree_path, ignore_errors=True)
            else:
                try:
                    git.worktree_remove(repo_root, repo.worktree_path, force=force)
                except GoblinError:
                    if not force:
                        raise
                    shutil.rmtree(repo.worktree_path, ignore_errors=True)
        if delete_branches and repo_root is not None and git.branch_exists(repo_root, repo.branch):
            with contextlib.suppress(GoblinError):
                git.delete_branch(repo_root, repo.branch, force=True)

    if delete_worktrees and task.workspace_path is not None and task.workspace_path.exists():
        shutil.rmtree(task.workspace_path, ignore_errors=True)

    state.delete_task_record(proj, task.id)


def archive_task(proj: Project, task: Task, *, force: bool) -> list[Path]:
    """Drop every worktree on `task`, keeping the record, branch, and sessions.

    The inverse of `rematerialize_task`. No prompts and no dirty check; the
    caller owns both. As in `destroy_task`, the `shutil.rmtree` fallback only
    kicks in under `force` — without that guard a non-force run could silently
    nuke untracked work after `git worktree remove` (sans `--force`) refused.

    Returns the worktree paths actually removed.
    """
    removed: list[Path] = []
    for repo in task.all_repos():
        if not repo.worktree_path.exists():
            continue
        repo_root = _repo_root(proj, repo.project)
        if repo_root is None:
            if not force:
                raise GoblinError(
                    f"Project {repo.project!r} is not registered, so git can't remove its "
                    f"worktree at {repo.worktree_path}.",
                    hint="Re-register the project (`gw project new`), or re-run with --force "
                    "to delete the directory outright.",
                )
            shutil.rmtree(repo.worktree_path, ignore_errors=True)
        else:
            try:
                git.worktree_remove(repo_root, repo.worktree_path, force=force)
            except GoblinError:
                if not force:
                    raise
                shutil.rmtree(repo.worktree_path, ignore_errors=True)
                # git still has the path registered after an rmtree behind its
                # back, which would make rematerializing it fail.
                with contextlib.suppress(GoblinError):
                    git.worktree_prune(repo_root)
        removed.append(repo.worktree_path)

    # The workspace dir is only a container for the per-repo checkouts. Once
    # they're gone, drop it if — and only if — it is empty, so anything the user
    # parked alongside them survives the archive.
    if task.workspace_path is not None and task.workspace_path.exists():
        with contextlib.suppress(OSError):
            task.workspace_path.rmdir()

    state.update_task(
        proj,
        task.id,
        lambda latest: latest.model_copy(
            update={"archived": True, "archived_at": datetime.now(UTC)}
        ),
    )
    return removed


def rematerialize_task(proj: Project, task: Task, *, run_setup: bool = True) -> Task:
    """Recreate an archived task's worktree(s) from its branch and un-archive it.

    Idempotent per repo: a checkout still on disk is left alone, so this is safe
    to call on a task that was only partly archived. A restored worktree is a
    bare checkout again, so the project's `[setup]` steps are re-applied to it —
    the same rule `gw new` follows for anything it materializes (ADR 0007).

    Raises rather than guessing when the branch is gone: rematerializing off the
    base branch would hand back an empty worktree wearing the task's name.
    """
    restored: list[TaskRepo] = []
    for repo in task.all_repos():
        if repo.worktree_path.exists():
            continue
        repo_root = _repo_root(proj, repo.project)
        if repo_root is None:
            raise GoblinError(
                f"Project {repo.project!r} is not registered, so task {task.id!r} can't be "
                f"rematerialized.",
                hint="Re-register it with `gw project new`, then try again.",
            )
        if not git.branch_exists(repo_root, repo.branch):
            raise GoblinError(
                f"Branch {repo.branch!r} no longer exists in project {repo.project!r}, so "
                f"there is nothing to rematerialize task {task.id!r} from.",
                hint=f"The branch was deleted after the task was archived. Remove the record "
                f"with `gw task rm {task.id}`, or recreate the branch first.",
            )
        # Clears any stale registration left by an rmtree; `worktree add` would
        # otherwise refuse a path git still believes it owns.
        with contextlib.suppress(GoblinError):
            git.worktree_prune(repo_root)
        git.worktree_add(repo_root, repo.worktree_path, repo.branch)
        restored.append(repo)

    updated = state.update_task(
        proj,
        task.id,
        lambda latest: latest.model_copy(update={"archived": False, "archived_at": None}),
    )
    if not restored:
        return updated

    for repo in restored:
        console.print(f"[muted]Restored worktree {repo.worktree_path} from {repo.branch}.[/]")
    if run_setup:
        # A failed step leaves the worktree in place and un-archived: the user
        # fixes the cause and re-runs `gw task setup`, exactly as after `gw new`.
        result = worktree_setup.setup_task_repos(updated, restored)
        if not result.ok:
            raise worktree_setup.setup_failure(updated.id, result)
    return updated


def dirty_worktrees(task: Task) -> list[Path]:
    """Worktree paths on `task` that have uncommitted changes (across all repos)."""
    dirty: list[Path] = []
    for repo in task.all_repos():
        if repo.worktree_path.exists():
            with contextlib.suppress(GoblinError):
                if git.has_uncommitted_changes(repo.worktree_path):
                    dirty.append(repo.worktree_path)
    return dirty


def busy_reasons(
    task: Task,
    *,
    cfg: config.Config | None = None,
    now: datetime | None = None,
) -> list[str]:
    """Signs that an agent is still running inside `task`'s worktree.

    The companion to `dirty_worktrees`, covering the other way a destructive step
    takes work with it. A headless agent that opens and merges its own PR keeps
    running afterwards — it still has to write its final output — and it has
    committed everything by then, so the uncommitted-changes guard is at its
    weakest at exactly the moment the merge fires. Prune woke on its own
    schedule, saw MERGED, and deleted the directory out from under a live agent
    (#56). That is the normal shape of a headless run, not an edge case.

    Three independent signals, cheapest first; any one of them is enough:

    * a live pid from a detached headless run (`os.kill(pid, 0)`). The crisp one,
      and the only one that stays honest for an agent that writes nothing to its
      transcript for minutes at a stretch.
    * a session the transcript classifies as `working` — mid tool call, or the
      turn handed over unanswered (ADR 0010).
    * a session whose transcript was touched within
      `defaults.activity_grace_seconds`. This is what covers the windowing modes
      that record no pid at all (tmux panes, inline runs): whatever is hosting
      it, an agent that just merged its PR wrote to its transcript seconds ago.

    The last two are the signals `gw status --active` already uses to call a task
    in flight, so the dashboard and a prune cannot disagree about who is busy.

    Every signal self-heals, which is the property that matters for a guard on a
    scheduled job: a pid is reaped when its process exits, and both transcript
    signals expire as the session goes quiet. Nothing here can wedge a task as
    unprunable forever. Deliberately *not* a signal: a tmux window named after
    the task, which routinely outlives the agent that was in it and would do
    exactly that.

    Not a proof of absence either — an inline agent parked at a prompt for an
    hour leaves nothing to find. Callers treat a hit as "skip and say why" and
    keep `--force` as the override, the same posture as the dirty-worktree guard.
    """
    reasons: list[str] = []
    with contextlib.suppress(GoblinError):
        pids = live_run_pids(task)
        if pids:
            joined = ", ".join(str(p) for p in pids)
            reasons.append(f"a headless run is still alive (pid {joined})")

    if cfg is None:
        cfg = config.load()
    now = now or datetime.now(UTC)
    grace = float(cfg.defaults.activity_grace_seconds)
    for session in task.sessions:
        act = activity.classify(
            session,
            now=now,
            active_seconds=int(cfg.defaults.activity_active_seconds),
            stalled_after=int(cfg.defaults.activity_grace_seconds),
        )
        if act.state == "working":
            reasons.append(f"session {session.session_id} is mid-turn")
        elif act.since is not None and (now - act.since).total_seconds() <= grace:
            reasons.append(f"session {session.session_id} was active moments ago")
    return reasons


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
        dirty = dirty_worktrees(task)
        if dirty:
            raise GoblinError(
                f"Worktree {dirty[0]} has uncommitted changes.",
                hint="Commit/stash first, or re-run with --force.",
            )
        busy = busy_reasons(task)
        if busy:
            raise GoblinError(
                f"Task {task.id!r} still looks busy: {busy[0]}.",
                hint="Wait for the agent to finish, or re-run with --force to delete the "
                "worktree out from under it.",
            )
        n = len(task.all_repos())
        scope = f"{n} worktrees" if task.is_multi_repo else f"worktree {task.worktree_path}"
        if task.kind == "scratch":
            message = (
                f"Remove scratch space {task.id!r}? This permanently deletes "
                f"{task.worktree_path} and everything in it."
            )
        else:
            message = f"Remove task {task.id!r}? This deletes {scope} and their branches."
        if not typer.confirm(
            message,
            default=False,
        ):
            console.print("[muted]Cancelled.[/]")
            raise typer.Exit(code=1)

    destroy_task(proj, task, force=force)
    print_success(f"Removed task {task.id!r}")


@app.command("archive")
def archive(
    task_id: str = typer.Argument(..., help="Task id.", autocompletion=complete_tasks),
    project: str | None = typer.Option(
        None,
        "--project",
        help="Limit the search to one project (disambiguates a task id shared across projects).",
        autocompletion=complete_projects,
    ),
    force: bool = typer.Option(
        False,
        "--force",
        help="Archive anyway when the worktree is dirty or a headless run is still alive "
        "(discards uncommitted changes).",
    ),
) -> None:
    """Drop a task's worktree, keeping the record, the branch, and the sessions.

    The middle ground between `gw task rm` and keeping everything: a checkout
    per task is the expensive part, and the branch already holds the committed
    work. `gw run <task-id>` rematerializes the worktree from the branch and
    re-applies the project's setup steps.

    Not prompted, unlike `gw task rm` — nothing committed is lost, so it's
    reversible. The uncommitted-changes guard is what `--force` overrides.
    """
    proj, task = _find_task(task_id, project)

    if task.kind == "scratch":
        raise GoblinError(
            f"Task {task.id!r} is a scratch space, and its directory is the only copy of "
            f"the work — there's no branch to rematerialize it from.",
            hint=f"Remove it outright with `gw task rm {task.id}` if you're done with it.",
        )

    live = [r.worktree_path for r in task.all_repos() if r.worktree_path.exists()]
    if not live:
        if not task.archived:
            # Nothing on disk but the record says otherwise: mark it, so the
            # record matches reality and `gw run` knows to rematerialize.
            archive_task(proj, task, force=force)
            print_success(f"Marked task {task.id!r} archived (its worktree was already gone)")
        else:
            console.print(f"[muted]Task {task.id!r} is already archived.[/]")
        return

    if not force:
        dirty = dirty_worktrees(task)
        if dirty:
            raise GoblinError(
                f"Worktree {dirty[0]} has uncommitted changes.",
                hint="Archiving keeps the branch, not the working tree — commit/stash first, "
                "or re-run with --force to discard them.",
            )
        if has_live_run(task):
            raise GoblinError(
                f"A headless run for task {task.id!r} is still alive.",
                hint="Wait for it to finish, or re-run with --force to pull the worktree out "
                "from under it.",
            )

    removed = archive_task(proj, task, force=force)
    print_success(f"Archived task {task.id!r}")
    for path in removed:
        console.print(f"  [muted]removed[/] {path}")
    console.print(
        f"[muted]Branch ({task.branch}), record, and {len(task.sessions)} session(s) kept. "
        f"`gw run {task.id}` brings the worktree back.[/]"
    )


def merge_detection(
    proj: Project, task: Task, *, snapshot: gh.PrSnapshot | None = None
) -> str | None:
    """How the task's branch was detected as merged: "PR", "ancestry", or None.

    PR state wins when available; falls back to ancestry. Returning the method
    (rather than a bool) lets the prune table render it without a second
    `gh pr view` round-trip per task.

    Pass `snapshot` when the caller has already looked this PR up — the sync
    pass fetches every task's state in one batched query, and re-fetching here
    would restore the per-task round-trip that batching exists to remove. A
    snapshot whose `state` is None means "we asked and got no signal", so it
    still suppresses the lookup and falls through to ancestry.
    """
    if task.pr_url:
        pr = snapshot.state if snapshot is not None else gh.pr_state(task.pr_url)
        if pr == "MERGED":
            return "PR"
        if pr in {"OPEN", "CLOSED"}:
            return None
        # pr is None: gh missing or PR unreadable; fall through to ancestry.
    if _ancestry_says_merged(proj, task):
        return "ancestry"
    return None


def _ancestry_says_merged(proj: Project, task: Task) -> bool:
    """The ancestry fallback, gated on knowing where the branch started.

    A branch that has never had a commit is an ancestor of its base and holds
    nothing unique — the same shape a merged branch has. The commit graph does
    not carry the fact that separates them, so ancestry on its own deleted
    minutes-old tasks the moment anyone else landed on the base branch (#46).

    The one thing that does separate them is the fork point, which gw knows
    because gw cut the branch. So: require a recorded fork point, and refuse to
    call the branch merged while its tip is still sitting on it. **A missing
    fork point reads as "unknown", never as "no commits yet"** — task records
    written before the field existed have none, and pruning is destructive.
    """
    if task.fork_sha is None:
        return False
    return git.is_branch_merged(proj.root, task.branch, task.base_branch, fork_sha=task.fork_sha)


def scratch_last_activity(task: Task) -> datetime:
    """Most recent sign of life on a scratch task: creation or last session use."""
    stamps = [task.created_at, *(s.last_used_at for s in task.sessions)]
    return max(ts if ts.tzinfo else ts.replace(tzinfo=UTC) for ts in stamps)


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
        help="Skip the confirmation prompt; also remove tasks with uncommitted changes "
        "or a still-running agent.",
    ),
    fetch: bool = typer.Option(True, "--fetch/--no-fetch", help="Run `git fetch` before checking."),
    scratch_older_than: int | None = typer.Option(
        None,
        "--scratch-older-than",
        help="Also prune scratch spaces idle for more than N days (0 = all of them). "
        "Idle means no session use since then. Without this flag, scratch tasks "
        "are never pruned.",
    ),
) -> None:
    """Remove tasks whose branch is merged into the base branch.

    Detection: if the task has a PR URL, `gh pr view` decides. Otherwise, falls
    back to `git merge-base --is-ancestor <branch> origin/<base>` — but only for
    a task whose recorded fork point says the branch has actually moved since it
    was cut. A task with no recorded fork point is never pruned by ancestry.
    Squash- and rebase-merged branches without a recorded PR go undetected.

    Scratch spaces have no branch, so "merged" never applies; pass
    `--scratch-older-than N` to prune the ones idle for more than N days
    (deleting their directories permanently).

    A merged task is still skipped, with a reason, when an agent looks to be
    running in its worktree (`busy_reasons`) or the worktree is dirty. `--force`
    overrides both.
    """
    if scratch_older_than is not None and scratch_older_than < 0:
        raise GoblinError("--scratch-older-than must be non-negative.")
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

    now = datetime.now(UTC)
    merged: list[tuple[Project, Task, str]] = []
    scratch_skipped = 0
    for proj in projects:
        if fetch and proj.kind != "scratch":
            with contextlib.suppress(GoblinError):
                git.fetch(proj.root)
        for task in state.list_tasks(proj):
            if task.kind == "scratch":
                # No branch, so "merged" can never apply; prune by idle age.
                if scratch_older_than is None:
                    scratch_skipped += 1
                    continue
                idle = now - scratch_last_activity(task)
                if idle >= timedelta(days=scratch_older_than):
                    merged.append((proj, task, f"idle {idle.days}d"))
                continue
            detected = merge_detection(proj, task)
            if detected is not None:
                merged.append((proj, task, detected))

    if not merged:
        console.print("[muted]Nothing to prune.[/]")
        if scratch_skipped:
            console.print(
                f"[hint]{scratch_skipped} scratch space(s) untouched — prune idle "
                f"ones with --scratch-older-than N.[/]"
            )
        return

    table = Table(
        title=f"Tasks to prune{' (dry run)' if dry_run else ''}",
        show_header=True,
        header_style="bold",
    )
    table.add_column("Project")
    table.add_column("Task")
    table.add_column("Branch")
    table.add_column("Detected via")
    for proj, task, detected in merged:
        branch = "—" if task.kind == "scratch" else task.branch
        table.add_row(proj.name, task.id, branch, detected)
    console.print(table)

    if dry_run:
        return

    if not force and not typer.confirm(f"Remove {len(merged)} task(s)?", default=False):
        console.print("[muted]Cancelled.[/]")
        raise typer.Exit(code=1)

    removed = 0
    skipped: list[tuple[Project, Task, str]] = []
    # Loaded once, not per task: the activity thresholds are the same for all of
    # them and `config.load()` re-reads the file on every call.
    cfg = config.load()
    for proj, task, _detected in merged:
        if not force:
            busy = busy_reasons(task, cfg=cfg)
            if busy:
                skipped.append((proj, task, busy[0]))
                continue
            if dirty_worktrees(task):
                skipped.append((proj, task, "uncommitted changes"))
                continue
        destroy_task(proj, task, force=force)
        removed += 1

    print_success(f"Removed {removed} task(s)")
    for proj, task, reason in skipped:
        console.print(f"[hint]Skipped {proj.name}/{task.id}: {reason}[/]")
