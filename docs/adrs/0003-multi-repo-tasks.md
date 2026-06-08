# 0003. A task can span multiple repositories

- Status: proposed
- Date: 2026-06-08

## Context

A `Task` in `gw` is single-repo by construction. Four places encode that assumption:

1. **Model** (`models.Task`) — scalar `project`, `branch`, `worktree_path`, `base_branch`, `pr_url`. One of each.
2. **Storage** (`state.task_file`) — a task's JSON lives inside one project at `<project_root>/.goblin/tasks/<id>.json`. That project physically owns the task.
3. **Launch** (`agents/launcher.launch`) — the agent runs with `cwd = task.worktree_path`. One process, one working directory.
4. **PR / teardown** (`commands/pr.py`, `gw task rm`) — push one branch from one worktree, open one PR.

But real changes routinely cross repositories: a backend API change plus its frontend consumer, a shared library plus the service that vendors it, a schema migration plus the jobs that read it. Today the only way to drive that with `gw` is to create two unrelated tasks and run two agents that can't see each other's working trees. The agent loses the cross-repo context that makes the change coherent, and the two halves are tracked as if unrelated.

What is *already* well-shaped for multi-repo:

- **Windowing** is neutral. `Windower.run` takes whatever `cwd` the launcher hands it, so one tmux window per task keeps working — only the `cwd` changes.
- **Sessions are task-level.** A single agent process spanning N repos is one session, not N. `Task.sessions` already models exactly that. No change to the session model.

So the decision is narrow: how to represent N repos on a task, how to give one agent process access to all of them, and how the PR/teardown flows fan out.

## Decision

A task may reference one or more repositories. We make three coupled choices.

### 1. Agent access: a workspace directory of real worktrees

When a task has more than one repo, `gw` creates a **workspace directory** and places each repo's git worktree as a subdirectory of it:

```
<workspace>/
  backend/      ← worktree of the backend repo on the task branch
  frontend/     ← worktree of the frontend repo on the task branch
```

