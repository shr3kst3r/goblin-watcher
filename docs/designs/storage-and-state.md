# Storage and State

Current-state design of how `goblin-watcher` persists projects, tasks, and configuration.

## Two tiers

```
┌─────────────────────────────────────────────────┐
│ Global tier (per-user, machine-wide)            │
│   ~/.local/share/goblin-watcher/                │  XDG_DATA_HOME
│     state.json         ← project registry       │
│     state.lock         ← registry lock (ADR 0004)
│     workspaces/<task>/ ← multi-repo task workspaces
│     sync/              ← background-sync state + indicator cache
│     logs/              ← commands.jsonl, sync.jsonl, describe.log
│   ~/.config/goblin-watcher/                     │  XDG_CONFIG_HOME
│     config.toml        ← user defaults          │
└─────────────────────────────────────────────────┘
┌─────────────────────────────────────────────────┐
│ Per-project tier (lives with the repo)          │
│   <repo>/.goblin/                               │
│     project.json       ← this project's record  │
│     tasks/<task_id>.json  (one file per task)   │
│   <repo>/.worktrees/                            │
│     <branch>/          ← one worktree per task  │
└─────────────────────────────────────────────────┘
```

Why split this way:

- **Global tier owns the registry.** `gw status`, `gw project ls`, `gw <LINEAR-ID>` all need a list of projects without a `cd` requirement. A single XDG file is the cheapest answer.
- **Per-project tier owns the records.** Project + task JSON live inside the repo so they survive `gw project rm` and aren't lost when the global registry is wiped. They're also multi-machine-portable in principle (sync via the repo) even though that flow isn't documented yet.
- **No SQLite.** JSON files are diff-friendly, atomically writable, and at the scale we're targeting (dozens of projects, hundreds of tasks) the read/write cost is trivial.

## Path resolution

`paths.py` is the single source of truth. Tests redirect XDG via env vars; everything else routes through these helpers:

```python
data_dir()            → $XDG_DATA_HOME/goblin-watcher/
config_dir()          → $XDG_CONFIG_HOME/goblin-watcher/
state_file()          → data_dir()/state.json
config_file()         → config_dir()/config.toml
logs_dir()            → data_dir()/logs/

project_meta_dir(p)   → p/.goblin/
project_tasks_dir(p)  → p/.goblin/tasks/
worktree_root(p)      → p/.worktrees/   (overridable per-project)
workspace_root()      → data_dir()/workspaces/
task_workspace(id)    → data_dir()/workspaces/<id>/   (multi-repo tasks)
```

XDG resolution comes from `platformdirs`, so macOS uses `~/Library/Application Support/...` if `XDG_DATA_HOME` is unset. Tests pin `XDG_DATA_HOME`/`XDG_CONFIG_HOME` to a `tmp_path` so behaviour is deterministic.

## Atomic writes

All JSON files go through `state._atomic_write_text`:

1. `tempfile.mkstemp` in the same directory as the target (so `replace` is atomic on the same filesystem).
2. Write the new contents.
3. `Path.replace(target)`. On POSIX this is a rename; the old file is replaced atomically.
4. On any exception, unlink the temp file and re-raise.

A crash mid-write leaves either the old state or the new one — never a half-written file. That matters because state corruption would mean losing the project registry.

## Cross-process locking

Atomic writes prevent torn files but not *lost updates*: two processes that each
load, mutate, and save the same record produce last-writer-wins. Since ADR 0004,
read-modify-write cycles are serialized with advisory `fcntl.flock` locks:

```python
state.update_task(project, task_id, mutate)   # lock → re-read → mutate → save
state.update_global(mutate)
```

The lock spans the **read**, so `mutate` always sees current on-disk state.
Locks are taken on stable sidecar files — `<tasks>/.<task_id>.lock` and
`data_dir()/state.lock` — never on the data file, whose inode every atomic write
replaces. Sidecars are empty, persist between runs, and are excluded from task
discovery by the `*.json` glob.

Callers pass **narrow patches** that touch only the fields they own. Persisting a
whole `Task` loaded minutes earlier (the old launcher pattern) would revert
whatever else landed meanwhile. Expensive work — network calls, transcript
parsing, agent-store globbing — must stay outside the callback, which is why
session reconciliation is split into `plan_reconciliation` / `apply_reconciliation`.

`state.write_json_atomic` is the public seam for modules that keep their own JSON
files (the sync tier) and want the same guarantee.

