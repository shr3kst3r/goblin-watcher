# AGENTS.md

Onboarding for AI coding agents (Claude Code, Codex, Gemini) and humans.

## What this is

**goblin-watcher** (`gw`) is a CLI that orchestrates AI coding agents in git worktrees. It replaces tools like Conductor and Superset. Three entry points:

- `gw <LINEAR-ID>` — auto-pilot: clone/find repo, create branch + worktree from a Linear ticket, spawn the agent.
- `gw new [--linear|--branch|--branch-name|--dir]` — explicit task creation from any of four sources.
- `gw run [PATH|TASK-ID]` — open a session picker for an existing task.

Multiple sessions per task are allowed (e.g. two claude conversations on the same Linear ticket). Each session carries a rolling summary derived from the agent's transcript.

## Docs and scratch

- Durable docs live under `docs/`. ADRs at `docs/adrs/`, current-state designs at `docs/designs/`. See `docs/AGENTS.md` for conventions.
- Ephemeral agent artifacts (investigation notes, plans, assessment reports) go in `.context/`. This directory is gitignored.

## Safety

- Never commit secrets. `LINEAR_API_KEY` lives in your shell or in `~/.config/goblin-watcher/config.toml` (literal or `op://...` reference). `.env*` is gitignored as a fallback.

## Architecture map (top-level)

```
src/goblin_watcher/
├── cli.py                 # Typer app + custom dispatcher for `gw <LINEAR-ID>`
├── command_log.py         # JSONL audit log of every `gw` invocation
├── console.py             # Rich Console singleton + colored agent badges
├── errors.py              # GoblinError (root) + ProjectNotFoundError, TaskNotFoundError, ...
├── config.py              # TOML config at $XDG_CONFIG_HOME/goblin-watcher/config.toml
├── state.py               # JSON persistence: global registry + per-project tasks
├── paths.py               # XDG resolution + per-project paths
├── models.py              # Pydantic: LinearIssue / Task / Project / SessionRecord / GlobalState
├── slug.py                # Branch / task-id slugification
├── git.py                 # Thin subprocess wrapper around git (clone, worktree_*, push, ...)
├── gh.py                  # Thin wrapper around the `gh` CLI for PR ops
├── secrets.py             # Linear API key resolution (env → config → `op://...`)
├── sessions.py            # SessionRecord rolling-summary refresh + upsert
├── picker.py              # questionary-backed interactive session picker
├── linear/                # GraphQL client + queries (httpx)
├── agents/                # Agent protocol + claude/codex/gemini impls + launcher
├── windowing/             # Windower protocol + Inline + Tmux impls
├── commands/              # Typer subcommand modules (project / task / session / pr / new / run / status / doctor / history / version)
└── templates/spawn_prompt.md
```

## Conventions

- **Type hints everywhere.** `ty` runs in CI.
- **Pydantic** models at all serialization boundaries. JSON dumps via `model_dump(mode="json", exclude_none=True)`.
- **Rich Console**, never `print`. User-facing errors go through `GoblinError`.
- **Shell-out args are always `list[str]`.** Never f-string a command for `subprocess`.
- **Atomic writes**: state JSON via temp file + `Path.replace()`.
- **Branch ops** go through `git.py`. Don't reach into `git._run` from elsewhere.
- **Agent registry is static.** Don't add a plugin system.
- **Two-tier storage**: global registry under `$XDG_DATA_HOME/goblin-watcher/`; per-project records under `<project_root>/.goblin/`. Worktrees at `<project_root>/.worktrees/<branch>/`. New clones (via `--repo`) land in `~/goblin/<project>/`; `--dir` adopts an existing checkout in place. Never touch the user's tracked `.gitignore` — append patterns to `.git/info/exclude` instead.

## Toolchain

[asdf](https://asdf-vm.com/) manages local tool versions. `.tool-versions` pins `python 3.12.8` and `just 1.40.0`. Fresh setup:

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

The same command set runs in CI (`.github/workflows/verify.yml`).

## Safety boundaries

- Never `git push --force` on `main` / default branches. For feature branches, only `--force-with-lease` — and only at user request.
- Never delete a worktree with uncommitted changes unless `--force` is passed (`gw task rm` enforces this).
- Never write outside: `<project>/.goblin/`, `<project>/.worktrees/`, the user's XDG dirs, or the project's working tree itself.
- **Linear API is read-only by default.** Posting a comment requires the explicit `--notify-linear` flag on `gw pr open`.
- 1Password `op` references resolve lazily; only fetched when actually needed.

## Adding an agent

The registered set is `claude`, `codex`, `gemini`, plus `managed` (scaffold; see ADR 0002 and `docs/designs/sessions-and-windowing.md`). To wire another:
1. Create `src/goblin_watcher/agents/<name>.py` with `spawn_command`, `resume_command`, `capture_session_id`, `list_sessions`, `read_transcript`, `env`.
2. Add it to `models.AgentName`.
3. Add it to `agents/registry.registry`.
4. Add a row to `commands/doctor.py`'s binary check list. Agents with no local binary (e.g. `managed`) get a custom check function instead.
5. If the agent has project-level prerequisites (e.g. `managed` requires a remote), extend `agents.registry.validate_agent_for_project` and call it from `commands/new.py` and `commands/run.py`.
6. Tests under `tests/test_agents_<name>.py`.

Resist adding entry-point discovery or a plugin system — keep it static.

## Tmux mode

`windowing = "tmux"` in `config.toml` (or `--windowing tmux`) routes spawns through `TmuxWindower`:
- One tmux session (default name `goblin`) hosts everything.
- One window per task, named after `task.id` (e.g. `eng-123`).
- One pane per agent session (additional sessions on the same task `split-window`). `tmux.split = "vertical"` (default) stacks panes top/bottom; `"horizontal"` is side-by-side.
- `attach_on_spawn = true` (default) means `gw` will `os.execvp` into `tmux attach` if invoked from outside tmux. From inside, it uses `tmux switch-window`.
- `mark_idle = true` (default `false`) sets `monitor-silence <mark_idle_seconds>` on each task window — when claude/codex/gemini goes quiet (done or waiting for input), tmux marks the window with a `~` in the status bar. No bell, no banner, no focus-stealing. `mark_idle_seconds` defaults to `5`. Caveat: any agent that streams a heartbeat/spinner will never trigger this; verify your agent actually falls silent at its prompt.

Test against a fake `tmux` (see `tests/test_windowing_tmux.py`) — never call the real binary in tests.

## What not to refactor

- Don't merge `state.py`, `sessions.py`, and `models.py` into one module. The separation tracks the persistence/business boundary.
- Don't replace the questionary picker with custom prompt_toolkit code unless the UX requires it.
- Don't introduce async at the CLI layer; `httpx.Client` (sync) is enough.

## CI parity

`.github/workflows/verify.yml` runs `uv sync --extra dev` then `uv run ruff check .`, `uv run ruff format --check .`, `uv run ty check src`, `uv run pytest -q`. The justfile's `verify` target matches.
