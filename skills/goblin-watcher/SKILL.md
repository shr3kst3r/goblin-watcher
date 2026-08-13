---
name: goblin-watcher
description: "Drive the goblin-watcher CLI (`gw`): create tasks (branch + git worktree) from Linear tickets, GitHub issues, GitHub PRs, or branches, manage agent sessions, run agents headless (including a fleet of them in parallel), open PRs, and prune merged work. Trigger when the user mentions gw or goblin-watcher, or asks to 'spin up an agent on ENG-123', 'work on issue 42', 'work these N issues in parallel', 'launch headless agents', 'create a task/worktree for this ticket/issue/PR/branch', 'list my gw tasks/sessions', 'what did the agent change', 'open a PR for this task', 'prune merged tasks', or 'make a scratch space'."
argument-hint: "<LINEAR-ID> | gh-<n> | new --issue <n> | new --pr <n> | status | diff | task prune | sync status"
allowed-tools: [Bash]
---

# goblin-watcher (`gw`) — parallel AI coding agents in git worktrees

`gw` turns a Linear ticket / GitHub issue / GitHub PR / branch into a **task**:
a branch + git worktree under `<project_root>/.worktrees/<branch>/`, with one or
more resumable agent **sessions** (claude / codex / gemini / antigravity) on top. Published on
`$PATH` via spg, so plain `gw ...` works anywhere; inside the goblin-watcher
repo itself, `uv run gw ...` also works.

## Agent rules — read first

- **You are usually already inside a gw task.** Commands that take a task id
  default to the cwd's task (`gw pr open`, `gw pr status`, `gw cd`, `gw run`),
  so omit the id when operating on the current worktree.
- **Avoid the interactive pickers.** Omitting `--project` / a task id outside a
  worktree opens a questionary picker, which hangs without a TTY. Always pass
  explicit ids and `--project <name>` when the cwd doesn't imply them.
- **Whether you may spawn depends on the windowing mode.** `inline` blocks on
  the agent and `tmux` may `exec` into `tmux attach` — never start either from
  inside a session; that hijacks the terminal. `headless` is the sanctioned way
  to launch from a session (`--windowing headless`): a detached print-mode run
  with stdin on `/dev/null`, which returns as soon as it has spawned. Every
  launching command takes `--windowing` (`gw new`, `gw run`, `gw scratch`, and
  the `gw <LINEAR-ID>` / `gw gh-<N>` shortcuts) — so never run one of them bare,
  where it inherits `defaults.windowing`. For an agent the *user* will drive,
  create with `gw new ... --no-launch` and tell them to run `gw run <task-id>`
  in their own terminal. For an agent *you* drive, see *Running agents headless*
  below.
- **`--prompt` and `--mode` require a launch.** Both error out with
  `--no-launch`, since there is no session to seed. Pair them with
  `--windowing headless`, or leave them off and let the user pick the brief.
- **Read-only commands are always safe**: `gw status --no-linear`,
  `gw diff <id> --stat`, `gw task ls --project X`, `gw task show <id>`,
  `gw session ls`, `gw session show/transcript <id>`, `gw pr status`,
  `gw pr checks`, `gw project ls`, `gw doctor` (without `--repair`),
  `gw history`, `gw sync status`, `gw config show`. Prefer `--no-linear` on
  `gw status` unless fresh ticket state matters (it skips the network call for
  Linear *and* GitHub issues).
- **Destructive commands need explicit user intent.** `gw task rm` deletes the
  worktree *and* the branch; `--force` additionally discards uncommitted
  changes — never pass it unprompted. `gw task prune --force` likewise. Prefer
  `gw task prune --dry-run` first. `gw task archive` is the reversible middle
  ground: it drops only the worktree.
- **`gw sync` deletes things too, on a timer.** A scheduled pass prunes merged
  tasks when `sync.prune` is on (worktree + branch), and `[sync.on]` can wire
  `prune` or `archive` to an event. Neither asks. Check `gw sync status` before
  assuming a vanished worktree was someone's mistake.
- **Two commands write to Linear, both opt-in.** `gw pr open --notify-linear`
  posts a comment with the PR URL — only pass it when asked. A workflow-state
  move happens only if the user has configured `[linear.transitions]`. Linear
  is read-only otherwise.
