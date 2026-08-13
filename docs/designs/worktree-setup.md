# Worktree Setup

Current-state design of the bootstrap `gw` applies to a worktree it just created.
Decision record: [ADR 0007](../adrs/0007-declarative-worktree-setup.md).

## The problem

`git worktree add` gives you tracked files and nothing else. Every gitignored
artifact a project depends on — `.env`, `.venv`, `node_modules`, generated local
config — is absent. An agent spawned into that directory starts by rediscovering
the project's bootstrap, and a wrong guess is expensive: it may install the wrong
toolchain, or run tests that fail for reasons that have nothing to do with the
task.

## The shape

Three ordered lists, declared once:

```toml
[setup]
copy = [".env", ".env.local", ".claude/settings.local.json"]
link = ["node_modules"]
run  = ["uv sync --extra dev"]
timeout_seconds = 600
```

| List | Semantics |
|---|---|
| `copy` | Path relative to the project root, copied to the same relative path in the worktree. Directories copy recursively (`dirs_exist_ok=True`, symlinks preserved). A missing source is a `skipped` step, not a failure. |
| `link` | Same resolution, but a symlink pointing back at the project's copy. For things that are large, rebuildable, and identical across worktrees. An existing file or symlink at the destination is replaced; a real directory is not. |
| `run` | Executed in the worktree, after `copy` and `link`. A `str` runs via `sh -c`; a `list[str]` is exec'd directly with no shell. |

`run` steps get `GW_PROJECT`, `GW_PROJECT_ROOT`, `GW_WORKTREE`, and `GW_TASK_ID`
added to the inherited environment, so a bootstrap script can locate both ends
without being told.

## Where the config comes from

`worktree_setup.load_setup(project)` resolves two tiers:

```
<project_root>/.goblin/setup.toml   present? → use it, whole
                                    absent?  → global [setup] in config.toml
```

The project file wins outright rather than merging — the same rule
`prompt_addition` applies to `prompt.md`, and for the same reason: there is no
sane way to spell "drop the global `copy` entry here". It accepts either a bare
table or one nested under `[setup]`, so a snippet moves between the two files
unedited.

`SetupConfig` lives in `config.py` alongside the rest of the schema. Its Python
fields are `copy_paths` / `link_paths` with `copy` / `link` aliases, because a
field literally named `copy` shadows `BaseModel.copy`. Validation is by alias
(`populate_by_name=True`) and `config.dump_toml_dict` serializes by alias, so
`gw config show|get|set setup.copy` all speak the TOML spelling.

## Containment

`copy`/`link` entries are strings from a config file that a repository can ship,
so they are treated as untrusted paths. `worktree_setup.resolve_inside` refuses:

- absolute paths,
- `.` and anything whose normalized form starts with `..`,
- any entry whose **resolved** path is not under the resolved project root —
  which is what catches a symlink pointing outside.

Every entry is resolved up front, before the first byte is copied, so a bad entry
fails with nothing half-applied and the error names the entry rather than
whatever the filesystem complained about first. A separate check refuses a source
that *contains* the destination worktree, since `.worktrees/` lives under the
project root.

`run` has no equivalent boundary. It is a command line; constraining it would be
theatre.

## Where it fires

Setup runs wherever a worktree is materialized:

| Command | Behaviour |
|---|---|
| `gw new` (`--linear`/`--issue`/`--pr`/`--branch`/`--branch-name`/`--branch-auto`) | Runs after the task record is saved and after any `--with-project` repos are attached, before the agent launches. |
| `gw new --dir` | Does not run. The checkout is the user's own, already bootstrapped. |
| `gw <LINEAR-ID>` / `gw gh-<N>` | Same as `gw new`; the shorthand rewrites to it. |
| `gw scratch` | Runs against the scratch container's root (`~/goblin/scratch`), so a `.env` dropped there is copied into every new space. |
| `gw task add-repo` | Runs for the newly attached repo, using *that* project's config. |
| `gw task setup <id>` | Re-runs the steps by hand, for every repo on the task or just `--repo <name>`. |

`--no-setup` skips it on `gw new`, `gw scratch`, and `gw task add-repo`.

`commands/new.Created` is what makes the `--dir` exclusion fall out rather than
being special-cased: each `_from_*` helper returns its `Task` alongside a
`materialized` flag, false when the worktree directory was already on disk.

Ordering matters in one place: `--with-project` repos attach *before* setup runs,
because `workspace.promote_to_workspace` relocates the primary worktree. Setup
populates where the agent will actually be launched, not the path it was moved
out of.

## Reporting

Every step produces a `Step(kind, target, status, detail, output)` with status
`ok`, `skipped`, or `failed`. All of them are printed as they happen and appended
to `~/.local/share/goblin-watcher/logs/setup.jsonl`, one JSON object per step,
carrying the run id, project, worktree, and — for failures — the captured output
(truncated at 8k chars).

A failed `run` step marks the remaining ones `skipped` and stops. The caller
turns that into a `GoblinError` via `worktree_setup.setup_failure`, which names
the failing step and points at both `gw task setup <id>` and the journal. `gw new`
therefore exits non-zero *without launching the agent*: an agent working in a
half-built worktree is the failure mode this whole surface exists to prevent.

The task record and the worktree both survive that error, so the recovery loop is
fix the cause → `gw task setup <id>` → `gw run <id>`.
