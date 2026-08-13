# 0007. Declarative worktree setup, not a setup script

- Status: accepted
- Date: 2026-08-13

## Context

A git worktree is a bare checkout. Everything gitignored — `.env`, `.venv`,
`node_modules`, generated config — is absent, and nothing in `gw` put it there.
Every agent `gw` spawns therefore begins by rediscovering the project's
bootstrap, or by failing at it and guessing. `gh-14` called this the single
biggest gap versus Conductor.

The obvious shape is "run a setup script". That raises three questions we had to
answer before writing any of it:

1. **Where does the config live?** `gw`'s config is user-wide
   (`~/.config/goblin-watcher/config.toml`), but a bootstrap is per-project:
   `uv sync --extra dev` is wrong for a Node repo. Meanwhile `copy = [".env"]` is
   close to universal and belongs in the user-wide file.
2. **Script or declaration?** A `setup.sh` in the repo is maximally flexible and
   maximally opaque: `gw` could not tell a copy from a curl, could not report
   which part failed, and could not enforce any boundary on what it touched.
3. **What stops it reaching outside the project?** Copying `.env` into a worktree
   means resolving a config-supplied path against the project root. `copy =
   ["../../.ssh/id_rsa"]` walks straight out of the safety boundary in AGENTS.md,
   and `.goblin/setup.toml` can be committed to a repo, so the path string is not
   always the local user's.

## Decision

**Declare the bootstrap as data, not as a script.** Three lists, applied in
order:

```toml
[setup]
copy = [".env", ".claude/settings.local.json"]
link = ["node_modules"]
run  = ["uv sync --extra dev"]
```

`copy` and `link` are relative paths reproduced at the same relative path inside
the worktree; `run` entries execute in the new worktree, a string via `sh -c` and
an argv list exec'd directly. A repo that genuinely needs a script still gets one
— it writes `run = ["./scripts/bootstrap.sh"]` — but the common cases stay
inspectable, and `gw` can name the step that failed.

**Two config tiers, project wins whole.** The global `[setup]` table applies to
every project; a `<project_root>/.goblin/setup.toml` replaces it outright. This
is the same "presence overrides" rule `prompt.md` already uses. Merging was
rejected: there is no sane spelling for "drop the global `copy` entry for this
one project".

**`copy`/`link` paths are contained by construction.** Every entry goes through
`worktree_setup.resolve_inside`, which refuses absolute paths, `..` components,
and — because it checks the *resolved* path — symlinks whose target sits outside
the project root. Sources are also refused when they contain the destination, so
no entry can ask us to copy a worktree into itself. `run` gets no such boundary:
it is a command line, and pretending otherwise would be security theatre.

**A failed setup stops the spawn.** Every step's outcome, and the captured output
of every failed command, is journaled to `logs/setup.jsonl` and printed. A
failing `run` step skips the remaining ones and raises, so `gw new` reports the
error instead of launching an agent into a half-built worktree. The task record
and worktree survive; `gw task setup <id>` re-runs the steps once the cause is
fixed.

**Setup fires at materialization.** `commands/new.Created` now carries a
`materialized` flag alongside the task, so the sources that adopt a checkout
already on disk (`--dir`, and the re-adopt path in `--branch`/`--pr`) are left
alone. `--no-setup` opts out per invocation.

## Consequences

- A spawned agent lands in a worktree that already has its secrets, its
  dependencies, and its local settings. The seed prompt no longer has to explain
  the bootstrap, and the agent no longer has to guess at it.
- `gw` gains a subprocess it does not control the contents of. The per-step
  `timeout_seconds` cap (default 600) is what keeps a hung bootstrap from
  blocking a spawn forever.
- `SetupConfig`'s Python fields are `copy_paths` / `link_paths` with `copy` /
  `link` aliases, because `copy` alone shadows `BaseModel.copy`. `config.load()`
  validates by alias and `config.dump_toml_dict` serializes by alias, so the TOML
  surface is unaffected — but anything walking `model_fields` (like
  `gw config set`) has to match aliases too.
- The journal is a third append-only log next to `commands.jsonl` and
  `sync.jsonl`. It has no prune command yet; setup runs are far rarer than sync
  passes, so that can wait for evidence it's needed.
- A repo can now ship a `.goblin/setup.toml` that runs commands on checkout.
  That is the same trust posture as a repo's `justfile` or `package.json`
  scripts, and `gw`'s default `unsafe = true` already spawns agents with
  permission prompts bypassed — but it is a new file to look at when reviewing an
  unfamiliar repo.

## Alternatives considered

- **A `setup.sh` hook in the repo.** Rejected: opaque to `gw`, so no per-step
  reporting and no containment boundary at all. Available anyway via `run`.
- **Global config only.** Rejected: the `run` list is inherently per-project.
- **Merging the two tiers.** Rejected: no way to subtract a global entry.
- **Copying every gitignored file automatically.** Rejected: `.venv` and
  `node_modules` are large and often architecture-specific, and "every gitignored
  file" includes build output nobody wants duplicated per worktree.
- **Warn and launch anyway on failure.** Rejected: a half-built worktree that
  looks fine is the exact failure this feature exists to prevent, and an agent
  is much worse than a human at noticing it.
