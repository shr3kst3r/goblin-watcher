"""Assemble multi-repo task workspaces.

A multi-repo task launches the agent in a *workspace directory* that holds each
participating repo as a subdirectory worktree (see ADR 0003). This module owns
the mechanics of building that layout:

- `promote_to_workspace` — relocate a single-repo task's worktree into a fresh
  workspace so additional repos can join it.
- `attach_repo` — create a branch + worktree for another project inside the
  workspace and append it to the task.

Teardown lives with `gw task rm` in `commands/task.py`, which already owns the
uncommitted-changes guard and record deletion.
"""

from __future__ import annotations

from pathlib import Path

from goblin_watcher import git, paths, state
from goblin_watcher.console import console
from goblin_watcher.errors import GoblinError
from goblin_watcher.models import Project, Task, TaskRepo
from goblin_watcher.slug import branch_slug


def _refresh_base(repo_root: Path, base: str) -> None:
    """Best-effort fast-forward of `base` from origin; warn but never block.

    Mirrors `commands/new._refresh_base` (kept separate to avoid importing a
    command module here). Creating the local base from origin is what lets
    `git worktree add ... <base>` succeed when the base only exists remotely.
    """
    res = git.pull_base_from_remote(repo_root, base)
    if res.outcome in {"updated", "created"}:
        console.print(f"[success]{res.detail}[/]")
    elif res.outcome in {"diverged", "dirty", "fetch_failed"}:
        console.print(f"[hint]Warning:[/] {res.detail}")


def _ensure_unique_branch(repo_root: Path, branch: str) -> str:
    if not git.branch_exists(repo_root, branch):
        return branch
    n = 2
    while git.branch_exists(repo_root, f"{branch}-{n}"):
        n += 1
    return f"{branch}-{n}"


def derive_branch(task: Task, new_proj: Project, branch_name: str | None) -> str:
    """Choose the branch name for `new_proj`'s worktree on this task.

    Precedence: an explicit `branch_name` → a Linear-derived slug (honoring the
    new project's prefix) → the primary branch with its prefix swapped for the
    new project's. Always honors `new_proj.branch_prefix`.
    """
    if branch_name:
        return f"{new_proj.branch_prefix}{branch_name}"
    if task.linear is not None:
        return branch_slug(task.linear.identifier, task.linear.title, prefix=new_proj.branch_prefix)
    primary = state.get_project(task.project)
    body = task.branch
    if primary.branch_prefix and body.startswith(primary.branch_prefix):
        body = body[len(primary.branch_prefix) :]
    return f"{new_proj.branch_prefix}{body}"


def promote_to_workspace(task: Task) -> Task:
    """Move a single-repo task into a workspace directory so repos can be added.

    No-op if the task already has a workspace. Refuses if the primary worktree
    has uncommitted changes — `git worktree move` would relocate live work out
    from under any running session.
    """
    if task.workspace_path is not None:
        return task

    proj = state.get_project(task.project)
    ws = paths.task_workspace(task.id)
    dest = ws / task.project

    if task.worktree_path.resolve() != dest.resolve():
        if task.worktree_path.exists() and git.has_uncommitted_changes(task.worktree_path):
            raise GoblinError(
                f"Worktree {task.worktree_path} has uncommitted changes; "
                "cannot move it into a multi-repo workspace.",
                hint="Commit or stash the changes, then retry.",
            )
        ws.mkdir(parents=True, exist_ok=True)
        git.worktree_move(proj.root, task.worktree_path, dest)

    return task.model_copy(update={"workspace_path": ws, "worktree_path": dest})


def attach_repo(
    task: Task,
    new_proj: Project,
    *,
    branch_name: str | None = None,
    from_: str | None = None,
) -> Task:
    """Add `new_proj` to `task`: create its branch + worktree inside the workspace.

    Promotes a single-repo task to a workspace first. Returns the updated task
    (not persisted — the caller saves). If this raises after the worktree was
    created, the worktree is left on disk; re-running attach is safe because
    `git worktree add` on an existing path fails loudly rather than clobbering.
    """
    if any(r.project == new_proj.name for r in task.all_repos()):
        raise GoblinError(
            f"Task {task.id!r} already includes project {new_proj.name!r}.",
            hint="Each project can join a task at most once.",
        )

    task = promote_to_workspace(task)
    assert task.workspace_path is not None

    base = from_ or new_proj.default_branch
    _refresh_base(new_proj.root, base)
    branch = _ensure_unique_branch(new_proj.root, derive_branch(task, new_proj, branch_name))
    dest = task.workspace_path / new_proj.name

    git.worktree_add(new_proj.root, dest, branch, base=base)
    repo = TaskRepo(
        project=new_proj.name,
        branch=branch,
        worktree_path=dest,
        base_branch=base,
    )
    return task.model_copy(update={"secondary_repos": [*task.secondary_repos, repo]})