## Schemas

### GlobalState (state.json)

```json
{
  "schema_version": 1,
  "projects": {
    "eng": "/Users/you/code/eng-repo",
    "scratch": "/Users/you/code/scratch"
  }
}
```

- `schema_version` exists so a future migration runner has something to dispatch on. No migrations yet.
- `projects` is a flat `name → root` map. The full Project record is loaded lazily from `project.json` inside that root.
- There is no "current project" pointer. Commands that need a project accept `--project NAME`; when omitted, `task_resolver.resolve_project` opens an interactive picker (auto-picking when only one project is registered). Commands that want to operate over every project just iterate `state.load_global().projects` directly.

### Project (.goblin/project.json)

```json
{
  "name": "eng",
  "root": "/Users/you/code/eng-repo",
  "repo_url": "git@github.com:org/eng.git",
  "default_branch": "main",
  "branch_prefix": "",
  "worktree_root": null,
  "default_agent": null,
  "linear_team_key": "ENG",
  "created_at": "2026-05-18T13:00:00+00:00"
}
```

- `branch_prefix` lets a project enforce a naming convention (e.g. `goblin/eng-123-...`).
- `worktree_root = null` means use `<root>/.worktrees/`. Set it explicitly to relocate worktrees (e.g. to an external SSD).
- `default_agent` overrides the user-level default for tasks in this project.
- `linear_team_key` powers auto-resolution from a Linear identifier; see `linear-integration.md`.

### Task (.goblin/tasks/<task_id>.json)

```json
{
  "id": "eng-123",
  "project": "eng",
  "linear": { "identifier": "ENG-123", "title": "...", "url": "...", ... },
  "github_issue": null,
  "branch": "eng-123-add-rate-limit",
  "worktree_path": "/Users/you/code/eng-repo/.worktrees/eng-123",
  "base_branch": "main",
  "pr_url": null,
  "created_at": "2026-05-18T13:05:00+00:00",
  "status": "open",
  "sessions": [
    {
      "agent": "claude",
      "session_id": "abc...",
      "created_at": "...",
      "last_used_at": "...",
      "label": "First user message snippet",
      "summary": "Mapped the existing limiter; planning token-bucket impl",
      "turn_count": 12,
      "summary_updated_at": "...",
      "transcript_path": "/Users/you/.claude/projects/-Users.../abc....jsonl"
    }
  ]
}
```

- `sessions` is a list, **not** a dict keyed by agent — see `sessions-and-windowing.md` for why.
- `linear` / `github_issue` snapshots are taken at task-creation time. Only the workflow/open-closed **state** is refreshed afterwards, TTL-gated, by `linear_state.py` and `github_state.py` (timestamps: `linear_state_updated_at`, `github_issue_state_updated_at`). Titles, bodies, and comments stay as captured.
- `github_issue` (default `null`) holds a `--issue`-sourced task's GitHub issue: `number`, `repo` (`owner/repo`), `title`, `body`, `state`, `url`, `labels`, `assignees`. `repo` is the *issue's* repo, which may differ from the task's project for a cross-repo tracking issue — that's what `gw pr open` checks to decide between `Closes #42` and `Closes owner/repo#42`.
- `parent_task` (default `null`) records the task this one is **stacked on** — set at creation time when `base_branch` turned out to be another task's primary branch (`gw new --from`, or a `--pr` whose base is tracked). It stores an *id*, not a branch name, because the branch disappears when the parent lands. Deliberately **not** a validated reference: the parent is routinely pruned, and readers (`gw status`, `gw task show`, `gw pr open`) treat a dangling id as "no longer tracked" rather than an error. `gw task rename` repoints children so a record-only rename doesn't orphan them.
- `pr_url` is set by `gw pr open` and `gw pr status`.
- `status` transitions: `open` → `pushed` (planned) → `pr-open` → `merged` | `closed` | `abandoned`. Today only `open` and `pr-open` are set automatically. For a multi-repo task it is a roll-up across repos.
- `secondary_repos` (default `[]`) and `workspace_path` (default `null`) support multi-repo tasks (ADR 0003). The scalar `project`/`branch`/`worktree_path`/`base_branch`/`pr_url` fields describe the **primary** repo; each additional repo is a `TaskRepo` (same five fields) in `secondary_repos`. `Task.all_repos()` yields primary-first. Single-repo task JSON is unchanged — the new fields default empty, so older records validate without migration.
- When `secondary_repos` is non-empty, `workspace_path` points at the workspace directory (below) that holds each repo's worktree as a subdir, and is the agent's cwd.
- `archived` (default `false`) and `archived_at` (default `null`) record that the task's worktree was dropped but everything else kept — see "Archived tasks" below. Deliberately separate from `status`: a task can be archived at any point in the PR lifecycle, and `status` must go on tracking the PR.

