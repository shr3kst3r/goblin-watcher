# Storage and State

Current-state design of how `goblin-watcher` persists projects, tasks, and configuration.

## Two tiers

```
┌─────────────────────────────────────────────────┐
│ Global tier (per-user, machine-wide)            │
│   ~/.local/share/goblin-watcher/                │  XDG_DATA_HOME
│     state.json         ← project registry       │
│     logs/              ← (planned, not used yet)│
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
```

XDG resolution comes from `platformdirs`, so macOS uses `~/Library/Application Support/...` if `XDG_DATA_HOME` is unset. Tests pin `XDG_DATA_HOME`/`XDG_CONFIG_HOME` to a `tmp_path` so behaviour is deterministic.

## Atomic writes

All JSON files go through `state._atomic_write_text`:

1. `tempfile.mkstemp` in the same directory as the target (so `replace` is atomic on the same filesystem).
2. Write the new contents.
3. `Path.replace(target)`. On POSIX this is a rename; the old file is replaced atomically.
4. On any exception, unlink the temp file and re-raise.

A crash mid-write leaves either the old state or the new one — never a half-written file. That matters because state corruption would mean losing the project registry.

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
- `linear` snapshot is frozen at task-creation time. Refetching Linear is out of scope for MVP.
- `pr_url` is set by `gw pr open` and `gw pr status`.
- `status` transitions: `open` → `pushed` (planned) → `pr-open` → `merged` | `closed` | `abandoned`. Today only `open` and `pr-open` are set automatically.

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
