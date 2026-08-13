# Sessions and Windowing

Current-state design of how `goblin-watcher` represents AI coding sessions and where their processes run.

## Data model

```
Project ──< Task ──< SessionRecord
```

- **Project** (`models.Project`) — a registered git repository. Stored as `~/.local/share/goblin-watcher/state.json` (registry) plus `<repo>/.goblin/project.json` (per-project record). Carries `name`, `root`, `repo_url`, `default_branch`, `branch_prefix`, optional `linear_team_key`.
- **Task** (`models.Task`) — a branch + worktree + optional Linear issue + a list of sessions. Stored at `<repo>/.goblin/tasks/<task_id>.json`. Worktrees live at `<repo>/.worktrees/<branch>/`.
- **SessionRecord** (`models.SessionRecord`) — one agent conversation. Many per Task: a user can keep two parallel claude sessions plus one codex session on the same ticket. Carries `agent`, `session_id`, timestamps, `summary`, `turn_count`, `transcript_path`.

The list-of-sessions shape is the load-bearing piece. It enables the headline UX (interactive picker when multiple conversations exist) without forcing a special-case "primary session" concept.

## Session lifecycle

1. **Source resolution** (`commands/new.py`) — a Task is born from one of four sources: `--linear`, `--branch-name`, `--branch`, `--dir`. Each path normalizes to (project, branch, worktree, optional LinearIssue).
2. **Spawn decision** (`agents/launcher.py`) — fresh vs resume. The CLI picks based on flags (`--session`, `--new`) and `task.sessions`. With multiple candidates, `picker.choose_session` shows a questionary picker.
3. **Run** (`agents/launcher.launch`) — invokes `agent.spawn_command(prompt=...)` or `agent.resume_command(session_id=...)`, hands the argv off to a `Windower`.
4. **Capture** — after the agent process returns, `agent.capture_session_id(cwd)` reads the agent's session store for the newest entry; falls back to a synthesized UUID for agents that don't expose stable ids.
5. **Summary refresh** (`sessions.refresh_summary`) — parses the agent's transcript file for turn count and last-message snippets. Lazy-refresh on read; eager refresh on session exit. Stale threshold defaults to 30s (`config.defaults.summary_ttl_seconds`).

## Windowing

The `Windower` protocol decides *where* the agent process runs.

- **`InlineWindower`** (default): `subprocess.run(cmd, cwd=worktree, env=...)` — blocking, stdio passes through. Terminal is the agent's terminal.
- **`TmuxWindower`** (opt-in via `windowing = "tmux"`): one tmux session named `goblin`, one window per task (`task.id`), one pane per session. Additional sessions on the same task `tmux split-window` — orientation is set by `tmux.split` ("vertical" → top/bottom, the default; "horizontal" → side-by-side). Attach behavior depends on `$TMUX` — `select-window` if already inside the goblin session, `os.execvp` into `tmux attach` from outside.

Both ship in MVP. Config switches; CLI flag `--windowing` overrides per invocation.

## Agent abstraction

`Agent` protocol (`agents/base.py`) has six methods:
- `spawn_command(prompt, cwd)` — argv for a fresh interactive session.
- `resume_command(session_id, cwd)` — argv for resume; `session_id=None` for cwd-scoped continue.
- `capture_session_id(cwd)` — newest session id at `cwd`, or `None`.
- `list_sessions(cwd)` — for the path-driven picker.
- `read_transcript(session_id, cwd)` — for summary refresh.
- `env()` — extra environment overlay.

Four concrete impls in `agents/{claude,codex,gemini,antigravity}.py`. Static registry in `agents/registry.py`. **No plugin system.** Adding another agent is a small documented checklist in root `AGENTS.md`.

Claude and Codex both implement the full surface. Claude reads `~/.claude/projects/<encoded-cwd>/<session-id>.jsonl`, and because `claude --session-id` accepts a caller-chosen UUID, the id `gw` records at spawn is already the id on disk. Codex walks the rollout JSONL under `~/.codex/sessions/<YYYY>/<MM>/<DD>/`, reads the leading `session_meta` envelope for the session id and cwd, and associates a rollout with a task by matching that cwd against the worktree; messages come from the `event_msg` records (`user_message` / `agent_message`) rather than the `response_item` ones, so codex's auto-injected AGENTS.md preamble and `<environment_context>` wrapper stay out of the summary. Codex can't preassign an id, so its resume always drops into codex's own picker (`codex resume`) and the transcript lookup falls back to the newest rollout for the cwd when the recorded id is a synthesized placeholder.

Gemini is the only fully stubbed implementation: no stable session ids at all, `--continue` for resume, and `list_sessions` / `read_transcript` / `render_transcript` return empty until its session-store layout is confirmed.

Antigravity (`agy`) sits in between: its conversations live in an internal SQLite store we don't parse, so transcripts are stubbed, but its documented workspace cache (`~/.gemini/antigravity-cli/cache/last_conversations.json`, a map of absolute workspace path → most recent conversation id) lets `capture_session_id` recover the real id after an inline run and resume it by id later. Note that `agy -p` is *headless* print mode — spawning an interactive session uses `--prompt-interactive`.

### Managed agent (scaffold only)

`agents/managed.py` adds a fourth agent name, `managed`, intended for Anthropic-hosted execution per ADR 0002. The current state is scaffolding:

- The `Agent` protocol surface exists but `spawn_command` / `resume_command` raise a `GoblinError` pointing at the missing launcher — the existing subprocess-shaped launcher can't drive a remote attach loop without further work.
- `ManagedClient` is a `runtime_checkable` `Protocol` capturing the remote-API surface (`create_session`, `submit_turn`, `stream_events`, `fetch_patch`, `terminate`). The only concrete impl is `NotConfiguredClient`, whose methods raise a uniform `GoblinError`.
- `agents.validate_agent_for_project` gates `managed` on the project having a `repo_url` set (no remote → no place for the sandbox to clone from).
- `git.apply_patch_safely` is the local half of the patch-return loop: it refuses on a dirty worktree or when `HEAD` has moved from the patch's `base_sha`, and uses `git apply --3way` otherwise.
- `gw doctor` carries a "managed agent" row that always reports `ok` with a "scaffold only" detail; the real backend wiring will replace it with checks that exercise the client.

What still has to be built before managed runs work end-to-end: a `ManagedClient` implementation against a real hosted backend, plus a launcher path that drives the attach loop (streaming events into a tmux pane or stdio, accepting checkpoint requests, applying returned patches). See ADR 0002 for the intended shape.

## Why this shape

- **Task as the unit** rather than Session: a branch + worktree is the load-bearing artifact (git operations, PRs, base-branch tracking). Sessions hang off because they're cheaper to recreate.
- **Many sessions per task**: real workflows want to try two approaches in parallel against the same ticket. Forcing one session per agent per task was the wrong shape.
- **Lazy-refresh summaries**: refreshing on read (status / picker) and on session exit covers the natural moments without a long-running process. This remains the fallback path and the only behaviour when background sync isn't installed. Since ADR 0005 the same work is *also* done ahead of time by `gw sync`, a short-lived scheduled command — still no resident daemon, but freshness no longer has to ride the interactive blocking path. See `background-sync.md`.
- **Tmux as opt-in**: tmux is the obvious right answer for power users but a hard dependency for everyone else. The `Windower` protocol keeps the spawn path agnostic.
