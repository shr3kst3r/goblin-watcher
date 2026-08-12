---
name: goblin-watcher
description: "Drive the goblin-watcher CLI (`gw`): create tasks (branch + git worktree) from Linear tickets, GitHub issues, GitHub PRs, or branches, manage agent sessions, open PRs, and prune merged work. Trigger when the user mentions gw or goblin-watcher, or asks to 'spin up an agent on ENG-123', 'work on issue 42', 'create a task/worktree for this ticket/issue/PR/branch', 'list my gw tasks/sessions', 'open a PR for this task', 'prune merged tasks', or 'make a scratch space'."
argument-hint: "<LINEAR-ID> | gh-<n> | new --issue <n> | new --pr <n> | status | task prune"
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
- **Don't spawn agents from inside a Claude session.** `gw <LINEAR-ID>`,
  `gw new`, `gw run`, and `gw scratch` launch an interactive agent (and in tmux
  mode may `exec` into `tmux attach`). From a session, create with
  `gw new ... --no-launch` and tell the user to run `gw run <task-id>` in their
  own terminal — or seed work with `--prompt "<text>"` only when the user asked
  for a launched agent.
- **Read-only commands are always safe**: `gw status --no-linear`,
  `gw task ls --project X`, `gw task show <id>`, `gw session ls`,
  `gw session show/transcript <id>`, `gw pr status`, `gw project ls`,
  `gw doctor`, `gw history`, `gw config show`. Prefer `--no-linear` on
  `gw status` unless fresh Linear state matters (it skips the network call).
- **Destructive commands need explicit user intent.** `gw task rm` deletes the
  worktree *and* the branch; `--force` additionally discards uncommitted
  changes — never pass it unprompted. `gw task prune --force` likewise. Prefer
  `gw task prune --dry-run` first.
- **`gw pr open --notify-linear` writes to Linear** (posts a comment with the
  PR URL). Linear is read-only otherwise; only pass it when asked.
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
| "what's the worktree path for eng-123?" | `gw cd eng-123` (prints the path; `gwcd`/`gwcode` shell wrappers cd/open it) |
| "what am I / is everything working on?" | `gw status --no-linear` |
| "list tasks / sessions" | `gw task ls --project <name>` / `gw session ls` |
| "open a PR for this task" | `gw pr open` (add `--draft` if asked; `--notify-linear` only if asked) |
| "PR status?" | `gw pr status` |
| "clean up merged tasks" | `gw task prune --dry-run`, review, then `gw task prune` |
| "clean up old scratch spaces" | `gw task prune --scratch-older-than 30` |
| "forget old sessions" | `gw session prune --older-than 30` |
| "register a repo with gw" | `gw project new <name> --repo <url>` (clone) or `--dir <path>` (adopt in place) |
| "scratch space to poke at something" | `gw scratch <name> --no-launch` (plain dir at `~/goblin/scratch/<name>`, no git repo) |
| "read what an agent did on a session" | `gw session transcript <session-id>` (`--raw` prints the transcript file path) |
| "is gw set up right?" | `gw doctor` |

## Concepts and conventions

- **Project**: a registered repo. `gw project new <name> --repo <url>` clones
  into `~/goblin/<name>/`; `--dir` adopts an existing checkout. Tag with
  `--team ENG` so `gw ENG-123` auto-resolves the repo.
- **Task**: id is the slugged branch/ticket (e.g. `eng-123`). Worktree lives at
  `<project_root>/.worktrees/<branch>/`. Task ids can collide across projects —
  disambiguate with `--project` (gw errors on ambiguity rather than guessing).
- **Session**: one agent conversation on a task; multiple per task is normal.
  Each carries a rolling summary derived from the agent transcript
  (`gw session refresh` recomputes).
- **Multi-repo workspace**: `gw new --with-project X` or
  `gw task add-repo <id> <project>` builds a task spanning repos; `gw pr open`
  then opens one PR per repo.
- **`gw <LINEAR-ID>` dispatcher**: `gw ENG-123` (no subcommand) is auto-pilot —
  resolve repo, create branch + worktree from the ticket, spawn the agent.
  Interactive; from a session prefer `gw new --linear ENG-123 --no-launch`.
- **`gw gh-<N>` dispatcher**: same auto-pilot for a GitHub issue in the current
  repo (`gw gh-42` ≡ `gw new --issue 42`). Needs an authenticated `gh`, not a
  Linear key. Task id is `gh-42`; `gw pr open` then adds `Closes #42` to the PR
  body. A tracking issue in another repo goes through
  `gw new --issue owner/repo#42 --project <name>` (the shorthand is same-repo
  only). From a session prefer `--no-launch`.
- **Config** is TOML at `$XDG_CONFIG_HOME/goblin-watcher/config.toml`; edit via
  `gw config set <key> <value>` (validated), never by hand-editing blind.
  Notable keys: `defaults.unsafe`, `defaults.agent`, `windowing`
  (`inline`/`tmux`), `tmux.split`, `tmux.mark_idle`.
- **State**: global registry under `$XDG_DATA_HOME/goblin-watcher/`; per-project
  records in `<project_root>/.goblin/`. Treat both as gw-owned — inspect with
  `gw task show` / `gw session show`, don't edit the JSON.