The agent launches with `cwd = <workspace>`. Because `git worktree add` accepts an arbitrary destination, each subdir is a normal worktree — no symlinks, no special git config. This is agent-agnostic: `claude`, `codex`, and `gemini` all simply see a folder containing two repos. (For agents that support a multi-root flag, e.g. Claude's `--add-dir`, we may additionally pass each repo root; this is an optimization, not load-bearing.)

The workspace lives at a neutral, task-scoped location under the data tier (`paths.workspace_root() / <task_id>/`), not inside any one repo's `.worktrees/`, so no repo is privileged as the filesystem parent and teardown is a single directory removal plus per-repo `git worktree remove`.

A **single-repo task is unchanged**: no workspace directory, `cwd = worktree_path` exactly as today. The workspace only materializes when a second repo is attached.

### 2. Model: primary scalars + a secondary list

`Task` keeps its existing scalar fields as the **primary repo** and gains a list of additional repos:

```python
class TaskRepo(_Frozen):
    project: str
    branch: str
    worktree_path: Path
    base_branch: str
    pr_url: str | None = None

class Task(_Frozen):
    id: str
    project: str            # primary repo (unchanged)
    branch: str
    worktree_path: Path
    base_branch: str
    pr_url: str | None = None
    secondary_repos: list[TaskRepo] = []      # NEW
    workspace_path: Path | None = None        # NEW; set iff secondary_repos
    linear: LinearIssue | None = None
    sessions: list[SessionRecord] = []
    status: TaskStatus = "open"
    created_at: datetime

    def all_repos(self) -> list[TaskRepo]:
        """Primary first, then secondaries — the canonical iteration order."""
        ...
```

Existing task JSON is valid as-is (`secondary_repos` defaults empty, `workspace_path` null), so there is **no migration**. Code that operates on a single repo keeps reading the primary scalars unchanged; only code that must act on *every* repo (workspace assembly, `pr open`, `task rm`, status display, the seed prompt) iterates `all_repos()`.

The launcher computes `cwd = task.workspace_path or task.worktree_path`.

### 3. Ownership: the primary project owns the task

The task JSON stays at `<primary_project>/.goblin/tasks/<id>.json`. Secondary projects are referenced and get a worktree added, but hold no task record. The Linear anchor, the task id, and `gw task ls` ownership all follow the primary. This keeps the documented two-tier storage model intact (no new global task tier) at the cost of a mild asymmetry: a secondary repo's `gw task ls` won't list the task unless we later add cross-referencing.

### CLI surface

- **`gw new --project A --with-project B …`** (`--with-project` repeatable) — create a multi-repo task up front. `--project` is primary; each `--with-project` names an already-registered project to add. (`--project` stays singular for consistency with every other command, where it is a scoping filter.) All repos share the branch slug (Linear-derived or auto-generated), each honoring its own `branch_prefix`; each repo's base is its own `default_branch` unless `--from` overrides. Not valid with `--dir`/`--pr`. Clone-on-demand for secondaries is a follow-up. Assembly saves progressively, so a mid-assembly failure leaves a consistent task (the repos attached so far) and surfaces the error rather than launching on a partial task.
- **`gw task add-repo <task> <project>`** — attach a repo to an existing task. Creates the matching branch + worktree, promotes the task to a workspace layout (moving the primary worktree into the workspace via `git worktree move`), and relinks. `--task-project` disambiguates a shared task id; `--branch-name`/`--from` override the added repo's branch/base. If the primary worktree is dirty, refuse and tell the user to commit/stash first (we do not `git worktree move` a dirty tree).
- **`gw pr open`** — iterate `all_repos()`: push + open a PR per repo, cross-linking the PR URLs in each body. `--repo <project>` targets a single repo. Each repo's `pr_url` is stored on its `TaskRepo` (or the primary scalar).
- **`gw task rm`** — tear down every worktree (the existing uncommitted-changes guard runs per repo) and remove the workspace directory.
- **Seed prompt** — list every repo with its branch/worktree instead of one.

## Consequences

- **Single-repo flows are untouched.** No migration, no behavior change, no workspace directory. The feature is inert until a second repo is attached.
- **One agent, full cross-repo context.** The agent sees both repos as sibling directories and can reason about and edit the change as a whole — the core value.
- **Blast radius is bounded** to the few call sites that must act on all repos. `all_repos()` is the seam; everything else keeps using the primary scalars.
- **`Task.status` becomes a roll-up.** With per-repo `pr_url`, the scalar status reflects the aggregate (e.g. still `pr-open` until every repo's PR has merged). Per-repo PR state is shown by `gw pr status`.
- **Teardown is per-repo.** Removing the task removes N worktrees; the uncommitted-changes guard fires independently for each, so a dirty secondary blocks removal just as a dirty primary does today.
- **The primary is privileged.** It owns the task file and the Linear anchor. Re-homing a task to a different primary is not supported; you'd remove and recreate.
- **`add-repo` requires a clean primary worktree** because it relocates that worktree into the workspace. This is a one-time cost per task and is guarded, not silent.

## Alternatives considered

- **Workspace of symlinks** (worktrees stay in each repo's canonical `.worktrees/`, the workspace is symlinks). Rejected as the default: it avoids `git worktree move` on `add-repo`, but symlinked roots occasionally confuse tooling and editors, and the real-directory layout is what both humans and agents expect. The relocation cost is one guarded `git worktree move`, paid once.
- **Primary cwd + per-agent `--add-dir`.** Rejected as the load-bearing mechanism: it is Claude-specific and would require threading an `extra_dirs` concept through the `Agent` protocol for a flag the other agents don't have. Kept only as an optional enhancement layered on top of the workspace.
- **A separate `TaskGroup` linking N single-repo tasks.** Rejected: a session spans repos, so it can't belong to any one sub-task — sessions would have to move onto the group, fracturing the model in a worse place. It also contradicts the existing "multiple sessions per *task*" framing and multiplies task ids.
- **Replace the scalars with `repos: list[TaskRepo]` outright.** Rejected for v1 on risk: it forces a `model_validator(mode="before")` upconvert and touches every reader of `task.project`/`.branch`/`.worktree_path`. The primary-plus-secondary shape gets the same capability with zero migration and a narrow blast radius. Revisit if the asymmetry becomes a maintenance burden.
- **Move multi-repo tasks to a global task tier** (XDG data dir) for symmetric ownership. Rejected for v1: it breaks the documented two-tier storage invariant and splits task I/O into two code paths keyed on repo count. Primary-owned keeps one path.
