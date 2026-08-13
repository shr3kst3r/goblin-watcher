# AGENTS.md

Onboarding for AI coding agents (Claude Code, Codex, Gemini) and humans.

## What this is

**goblin-watcher** (`gw`) is a CLI that orchestrates AI coding agents in git worktrees. It replaces tools like Conductor and Superset. Four entry points:

- `gw <LINEAR-ID>` — auto-pilot: clone/find repo, create branch + worktree from a Linear ticket, spawn the agent. `gw gh-<N>` is the same thing for a GitHub issue in the current repo (that pattern is claimed before the Linear one, so a Linear team keyed `GH` must use `gw new --linear GH-42`).
- `gw new [--linear|--issue|--pr|--branch|--branch-name|--dir]` — explicit task creation from any source. `--pr` takes a GitHub PR number or URL and checks out its head branch (a URL also auto-resolves the project by repo). `--issue` takes a GitHub issue as `42`, `owner/repo#42`, or a URL; the task id is `gh-<N>` and the qualified forms support a tracking issue that lives outside the repo being worked in.
- `gw run [PATH|TASK-ID]` — open a session picker for an existing task.
- `gw scratch [NAME]` — a scratch space: a plain directory (no git repo, no project) at `~/goblin/scratch/<name>` with tracked, resumable sessions. Backed by the reserved `scratch` project (`Project.kind/Task.kind = "scratch"`); git/PR-flavored commands skip or reject scratch tasks. Clean up idle spaces with `gw task prune --scratch-older-than <days>`.

Plus `gw sync` — a short-lived, idempotent background pass (Linear + session refresh, PR/CI state, cached git indicators, safe prune, edge-triggered notifications), scheduled via launchd by `gw sync install`. Not a daemon. `gw sync watch` follows it live; `gw sync status` reports installation and component health. See ADR 0005 and `docs/designs/background-sync.md`.

Multiple sessions per task are allowed (e.g. two claude conversations on the same Linear ticket). Each session carries a rolling summary derived from the agent's transcript.

## Docs and scratch

- Durable docs live under `docs/`. ADRs at `docs/adrs/`, current-state designs at `docs/designs/`. See `docs/AGENTS.md` for conventions.
- Ephemeral agent artifacts (investigation notes, plans, assessment reports) go in `.context/`. This directory is gitignored.

## Safety