## Archived tasks

A worktree is the expensive part of a task — a full checkout each, which adds up fast across many parallel tasks — while the branch, the record, and the session history cost almost nothing. `gw task archive <task-id>` removes only the checkout:

- every repo's worktree goes through `git.worktree_remove` (falling back to `shutil.rmtree` only under `--force`, the same guard `destroy_task` uses so untracked work isn't silently lost);
- a multi-repo task's `workspace_path` is `rmdir`'d, and only if it came out empty — anything the user parked next to the checkouts survives;
- the record is patched to `archived = true` with an `archived_at` stamp, under the task lock like every other write.

`gw run` is the inverse. When it resolves an archived task it calls `rematerialize_task` before anything else touches the filesystem: `git worktree prune` (a checkout deleted behind git's back leaves the path registered, which would make `worktree add` refuse), then `git worktree add <path> <branch>` per repo, then the project's `[setup]` steps against the restored checkouts, since a fresh worktree is a bare one again (ADR 0007). The record's `archived` flag is cleared on the way through. If the branch is gone by then, it raises rather than checking out the base branch — that would hand back an empty worktree wearing the task's name.

Two consumers know about the flag:

- `gw status` renders an archived task dimmed with an `(archived)` tag. Its git-indicator helper already returns nothing for a missing worktree, so no separate check is needed there.
- `gw sync` skips the per-repo git facts (uncommitted, ahead counts) for an archived task. PR state and checks still refresh: the branch and the PR outlive the worktree, so a task parked mid-review keeps reporting.

Scratch tasks are refused — a scratch directory *is* the work, with no branch to come back from — and `gw task rm` is unaffected: it already skips worktrees that aren't on disk and deletes the branch and record as usual.

## Configuration (config.toml)

Hand-edited TOML; pydantic-validated on load.

```toml
[defaults]
agent = "claude"
windowing = "inline"
summary_ttl_seconds = 30

[linear]
api_key = "op://Personal/Linear/api_key"

[tmux]
session_name = "goblin"
attach_on_spawn = true
split = "vertical"
```

`config.save()` uses `model_dump(exclude_none=True)` because TOML doesn't serialize `null`. This means saving a Config with `api_key = None` omits the line entirely — round-tripping is lossy in that one direction, by design.

The user can hand-edit `config.toml` and it'll load fine as long as the values pass pydantic validation. Unknown keys (top-level or in sub-sections) are rejected because pydantic models default to forbidding extras — that catches typos.

## Touching the host repo carefully

When `gw project new` registers a repo, it appends `.goblin/` and `.worktrees/` to `<repo>/.git/info/exclude`. This is the user's local-only ignore file; it never appears in `git status` output, never goes into a commit, and never touches the tracked `.gitignore`.

We do this to keep `.goblin/` and `.worktrees/` invisible to the host repo's tooling without forcing the team to merge a `.gitignore` change. It's idempotent — re-running `gw project new` on the same repo doesn't duplicate the lines.

## Task discovery

`state.list_tasks(project)` globs `<root>/.goblin/tasks/*.json` and returns successfully-parsed records. Files that fail validation are skipped (so a stray file in `tasks/` doesn't crash `gw status`). There's no index file; task listings are O(n) over the task files on disk, which is fine for any human-scale project.

`state.find_task_by_worktree(project, path)` walks `list_tasks` looking for a worktree match — used by `gw run` to resolve `cwd → task`.

## Migration model (none yet)

`GlobalState.schema_version = 1` is the only versioned schema today. Plan: when v2 is needed, add a `state/migrations.py` that dispatches on the version field and rewrites in place. Per-project records (`project.json`, task JSON) don't carry version fields yet — if they ever need migration we'll add them then.

## Tests

- `tests/test_state.py` — global state round-trip, register/unregister, atomic-write leaves no temp files.
- `tests/test_paths.py` — XDG resolution, project meta dirs, worktree root override.
- `tests/test_cli_project.py` — full project CRUD via the CLI.
- `tests/test_cli_task.py` — task lifecycle inside a project.