- **Agents launch permission-bypassed by default** (`defaults.unsafe = true`,
  e.g. claude's `--dangerously-skip-permissions`). `--no-unsafe` opts out per
  invocation.

## Intent → command

| User intent | Command |
|---|---|
| "spin up an agent on ENG-123" (they'll run it) | `gw new --linear ENG-123 --no-launch`, then user runs `gw ENG-123` or `gw run eng-123` |
| "work on GitHub issue 42" | `gw new --issue 42 --project <name> --no-launch` (task id `gh-42`; `owner/repo#42` or an issue URL also work) |
| "create a task from PR 42 / a PR URL" | `gw new --pr 42 --project <name> --no-launch` (a URL auto-resolves the project) |
| "task from an existing branch" | `gw new --branch feat/foo --project <name> --no-launch` |
| "fresh task, new branch" | `gw new --branch-name spike/foo --title "..." --project <name> --no-launch` (or `--branch-auto`) |
| "work these N issues in parallel" (you drive) | one `gw new --issue <n> --project <name> --windowing headless --prompt "<brief>"` per issue — see *Running agents headless* |
| "research this ticket, don't implement it" | `gw new --linear ENG-123 --mode research --windowing headless` (or `--no-launch`, then the user runs `gw run eng-123 --research`) |
| "what's the worktree path for eng-123?" | `gw cd eng-123` (prints the path; `gwcd`/`gwcode` shell wrappers cd/open it) |
| "what am I / is everything working on?" | `gw status --no-linear` (add `--active` for only work in flight, `--watch` for a live dashboard) |
| "what did the agent change on eng-123?" | `gw diff eng-123 --stat` (drop `--stat` for the patch; works on an archived task too) |
| "list tasks / sessions" | `gw task ls --project <name>` / `gw session ls` |
| "open a PR for this task" | `gw pr open` (add `--draft` if asked; `--notify-linear` only if asked) |
| "PR status?" | `gw pr status` |
| "is CI red on this PR?" | `gw pr checks` (name, state, details URL per check) |
| "free up the worktree, keep the branch" | `gw task archive <task-id>` (`gw run <task-id>` rebuilds it) |
| "clean up merged tasks" | `gw task prune --dry-run`, review, then `gw task prune` |
| "clean up old scratch spaces" | `gw task prune --scratch-older-than 30` |
| "forget old sessions" | `gw session prune --older-than 30` |
| "register a repo with gw" | `gw project new <name> --repo <url>` (clone) or `--dir <path>` (adopt in place) |
| "scratch space to poke at something" | `gw scratch <name> --no-launch` (plain dir at `~/goblin/scratch/<name>`, no git repo) |
| "read what an agent did on a session" | `gw session transcript <session-id>` (`--raw` prints the transcript file path) |
| "is gw set up right?" | `gw doctor` |
| "gw's records don't match reality" | `gw doctor --repair` (applies only the fixes that cannot lose work; anything holding commits or files stays a report) |
| "is background sync running?" | `gw sync status`; `gw sync watch` follows it live, `gw sync run --verbose` forces one pass |

## Running agents headless

`--windowing headless` is the one mode built to be driven from a session. Per
launch:

```
gw new --issue 58 --project goblin-watcher \
       --windowing headless --prompt "<the whole brief>" --no-classify
```

- **Fresh sessions only.** `--windowing headless` with `--session` is refused —
  print mode is "run this prompt to completion", so there is no conversation to
  resume. A follow-up is a *new* headless run whose `--prompt` is the follow-up.
- **Keep `unsafe` on** (it is the default). A headless agent that hits a
  permission prompt has nobody to answer it and looks like a hang.
- **The prompt is the entire briefing.** Nobody will clarify anything. Say what
  the task is, which files it owns, how to verify, and how to finish.
- `--no-classify` skips the advisory cheap-model ticket read — worth it when
  launching many at once, since it only prints a suggestion.

**Give each agent a file lane.** Parallel agents on one repo collide by editing
the same file, and the only fix that works is telling each one in its prompt
which files it owns and which to leave alone. Name the other agents' hot files
explicitly rather than trusting them to guess. (Across eleven parallel issues in
goblin-watcher itself, the hotspots were `commands/new.py`, `commands/status.py`,
`commands/doctor.py`, and `sync/engine.py` — the shared registries and the one
module every feature has to register with.)

**Launch a dependent task when its parent merges, not in fixed waves.** Waves
idle every agent behind the slowest one in the batch. Poll for the parent's
merge (`gw pr status`, or the `pr-merged` sync event) and launch the child then.

**Monitoring — the log is not the signal.** `claude -p` buffers its output, so
`<project>/.goblin/logs/<task>-<session>.log` usually stays empty until the run
exits. Read state from elsewhere:

| Question | Where to look |
|---|---|
| Is it still alive? | the sibling `<task>-<session>.pid`, or `gw status --active` |
| What is it doing? | `gw session transcript <session-id>`, `gw status` (activity badge) |
| Has it made progress? | `gw diff <task-id> --stat`, then `gw pr status` / `gw pr checks` |
| Why did it die? | the `.log` — a failed *start* (bad flag, expired auth) does land there |

`gw session send` does not work on a headless run (stdin is `/dev/null`); it
needs a tmux pane.

**Three traps, each one round trip:**

- **A fresh worktree has no `.venv`.** A worktree is a bare checkout, so nothing
  gitignored comes along and the project's verify command fails until the agent
  bootstraps (`uv sync --extra dev` here). Put it in the prompt, or make it a
  `[setup]` `run` step so every new worktree gets it.
- **`gh pr merge --delete-branch` fails when the branch is checked out in a
  worktree** — which it always is for a gw task. Merge without it and let
  `gw task prune` clean up.
- **A scheduled `gw sync` prune can delete a worktree mid-run.** Its guard stops
  on a dirty worktree, not on a live headless pid. Set `sync.prune = false`
  (`gw config set sync.prune false`) while a fleet is in flight, or accept that
  a task that has already committed and merged may lose its checkout underneath
  the agent.

## Concepts and conventions

- **Project**: a registered repo. `gw project new <name> --repo <url>` clones
  into `~/goblin/<name>/`; `--dir` adopts an existing checkout. Tag with
  `--team ENG` so `gw ENG-123` auto-resolves the repo.
- **Task**: id is the slugged branch/ticket (e.g. `eng-123`). Worktree lives at
  `<project_root>/.worktrees/<branch>/`. Task ids can collide across projects —
  disambiguate with `--project` (gw errors on ambiguity rather than guessing).
- **Worktree setup**: the `[setup]` table (global, or `<project_root>/.goblin/setup.toml`)
  declares `copy` / `link` / `run` steps applied to each new worktree, since a
  bare checkout has none of the gitignored files a build needs. `gw task setup`
  re-runs them; `gw new --no-setup` skips them.
- **Session**: one agent conversation on a task; multiple per task is normal.
  Each carries a rolling summary derived from the agent transcript
  (`gw session refresh` recomputes).
- **Multi-repo workspace**: `gw new --with-project X` or
  `gw task add-repo <id> <project>` builds a task spanning repos; `gw pr open`
  then opens one PR per repo.
- **`gw <LINEAR-ID>` dispatcher**: `gw ENG-123` (no subcommand) is auto-pilot —
  resolve repo, create branch + worktree from the ticket, spawn the agent. It is
  a literal argv rewrite to `gw new --linear ENG-123`, so every `gw new` flag
  passes through (`gw ENG-123 --windowing headless --prompt "..."` works). Only
  the *bare* form is off-limits from a session: it inherits
  `defaults.windowing`, which is usually interactive.
- **`gw gh-<N>` dispatcher**: same auto-pilot for a GitHub issue in the current
  repo (`gw gh-42` ≡ `gw new --issue 42`). Needs an authenticated `gh`, not a
  Linear key. Task id is `gh-42`; `gw pr open` then adds `Closes #42` to the PR
  body. A tracking issue in another repo goes through
  `gw new --issue owner/repo#42 --project <name>` (the shorthand is same-repo
  only). Same argv rewrite, so the same caveat: pass `--no-launch` or
  `--windowing headless` rather than running it bare.
- **Work mode**: `gw new --mode <name>` changes the agent's standing brief
  without changing the task. Built-ins are `research` (investigate and report,
  don't implement; needs a ticket) and `adversarial-review`; `--research` and
  `--adversarial-review` are aliases. Users can add their own under
  `[modes.<name>]`. `gw run` has no `--mode` — it takes `--research`,
  `--adversarial-review`, and `--address-review` (seeds the session with the
  PR's unresolved review threads and failing-check output).
- **Archive vs remove**: `gw task archive <id>` drops the worktree and keeps the
  record, branch, and sessions — the checkout is the expensive part. `gw run`
  rematerializes it. `gw task rm` deletes the branch too, and is not reversible.
- **`gw sync`**: a short-lived idempotent background pass, not a daemon,
  scheduled through launchd by `gw sync install`. Each pass refreshes ticket and
  PR/CI state plus cached git indicators (which is what `gw status` reads unless
  you pass `--no-cache`), fires edge-triggered notifications, and prunes merged
  tasks when `sync.prune` is on. `[sync.on]` maps an event to an action
  (`spawn-fix-session`, `prune`, `archive`) — empty by default, and it is
  dict-valued, so it needs `gw config edit`, not `gw config set`.
- **Config** is TOML at `$XDG_CONFIG_HOME/goblin-watcher/config.toml`; edit via
  `gw config set <key> <value>` (validated), never by hand-editing blind.
  Notable keys: `defaults.unsafe`, `defaults.agent`, `defaults.windowing`
  (`inline`/`tmux`/`headless`), `tmux.split`, `tmux.mark_idle`,
  `sync.interval_seconds`, `sync.prune`.
- **State**: global registry under `$XDG_DATA_HOME/goblin-watcher/`; per-project
  records in `<project_root>/.goblin/`. Treat both as gw-owned — inspect with
  `gw task show` / `gw session show`, don't edit the JSON.