- Never commit secrets. `LINEAR_API_KEY` lives in your shell or in `~/.config/goblin-watcher/config.toml` (literal or `op://...` reference). `.env*` is gitignored as a fallback.
- **`defaults.unsafe = true` is the default**: agents launch with their bypass-permission flag (e.g. claude's `--dangerously-skip-permissions`) unless you set `unsafe = false` in config (`gw config set defaults.unsafe false`) or pass `--no-unsafe`. Deliberate — gw is built for parallel autonomous agents — but know what you're opting into.

## Architecture map (top-level)

```
src/goblin_watcher/
├── activity.py            # working / needs-you / done, classified from the transcript tail (ADR 0010)
├── cli.py                 # Typer app + custom dispatcher for `gw <LINEAR-ID>`
├── command_log.py         # JSONL audit log of every `gw` invocation
├── console.py             # Rich Console singleton + colored agent badges
├── errors.py              # GoblinError (root) + ProjectNotFoundError, TaskNotFoundError, ...
├── config.py              # TOML config at $XDG_CONFIG_HOME/goblin-watcher/config.toml
├── locks.py               # advisory fcntl.flock on sidecar files (ADR 0004)
├── state.py               # JSON persistence: global registry + per-project tasks
├── linear_state.py        # TTL-cached Linear workflow-state refresh (status + sync)
├── github_state.py        # TTL-cached GitHub issue-state refresh (status + sync)
├── paths.py               # XDG resolution + per-project paths
├── models.py              # Pydantic: LinearIssue / GhIssue / Task / Project / SessionRecord / GlobalState
├── slug.py                # Branch / task-id slugification
├── git.py                 # Thin subprocess wrapper around git (clone, worktree_*, push, ...)
├── gh.py                  # Thin wrapper around the `gh` CLI for PR + issue ops
├── secrets.py             # Linear API key resolution (env → config → `op://...`)
├── sessions.py            # SessionRecord rolling-summary refresh + upsert
├── modes.py               # `gw new --mode` registry: built-in + user-defined work modes (ADR 0009)
├── review_feed.py         # PR review threads + failing-check logs for `gw run --address-review`
├── drift.py               # state-drift detection + the safe repairs behind `gw doctor --repair`
├── usage.py               # token rollups + list-price cost estimates (docs/designs/token-usage-and-cost.md)
├── workspace.py           # multi-repo task workspaces (promote + attach repos)
├── worktree_setup.py      # [setup] copy/link/run bootstrap applied to new worktrees (ADR 0007)
├── picker.py              # questionary-backed interactive session picker
├── linear/                # GraphQL client + queries (httpx)
├── agents/                # Agent protocol + claude/codex/gemini/antigravity impls + launcher
├── windowing/             # Windower protocol + Inline + Tmux + Headless impls
├── sync/                  # background sync: engine, journal, indicator cache, notify, launchd
├── commands/              # Typer subcommand modules (project / task / session / pr / new / run / scratch / status / sync / doctor / history / version)
└── templates/spawn_prompt.md
```

## Conventions

- **Type hints everywhere.**
- **Pydantic** models at all serialization boundaries. JSON dumps via `model_dump(mode="json", exclude_none=True)`.
- **Rich Console**, never `print`. User-facing errors go through `GoblinError`.
- **Shell-out args are always `list[str]`.** Never f-string a command for `subprocess`.
- **Atomic writes**: state JSON via temp file + `Path.replace()`.
- **Branch ops** go through `git.py`. Don't reach into `git._run` from elsewhere.
- **Agent registry is static.** Don't add a plugin system.
- **Two-tier storage**: global registry under `$XDG_DATA_HOME/goblin-watcher/`; per-project records under `<project_root>/.goblin/`. Worktrees at `<project_root>/.worktrees/<branch>/`. New clones (via `--repo`) land in `~/goblin/<project>/`; `--dir` adopts an existing checkout in place. Never touch the user's tracked `.gitignore` — append patterns to `.git/info/exclude` instead.

## Toolchain

[asdf](https://asdf-vm.com/) manages local tool versions. `.tool-versions` pins `python 3.14.0` and `just 1.40.0`. Fresh setup:

```
asdf install              # ensures pinned Python + just are installed
uv sync --extra dev
just hooks                # install pre-commit hooks (.pre-commit-config.yaml)
```

`uv` uses the asdf-managed Python (`[tool.uv] python-preference = "only-system"` in pyproject.toml prevents uv from downloading its own).

## Local verification (run these after any change)

```
uv run ruff check .
uv run ruff format --check .
uv run ty check src
uv run pytest -q
```

Or, one shot: `just verify`.

`just` is the canonical task runner. `Makefile` is not provided.

There is no CI in this repo — no GitHub Actions workflow, no external checks on a PR. `just verify` is the only gate, so run it locally and get it green before you push.

## Safety boundaries

- Never `git push --force` on `main` / default branches. For feature branches, only `--force-with-lease` — and only at user request.
- Never delete a worktree with uncommitted changes unless `--force` is passed (`gw task rm` and `gw task archive` both enforce this).
- Never write outside: `<project>/.goblin/`, `<project>/.worktrees/`, the user's XDG dirs, or the project's working tree itself.
- **Never call a branch merged from the commit graph alone.** A branch with no commits yet is an ancestor of its base and has nothing unique on it — indistinguishable from a merged one — so ancestry-based prune deleted brand-new tasks the moment anyone else landed on the base branch (#46). `merge_detection` takes the ancestry path only when `Task.fork_sha` is recorded *and* the branch tip has moved off it; a missing `fork_sha` means "unknown", never "no commits yet". Prefer PR state, which is the only detection that works on a squash-merge repo at all.
- **Linear API is read-only by default.** Posting a comment requires the explicit `--notify-linear` flag on `gw pr open`.
- 1Password `op` references resolve lazily; only fetched when actually needed.

## State drift (`gw doctor --repair`)

`drift.py` detects the ways gw's records diverge from git and the filesystem — an untracked worktree under `<project>/.worktrees/`, a task whose worktree or branch vanished, `.git/info/exclude` missing gw's patterns, indicator-cache rows for dead tasks — and `commands/doctor.py` renders them as one summary check plus a detail table. Any drift makes `gw doctor` exit non-zero. See `docs/designs/storage-and-state.md`.

Two rules when extending it:

- **Repair is only for fixes that cannot lose work.** `drift.REPAIRABLE_KINDS` is the whitelist and `Finding.__post_init__` enforces it, so a new kind cannot quietly opt itself into being auto-fixed. Anything holding commits or files (an untracked worktree, a task whose branch survived its worktree) stays a report — the same posture as sync's prune, which refuses to force-delete a branch it can't prove is safe.
- **Detection is read-only and must not raise.** A project whose root or repo is unreadable is skipped, not crashed on: a drift report that dies on the first broken project is worse than one that skips it.

## Worktree setup

A new worktree is a bare checkout, so `worktree_setup.py` applies a declared bootstrap to it: `copy` (gitignored files pulled in from the project root), `link` (symlinks for the big rebuildable ones), then `run` (commands executed in the new worktree). Config comes from the global `[setup]` table, or from `<project_root>/.goblin/setup.toml` when the project has one — the project file replaces the global table whole, mirroring `prompt.md`. See ADR 0007 and `docs/designs/worktree-setup.md`.

Two invariants when touching this:

- `copy`/`link` entries go through `worktree_setup.resolve_inside`, which refuses absolute paths, `..` components, and symlinks resolving outside the project root. Don't add a code path that joins a config-supplied path without it.
- Setup runs where a worktree is *materialized* — that's what `commands/new.Created.materialized` tracks. Sources that adopt an existing checkout (`--dir`) don't run it. `commands/task.rematerialize_task` is the other materializing path, so it runs setup too.

## Archiving

`gw task archive <task-id>` drops a task's worktree and keeps everything else — record, branch, session history — because the checkout is the expensive part when many tasks run in parallel. `Task.archived` / `Task.archived_at` carry the flag; it is orthogonal to `Task.status`, which goes on tracking the PR.

`gw run` on an archived task calls `commands/task.rematerialize_task` first: `git worktree prune`, `git worktree add <path> <branch>` per repo, then the project's setup steps. Two consumers read the flag — `gw status` dims the row, and `gw sync` skips the per-repo git facts (PR state still refreshes, since the branch outlives the worktree). Scratch tasks are refused: the directory is the only copy of the work. See `docs/designs/storage-and-state.md`.

## Adding a work mode

A **work mode** changes the agent's standing brief without changing the task (ADR 0006). `gw new --mode <name>` selects one from the registry in `modes.py`; `--research` and `--adversarial-review` are aliases kept for compatibility. See ADR 0009 and `docs/designs/task-sources.md`.

To add one:
1. Add a `ModeSpec` entry to `modes.BUILTIN_MODES` — exactly one of `template` (rendered through `build_seed_prompt` with the shared slots) or `seed` (a literal first message, for slash commands that must be the whole user message). Optional: `agent`, `requires_ticket`, `focus_lead`, `summary`, `suggest_when`.
2. Drop the template in `templates/` if it's a template mode. Slots: `{ticket_id}`, `{title}`, `{repos_block}`, `{description}`, `{addition_block}`, `{focus}`.
3. Tests in `tests/test_modes.py` (the spec) and `tests/test_cli_new_sources.py` (the CLI).

Two invariants when touching this:

- **No consumer branches on a mode's name.** Every check reads a field. `allows_prompt` is *derived* from the shape (a seed mode has no `{focus}` slot), not configured, so user modes inherit the refusal. If a new mode needs behaviour no field expresses, add the field — don't special-case the name.
- **A mode contributes prompt text and policy flags, never code.** `[modes.<name>]` in the user's `config.toml` is a supported extension point; letting it name a command to run would make it the plugin system this file forbids. A mode that needs a data-gathering step (like `--address-review`'s review feed) belongs in gw, not in the registry.

## Agent activity states

What an agent is doing is classified from the *shape of its transcript's tail*, never from file mtime (ADR 0010). `activity.classify(session)` is the single entry point; `gw status`'s badge and `gw sync`'s notifications both call it, so they can't disagree. Five states, and the names are load-bearing — other surfaces key off them:

| state | meaning | badge | sync event |
| --- | --- | --- | --- |
| `working` | tool call outstanding, or the turn is the agent's and unanswered | `● working` | — |
| `needs-you` | last turn ended on a question | `◆ needs you` | `agent-needs-you` |
| `done` | last turn finished, nothing pending | `✓ done` | `agent-done` |
| `idle` | quiet, and the transcript can't say why | `idle <age>` | `agent-idle` |
| `unknown` | no transcript on disk | *(none)* | — |

Four things to keep true when touching this:

- **Agents report shape, not meaning.** `Agent.read_tail(path)` returns a `TranscriptTail` (pending tool call, who spoke last, the tail of the final assistant turn). Naming a state from it — including the question heuristic — lives in `activity.py`, written once. Don't push classification down into an agent module.
- **`read_tail` takes a path, and reads a bounded window.** It's called per session per render, and `gw status --watch` renders every two seconds. Use `agents/_tail.py`; never parse a whole transcript, and never route it through `read_transcript`'s `(session_id, cwd)` lookup (codex re-globs `~/.codex/sessions`).
- **Nothing is persisted.** Classification is a pure function of the file on disk. Don't add a `SessionRecord` field caching it.
- **Every degradation lands on `idle` or `unknown`.** An unparseable record, an unknown agent, a drifted format — all return None from `read_tail` and fall back to mtime. A transcript-reading bug must never break `gw status`.

## The ticket check

`gw new` runs one cheap-model read of the ticket (`classify.advise`) after the task exists and before the agent launches: it prints a suggested `--mode` and up to three ambiguities. Two invariants, both load-bearing (ADR 0011):

- **It is advisory and it fails open.** It never changes the task, the mode, the agent, or the seed prompt, and `advise` swallows every exception — a missing binary, a timeout, malformed output. It runs after the branch and worktree are on disk, so a failure has nothing left to protect. Don't add a code path where a classification result decides anything.
- **Suggestable modes are `ModeSpec.suggest_when`, never a name.** A mode with no `suggest_when` is never suggested, and a returned name that isn't in the candidate list is dropped at parse time.

`description.run_llm` is gw's only cheap-model call; classification shares the `description_agent` / `description_model` pair rather than adding a second LLM surface. Off switches: `--no-classify`, `defaults.classify_tickets`, `GW_CLASSIFY=off`, or `description_agent = "off"`. `tests/conftest.py` sets `GW_CLASSIFY=off` in `isolated_xdg` — **no test may reach a real model**; unset it deliberately and patch `description.run_llm` when testing this path.

## Adding an agent

The registered set is `claude`, `codex`, `gemini`, `antigravity` (Google Antigravity's `agy` CLI), plus `managed` (scaffold; see ADR 0002 and `docs/designs/sessions-and-windowing.md`). To wire another:
1. Create `src/goblin_watcher/agents/<name>.py` with `spawn_command`, `headless_command`, `resume_command`, `capture_session_id`, `list_sessions`, `read_transcript`, `read_tail`, `env`.
2. Declare `transcripts: TranscriptCapability` on the class — `PARSEABLE_TRANSCRIPTS` if `read_transcript` / `render_transcript` are real, otherwise `TranscriptCapability(parseable=False, reason="…")`. Stubbing them silently empties out session summaries, descriptions, turn counts, and the transcript-derived activity states (such an agent can only report `working`/`idle` off mtime, never `needs-you` or `done`); the declaration is what `gw doctor` warns from, so it has to be honest. `tests/test_agents_transcript_capability.py` asserts the declaration matches what the methods actually return.
3. Add it to `models.AgentName`.
4. Add it to `agents/registry.registry`.
5. Add a row to `commands/doctor.py`'s binary check list. Agents with no local binary (e.g. `managed`) get a custom check function instead. The per-agent transcript row is generated from the registry — no doctor edit needed for that one.
6. If the agent has project-level prerequisites (e.g. `managed` requires a remote), extend `agents.registry.validate_agent_for_project` and call it from `commands/new.py` and `commands/run.py`.
7. Tests under `tests/test_agents_<name>.py`.

Resist adding entry-point discovery or a plugin system — keep it static.

## Tmux mode

`windowing = "tmux"` in `config.toml` (or `--windowing tmux`) routes spawns through `TmuxWindower`:
- One tmux session (default name `goblin`) hosts everything.
- One window per task, named after `task.id` (e.g. `eng-123`).
- One pane per agent session (additional sessions on the same task `split-window`). `tmux.split = "vertical"` (default) stacks panes top/bottom; `"horizontal"` is side-by-side.
- `attach_on_spawn = true` (default) means `gw` will `os.execvp` into `tmux attach` if invoked from outside tmux. From inside, it uses `tmux switch-window`.
- Each pane is stamped with its session id (`@gw_session` pane option) at creation, which is how `gw session send <task-id> "…"` finds the right pane to type into. tmux holds that mapping for the pane's lifetime — gw stores no pane ids.
- `mark_idle = true` (default `false`) sets `monitor-silence <mark_idle_seconds>` on each task window — when the agent goes quiet (done or waiting for input), tmux marks the window with a `~` in the status bar. No bell, no banner, no focus-stealing. `mark_idle_seconds` defaults to `5`. Caveat: any agent that streams a heartbeat/spinner will never trigger this; verify your agent actually falls silent at its prompt.

Test against a fake `tmux` (see `tests/test_windowing_tmux.py`) — never call the real binary in tests.

## Headless mode

`windowing = "headless"` (or `--windowing headless`) routes spawns through `HeadlessWindower`, for unattended runs from cron, a queue, or another agent. It `Popen`s the agent's print mode (`Agent.headless_command` — `claude -p`, `codex exec`, `agy -p`, `gemini -p`) with `start_new_session=True` and stdin on `/dev/null`, appends stdout+stderr to `<project>/.goblin/logs/<task>-<session>.log`, records the pid in a sibling `.pid`, and returns 0 as soon as the spawn succeeds.

It is deliberately narrow: fresh sessions only (a `Resume` choice is refused), no exit-status capture, and `send` raises. Completion rides on `gw sync`'s edge-triggered `agent-done` notification. Keep `unsafe = true` or the agent stalls at its first permission prompt with nobody to answer. See ADR 0007.

`Windower` declares two booleans the launcher branches on: `detaches` (run returns while the agent is still going, so post-run reconciliation is skipped) and `headless` (no terminal, so build the argv from `headless_command`). Never reintroduce a `windower.name == "..."` check for either.

Patch `subprocess.Popen` in tests (`tests/test_windowing_headless.py`) — never spawn a real agent.

## What not to refactor

- Don't merge `state.py`, `sessions.py`, and `models.py` into one module. The separation tracks the persistence/business boundary.
- Don't replace the questionary picker with custom prompt_toolkit code unless the UX requires it.
- Don't introduce async at the CLI layer; `httpx.Client` (sync) is enough.
