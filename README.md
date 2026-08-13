# goblin-watcher

**Parallel AI coding agents in git worktrees.** A CLI replacement for [Conductor](https://conductor.build/) and [Superset](https://superset.sh/) — Linear ticket → branch + worktree → spawn agent (resume or fresh), with multiple agent sessions per ticket and optional tmux windowing.

```text
gw ENG-123                         # Linear ticket → clone repo → branch + worktree → agent
gw gh-42                           # GitHub issue → branch + worktree → agent
gw new --branch-name spike/foo --title "Trying a thing"
gw new --branch-auto               # auto-named branch (e.g. swift-otter)
gw new --branch feat/eng-456-token-bucket
gw new --dir ~/code/scratch-fork --title "Sandbox"
gw run                             # in worktree: pick a session; outside: project → task → session
gw run --new                       # fresh session on an existing task
gwcd eng-123                       # cd into a task's worktree (shell wrapper around `gw cd`)
gwcode eng-123                     # open a task's worktree in VS Code
gwobsidian eng-123                 # open a task's worktree as an Obsidian vault
gwfinder eng-123                   # open a task's worktree in Finder
gw status                          # tree view across projects, tasks, sessions
gw pr open                         # open a GitHub PR via `gh`
gw task prune                      # forget tasks whose branch is merged
gw session prune --older-than 30   # forget sessions idle for 30+ days
```

## Install

Prerequisites:

- **[asdf](https://asdf-vm.com/)** with the `python` and `just` plugins (`asdf plugin add python && asdf plugin add just`).
- **[uv](https://docs.astral.sh/uv/)** ≥ 0.8.x (Astral's Python package manager).
- **`git`** (you almost certainly have this already).
- Optional but recommended: **`gh`** (GitHub CLI, for `gw pr`), **`tmux`** (for windowing mode), **`op`** (1Password CLI, for `op://...` references to Linear keys).

Quickstart:

```bash
asdf install              # installs Python 3.14.0 and just 1.40.0 per .tool-versions
uv sync --extra dev
uv run gw --help
uv run gw doctor          # confirms which agent CLIs and helpers are on PATH
```

Optional install for daily use (so `gw` works outside the venv):

```bash
uv tool install --editable .
```

### Shell completion

```bash
# Zsh: dump to your fpath and rebuild compinit.
mkdir -p ~/.zfunc && gw completion zsh > ~/.zfunc/_gw
# Then in ~/.zshrc:
#   fpath+=~/.zfunc
#   autoload -U compinit && compinit

# Bash:
gw completion bash > ~/.gw-completion.bash
# Then in ~/.bashrc:  source ~/.gw-completion.bash

# Fish:
gw completion fish > ~/.config/fish/completions/gw.fish
```

`gw --install-completion` / `--show-completion` (Typer's built-ins) fail to detect your shell when `gw` is launched via a wrapper like `uv run`, so we ship `gw completion <shell>` as a stable replacement that just emits the script to stdout.

The zsh script is **static** — it knows every subcommand and flag at generation time, so `gw new <TAB>` shows the flag list right away (no need to type `--` first). Bash and fish use Typer's dynamic completion, which calls back into `gw` per tab press. Pass `--dynamic` if you want the dynamic zsh script too.

The `gwcd` / `gwcode` / `gwobsidian` / `gwfinder` shell-function wrappers around `gw cd` (which can only print a path — a subprocess can't `cd` its parent shell) are published via [`spg`](https://github.com/shr3kst3r/spg) from this project's `spg.toml`, not by `gw completion`. After `spg install` + a fresh zsh:

- `gwcd eng-123` (or just `gwcd` to open the picker) `cd`s your shell into the matching task's worktree.
- `gwcode eng-123` opens that worktree in VS Code (requires the `code` CLI on `$PATH`).
- `gwobsidian eng-123` opens that worktree as an Obsidian vault (requires Obsidian installed; first open prompts to trust the folder).
- `gwfinder eng-123` opens that worktree in Finder (`open <path>`).

## Core concepts

### Project

A registered git repository. Created with `gw project new <name> --repo <url>` (clones into `~/goblin/<name>/`) or `--dir <path>` (adopts an existing checkout in place). Projects can be tagged with a Linear team key (`--team ENG`) so `gw ENG-123` auto-resolves which repo to use.

There is no "current" project. Any command that needs one accepts `--project NAME`; if you omit the flag, gw opens an interactive project picker (auto-picking when only one is registered).

Stored at:

- Registry: `~/.local/share/goblin-watcher/state.json` (XDG)
- Per-project: `<repo>/.goblin/project.json`

### Task

A unit of work: branch + worktree + an optional tracking item (a Linear ticket or a GitHub issue) + a list of agent sessions. Created from one of these **sources**:

| Source | Flag | Example |
|---|---|---|
| Linear ticket | `--linear` | `gw new --linear ENG-123 --repo git@github.com:org/repo.git` |
| Linear ticket stacked on another PR | `--linear ... --from` | `gw new --linear ENG-123 --from feat/eng-456-token-bucket` |
| GitHub issue | `--issue` | `gw new --issue 42`, `gw new --issue org/repo#42`, or the issue URL |
| GitHub PR | `--pr` | `gw new --pr 42` (checks out the PR's head branch) |
| Fresh branch | `--branch-name` | `gw new --branch-name spike/profiler --from main --title "Profile feed"` |
| Auto-named branch | `--branch-auto` | `gw new --branch-auto` (yields e.g. `swift-otter`) |
| Existing branch | `--branch` | `gw new --branch feat/eng-456-token-bucket` |
| Existing directory | `--dir` | `gw new --dir ~/code/scratch-fork --title "Sandbox"` |

Worktrees live at `<repo>/.worktrees/<branch>/` by default. Task JSON is at `<repo>/.goblin/tasks/<task-id>.json`.

### Session

One agent conversation on a task. **Many sessions per task** — e.g. two parallel claude sessions exploring different approaches to the same ticket, plus a codex session applying review fixes. Each session carries a rolling summary derived from the agent's transcript.

When you run an agent against a task with existing sessions, the resolution rules are:

- `--session <id>` → resume that exact session.
- `--session` (no value) → force the session picker, even if there's only one match.
- `--new` → fresh session, seeded with the task's context.
- `--agent <other>` with no existing sessions for that agent → fresh.
- 0 matches for the chosen agent → fresh.
- 1 match → resume silently.
- 2+ matches → interactive picker (the picker includes a `[ New session ]` entry).

## Headline workflows

### `gw <LINEAR-ID>` — auto-pilot

```bash
gw ENG-123
```

What it does (the first time):

1. Resolves `ENG-123` via the Linear GraphQL API (needs `LINEAR_API_KEY` env var or a configured `op://` reference).
2. Picks the project whose `linear_team_key == "ENG"`. If none, falls back to `--repo <url>` to clone+register a new project named `eng`.
3. Creates branch `eng-123-<slug-from-title>` off the project's default branch.
4. Materializes a worktree at `<repo>/.worktrees/eng-123/` (the worktree dir uses the bare task id; only the branch carries the slug).
5. Spawns your default agent (`claude` unless overridden) with the Linear issue as the seed prompt.

Subsequent invocations on the same ticket error out, because the task already exists. Resume with `gw run <task-id>` (e.g. `gw run eng-123`), fork a parallel session with `gw run <task-id> --new`, or pick a specific one with `gw run <task-id> --session <id>`.

### `gw gh-42` — the same thing from a GitHub issue

```bash
gw gh-42                                   # issue #42 in the current project
gw new --issue 42 --project my-repo        # same, explicit about which repo
gw new --issue https://github.com/org/repo/issues/42
gw new --issue org/tracker#3 --project my-repo   # tracking issue lives in another repo
```

For repos tracked in GitHub Issues instead of Linear. `gw gh-42` is exactly parallel to `gw ENG-123`:

1. Reads the issue via `gh issue view` (no Linear key needed — just an authenticated `gh`).
2. Task id is `gh-42`; branch is `gh-42-<slug-from-title>` off the default branch (or `--from`).
3. Worktree at `<repo>/.worktrees/gh-42/`, and the agent's seed prompt carries the issue title, state, labels, and body.
4. `gw pr open` then adds `Closes #42 — <title>` to the PR body, so landing the PR closes the issue.

Which repo the work happens in: `--project` wins; otherwise a registered project whose remote matches the issue's `owner/repo`; otherwise the project you're standing in; otherwise `--repo <url>`, which clones and registers one. The cwd rule is what makes the cross-repo form work — when the tracking issue lives in `org/tracker` but the code is in `org/repo`, run it from inside `org/repo`'s checkout or pass `--project`. Cross-repo PRs get the qualified `Closes org/tracker#3` form, which GitHub only auto-closes when you can write to the issue's repo; otherwise it lands as a plain reference.

The `gw gh-42` shorthand is same-repo only, and it claims `gh-<digits>` from the Linear shorthand — a Linear team literally keyed `GH` needs `gw new --linear GH-42`.

### `gw new` — explicit task creation

The general form. Use this when:

- You don't have a ticket yet (`--branch-name`, `--dir`).
- The work is tracked in GitHub Issues (`--issue`).
- You're picking up a teammate's branch (`--branch`) or PR (`--pr`).
- You want to skip the agent launch (`--no-launch`).

Examples:

```bash
gw new --branch-name spike/test-token-bucket --title "Try a token bucket"
gw new --branch feat/eng-456-token-bucket          # pick up an existing branch
gw new --dir ~/code/scratch-fork --title "Quick experiment"
gw new --linear ENG-123 --no-launch                # create task, don't spawn agent
gw new --linear ENG-123 --from feat/other-pr       # stack on a teammate's PR branch
gw new --issue 42                                  # GitHub issue #42 in this repo
gw new --issue org/tracker#3 --project my-repo     # tracking issue in another repo
gw new --issue 42 --research                       # investigate and report back, don't implement
```

#### `--from` — stacked branches

`--from <branch>` bases the new task on something other than the default branch. When that branch belongs to a task gw already tracks, the link is recorded as `parent_task` on the new task, and the stack becomes visible instead of looking like unrelated work:

- `gw status` nests the child under its parent, so a four-deep chain renders as one chain.
- `gw task show` prints a `stacked on` line.
- `gw pr open` adds a "Stacked on `<branch>`" section to the PR body, linking the parent's PR when it has one.
- Once the parent's PR merges, `gw sync` fires a `parent-merged` notification naming the *child* — the branch that now needs rebasing.

```bash
gw new --branch-name feat/part-2 --from feat/part-1   # stacked on the feat/part-1 task
gw new --pr 412                                       # a PR whose base is a tracked branch, too
```

Nothing rebases automatically. The default branch never counts as a parent, and an untracked base branch (a teammate's, say) records no link — there's no task to point at.

#### `--research` — spawn an investigation instead of an implementation

```bash
gw new --linear ENG-123 --research
gw new --issue 42 --research --prompt "just the sync path"   # narrows the focus
```

The agent gets the same ticket context as usual, but a different standing brief: read the code, search history, run tests and linters, make read-only fetches — then report its findings **in the session** rather than implementing anything, opening a PR, or commenting on the ticket. `--prompt` narrows the investigation instead of replacing the brief.

Two honest caveats:

- **It needs a ticket.** `--research` is only valid with `--linear` or `--issue`; the other sources carry no tracking item, so gw refuses rather than seeding a research brief about nothing. Same for `gw run --research` on a task with neither.
- **The boundary is instruction-level, not enforced.** With `defaults.unsafe = true` (the default) the agent still *can* push or comment — it's just not told to. gw doesn't gate `gw pr open` or anything else on research mode. See [ADR 0006](docs/adrs/0006-research-mode-seed-prompt.md).

### `gw run` — interactive session picker

For a task you've already created. Resolves the task from `cwd` (you're inside the worktree), an explicit path, a task id, or — when none of those are available — an interactive picker chain:

```bash
cd ~/goblin/my-repo/.worktrees/eng-123
gw run                                              # in-worktree: jump straight to sessions

gw run                                              # outside any worktree: project → task → session pickers
gw run eng-123                                      # explicit task id
gw run --session abc123                             # skip picker, resume that id
gw run --session                                    # force the picker even if there's only one match
gw run --new                                        # force fresh
gw run --agent codex                                # pick a non-default agent
gw run --project my-repo                       # scope task lookup + picker to one project
gw run eng-123 --research                           # fresh read-only research session on the ticket
gw run eng-123 --address-review                     # fresh session seeded with the PR's review feedback
```

`--research` always starts a fresh session (so it can't be combined with `--session`) and needs the task to carry a Linear ticket or GitHub issue. Same caveats as `gw new --research` above: the read-only boundary is what the agent is told, not what it's prevented from doing.

#### `--address-review` — hand the PR's feedback back to an agent

```bash
gw run eng-123 --address-review
gw run eng-123 --address-review --prompt "just the concurrency thread"   # narrows the focus
```

The loop this replaces is reading the review comments, reading the failing check's log, and pasting both into a session by hand. gw already knows the PR, so it fetches instead:

- every **unresolved review thread**, with its diff hunk and the whole reply chain (bot findings included — Bugbot and Codex post as review threads)
- the body of every **`CHANGES_REQUESTED` / `COMMENTED` review** (an `APPROVED` body is congratulation; a `DISMISSED` one was overruled)
- for each **failing check**, the tail of its failing steps' log, pulled with `gh run view --log-failed`

…all embedded in the seed prompt, with a brief to adjudicate each item against the code before changing anything, fix what's real, and say what it's leaving alone and why.

Worth knowing:

- **It needs a PR with something outstanding.** No PR, an unreadable one, or one with every thread resolved and every check green all get a refusal rather than a session seeded with nothing. Checks still *running* don't count as failing.
- **It doesn't write to GitHub.** The agent is told to report in-session and push its fixes with `gw pr open` (idempotent — it pushes the branch and skips creating a second PR). Replying to threads and resolving them stays yours.
- **Plain PR comments are out of scope** — only review threads carry a resolved/unresolved state, which is what "unresolved" is derived from.
- Like `--research`, it always starts a fresh session and is a property of that session, not the task. Multi-repo tasks get one block per repo. See [ADR 0008](docs/adrs/0008-address-review-seed-prompt.md).

The picker chain auto-skips a level when there's only one option: one project → goes straight to its tasks; one task → goes straight to its sessions.

### `gw cd` / `gwcd` / `gwcode` / `gwobsidian` / `gwfinder` — jump into a task's worktree

```bash
gwcd eng-123                                        # cd into <repo>/.worktrees/eng-123/
gwcd                                                # no arg → opens the project → task picker
gwcd --project my-repo                         # scope the picker to one project
gwcode eng-123                                      # open the worktree in VS Code (`code <path>`)
gwobsidian eng-123                                  # open the worktree as an Obsidian vault
gwfinder eng-123                                    # open the worktree in Finder (`open <path>`)
```

`gw cd` itself just prints the resolved worktree path on stdout (a subprocess can't `cd` its parent shell). The `gwcd` / `gwcode` / `gwobsidian` / `gwfinder` wrappers that act on that path are shell functions published by [`spg`](https://github.com/shr3kst3r/spg) from this project's `spg.toml` (install once with `spg install`, then start a new zsh). `gwobsidian` opens the path via the `obsidian://open?path=…` URI — Obsidian prompts to trust the folder the first time you open it as a vault. `gwfinder` opens the path with macOS `open`, which reveals the worktree in Finder.

### `gw pr open` — push and open a PR

```bash
gw pr open eng-123 [--project NAME] [--draft] [--notify-linear]
```

Pushes the branch via `git push -u origin`, then shells out to `gh pr create`. The PR is titled after the task's tracking item, and the body is templated from it: a Linear ticket contributes a `Resolves ENG-123` line plus its description and comment thread; a GitHub issue contributes a `Closes #42 — <title>` line (`Closes owner/repo#42` when the issue lives in another repo). The PR URL is persisted on the task record. Pass `--project` if the same task id exists in more than one registered project.

Re-running is idempotent: if an open PR already exists for the branch, the push still happens but `gh pr create` is skipped. `--notify-linear` posts a comment with the PR URL(s) on the task's Linear issue — the only write `gw` ever performs against Linear.

### `gw pr checks` — which check broke

```bash
gw pr checks eng-123 [--project NAME]
```

`gw status` flags a task with `✗ checks` but not *which* check. This lists them, one row per check: state glyph, name (`workflow / job` for a GitHub Actions run), GitHub's own word for the outcome, and the details URL to open.

Failing checks sort first, then still-running, then the ones that passed. A PR with no CI configured at all says so rather than rendering an empty list. Exits non-zero only when the task has no PR to look at; a red check is still a successful report.

## Worked examples

### Start a new Linear ticket against a checkout you already have

You've got `my-repo` cloned at `/Users/you/goblin/my-repo` and want to start `ENG-123`.

```bash
# 1. Register the existing checkout as a project, tagged with the Linear team.
gw project new my-repo \
    --dir /Users/you/goblin/my-repo \
    --team ENG

# 2. Auto-pilot.
gw ENG-123
```

That second command fetches the issue from Linear, creates `eng-123-<title-slug>` off the project's default branch, materializes a worktree at `/Users/you/goblin/my-repo/.worktrees/eng-123/`, and spawns claude with the issue body as its seed prompt. Re-running `gw ENG-123` later resumes that session.

If you haven't tagged the project with `--team`, pass `--project` explicitly:

```bash
gw ENG-123 --project my-repo
```

### Start a Linear ticket when the repo isn't cloned yet

```bash
gw ENG-123 --repo git@github.com:your-org/my-repo.git
```

`gw` clones the repo into `~/goblin/eng/` (named after the team), registers it as a project, then proceeds as above. Future `gw ENG-<n>` calls reuse that project.

### Spike without a Linear ticket

```bash
gw new --branch-name spike/cache-warmer --project my-repo \
    --title "Try warming the LRU on boot"
```

Creates branch `spike/cache-warmer` off the default branch, worktree at `.worktrees/spike-cache-warmer/`, and spawns the agent with the title as the seed. Omit `--project` and you'll get the project picker (or auto-pick if there's only one registered).

### Pick up a teammate's branch

```bash
gw new --branch feat/ingest-backfill --project my-repo   # local or remote branch; fetched if needed
```

### Run a second agent in parallel on the same ticket

```bash
gw run eng-123 --new                        # fresh claude alongside the existing session
gw run eng-123 --agent codex --new          # or try codex on the same ticket
```

Both sessions live on the same task. Switch between them with the picker:

```bash
cd /Users/you/goblin/my-repo/.worktrees/eng-123
gw run                                      # picker lists every session on this task
```

### Create the task without spawning the agent

```bash
gw new --linear ENG-123 --no-launch
```

Useful when you want the branch + worktree pre-built before opening it yourself, or when scripting setup.

### Open a PR when the work is done

```bash
gw pr open eng-123                          # push + `gh pr create`
gw pr open eng-123 --draft --notify-linear  # draft PR; also post the URL back to Linear
```

### Inspect state

```bash
gw status                                   # tree: projects → tasks → sessions
gw status --project my-repo            # limit to one project
gw status --cost                            # same tree, annotated with tokens + estimated cost
gw status --active                          # only tasks with work actually in flight
gw status --watch --active                  # ...redrawn continuously. The dashboard.
gw pr checks eng-123                        # one row per CI check: state, name, details URL
gw task show eng-123                        # task detail with rolling session summaries (--project to disambiguate)
gw session ls                               # sessions for the current task
gw session show <session-id>                # one session in full, including its token counts
gw session transcript <session-id>          # full transcript as [user]/[assistant] blocks (--raw: file path)
gw session send eng-123 "also fix the tests"  # type into a running agent's tmux pane
gw history --cost                           # day-by-day token + cost rollup across every session
gw doctor                                   # which agent CLIs are on PATH + Linear key status
gw config show                              # resolved config (file merged over defaults)
gw sync status                              # is background sync scheduled? when did it last run?
```

Each session row in `gw status` carries an activity hint derived from the transcript's mtime: `● active` while the agent is producing output, `idle <age>` once it has gone quiet (done, or waiting on you). Linear states are cached for `linear_state_ttl_seconds` (default 300) to keep status fast; `--no-linear` skips the refresh entirely.

`gw status` also adopts any agent transcripts it finds on disk under a worktree that aren't yet recorded as sessions — useful after spawning an agent outside of `gw`, or after a session record was deleted.

#### The dashboard: `--active` and `--watch`

Once you have thirty tracked tasks, the full tree is three hundred lines and the two agents actually working are somewhere in the middle of it. `--active` keeps only the tasks with something in flight:

- a session whose transcript has been written within `defaults.activity_grace_seconds` (default 900 — fifteen minutes), or
- a live headless run, detected from its pid file.

The grace window is deliberately much wider than the two minutes behind the `● active` badge. An agent that stops to ask you a question goes quiet almost immediately, and that is exactly when you want it still on screen — so `● active` and `idle 6m` both stay, `idle 2h` doesn't. A live headless run is a second, independent signal: it has no terminal and can go a long time between transcript writes, so it earns a `⚡ headless` badge and stays regardless of mtime. Projects with nothing in flight drop out entirely rather than rendering an empty heading.

`--watch` (`-w`) redraws in place every `--interval` seconds (default 2) until you Ctrl-C. Together they're the thing you leave open on a second monitor:

```bash
gw status --watch --active
```

Watch mode is deliberately cheaper than a snapshot: it reads task state, transcript mtimes, headless pid files, and the `gw sync` indicator cache — no Linear or GitHub round-trips, no LLM description refresh, no session reconciliation, and no state write unless a summary actually moved. That is what the indicator cache is for; keep `gw sync` scheduled and the dashboard stays current for free. The tradeoff: a session started outside `gw` is adopted by `gw sync` or by a plain `gw status`, not by a watch tick.

`--active` works on its own too, and skips the ticket refresh for tasks it filters out — so it's faster than a full `gw status`, not just shorter.

### What did today cost?

Claude's and codex's transcripts carry per-request token counts, so `gw` — the only thing that sees all N parallel sessions at once — can total them up:

```bash
gw session show <session-id>                # tokens + cost for one conversation
gw status --cost                            # rolled up per session, per task, per project, plus a grand total
gw history --cost --days 7                   # day by day, across everything
```

```
 day           in     out  cache read  cache write     cost
 2026-08-13  3.8K  288.1K       73.6M         1.1M  ~$55.28
 total       3.8K  288.1K       73.6M         1.1M  ~$55.28
```

Read the dollar figure as *what these tokens would have cost at published API rates* — it is an estimate, not a bill. It doesn't know about your subscription plan (where the marginal cost of a session is zero), negotiated rates, introductory pricing, or fast mode. Gemini and antigravity report nothing, because `gw` can't read their transcripts.

Counts are parsed on the same pass that refreshes session summaries, so they cost no extra work and follow the same `summary_ttl_seconds` freshness rules — `gw history --cost` reads only what's already recorded, so run `gw status` or `gw session refresh` first if a session looks stale. Models `gw` has no rate for (codex's `gpt-*` models ship unpriced) still have their tokens counted; give them a price under `[cost.pricing]` and they join the dollar total.

### Add a fresh session to an existing task

```bash
gw run --new                                # in-worktree: fork a fresh session on this task
gw run eng-123 --new                        # by task id, from anywhere
gw run --agent codex --new                  # fresh codex session alongside any claude sessions
```

When the picker shows (2+ existing sessions for the agent), pick the `[ New session ]` entry. A different `--agent` with no prior sessions on the task always starts fresh — no `--new` needed.

## Cleanup

```bash
gw session rm <session-id>                  # forget a session from gw's record; agent transcript untouched
gw session prune --older-than 30            # forget every session whose last_used_at is >30d ago
gw session prune --older-than 7 --agent codex --dry-run
gw task archive <task-id>                   # drop the worktree; keep the branch, the record, and the sessions
gw task archive <task-id> --force           # archive anyway when the worktree is dirty or a headless run is live
gw task rm <task-id>                        # delete worktree + branch + record (confirms; --force to skip)
gw task rm <task-id> --project my-repo      # scope the lookup when the id exists in multiple projects
gw task prune                               # remove every task whose branch is merged (all projects)
gw task prune --project my-repo        # limit to one project
gw task prune --dry-run                     # preview without removing
gw task prune --force                       # skip confirm + ignore uncommitted-changes guard
gw project rm <name>                        # unregister a project (does NOT delete the checkout)
```

`gw task prune` checks each task's PR state via `gh pr view` when a PR URL is recorded, and falls back to `git merge-base --is-ancestor` against the base branch. Squash- and rebase-merged branches without a recorded PR URL won't be detected by the ancestry check alone.

`gw task archive` is the middle ground between `gw task rm` and keeping everything. Worktrees are the expensive part — a full checkout per task adds up fast when you run many in parallel — while the branch, the task record, and the session history cost almost nothing. Archiving removes only the checkout; `gw run <task-id>` recreates it from the branch and re-applies the project's `[setup]` steps, so a parked task picks straight back up. Archived tasks render dimmed in `gw status` and are skipped by `gw sync`'s git-indicator step (there's no working tree left to read). It refuses a dirty worktree or a live headless run unless you pass `--force`, and it refuses scratch spaces outright — a scratch directory has no branch to come back from.

`gw session prune` operates only on `gw`'s record of `task.sessions`. The underlying agent transcript files (e.g. `~/.claude/projects/<encoded-cwd>/*.jsonl`) are untouched, so you can still resume a forgotten session by id if you re-add it.

## Background sync

`gw sync` does ahead of time what `gw status` and the picker otherwise do on the
blocking path: refresh Linear states and session summaries, reconcile sessions,
pull PR and CI state, cache the git indicators, prune what's safely prunable, and
notify you when something changes. It is **not a daemon** — each pass is a
short-lived, idempotent command, scheduled by launchd.

```bash
gw sync                                     # run one pass now, printing every action
gw sync install                             # schedule it (launchd; --interval SECONDS)
gw sync status                              # installed? loaded? last run? what's missing?
gw sync watch                               # follow background passes live
gw sync uninstall                           # stop scheduling
```

Nothing runs in the background until you run `gw sync install`; without it,
`gw status` behaves exactly as before.

**Notifications are edge-triggered** — each fires once, when the state actually
changes, so a quiet day produces none:

| Event | Fires when |
|---|---|
| `agent-idle` | a session that was producing output goes quiet |
| `pr-merged` | the PR's state becomes `MERGED` |
| `parent-merged` | a task you're stacked on landed, so your branch needs a rebase |
| `checks-failed` / `checks-passed` | CI flips |
| `prunable` | a merged branch can't be auto-pruned because the worktree is dirty |

**Pruning never forces.** A task is removed only when it is merged *and* clean;
anything dirty or ambiguous is reported and left alone. Scratch spaces have no
merge signal, so they're pruned by idle age and only when you opt in:

```bash
gw config set sync.scratch_prune_days 14    # prune scratch spaces idle > 14 days (0 = off)
gw config set sync.prune false              # disable auto-pruning of merged tasks
```

`gw status` reads the cached indicators when they're fresh, showing the reading's
age (`↑2 unpushed (3m)`), and recomputes live otherwise. `gw status --no-cache`
always recomputes.

Everything a pass does is journaled to `~/.local/share/goblin-watcher/logs/sync.jsonl`,
which is what `gw sync watch` and `gw sync status` read. Trim it with
`gw sync prune-journal --days 30`.

## Worktree setup

A fresh worktree is a bare checkout. No `.env`, no `.venv`, no `node_modules`, nothing `uv sync` would have built — anything gitignored simply isn't there, so without a hook the first thing a spawned agent does is rediscover the project's bootstrap, or fail at it and start guessing.

Declare that bootstrap once and `gw` applies it every time it materializes a worktree:

```toml
[setup]
copy = [".env", ".env.local", ".claude/settings.local.json"]
link = ["node_modules"]                # symlink instead of copy — big, rebuildable
run  = ["uv sync --extra dev"]
timeout_seconds = 600                  # per run step
```

- **`copy`** / **`link`** are paths relative to the project root, reproduced at the same relative path inside the worktree. A missing source is skipped, not an error — `.env.local` doesn't exist in every checkout.
- **`run`** entries execute in the new worktree, in order, after `copy` and `link`. A string goes through `sh -c` (so `&&`, pipes, and `$VARS` work); an argv list — `["just", "hooks"]` — is exec'd directly with no shell. Each step gets `GW_PROJECT`, `GW_PROJECT_ROOT`, `GW_WORKTREE`, and `GW_TASK_ID` in its environment.

Setup runs on `gw new`, `gw <LINEAR-ID>`, `gw gh-<N>`, `gw scratch`, and `gw task add-repo` — anywhere a worktree is created. It does **not** run for `gw new --dir`, which adopts a checkout you already bootstrapped yourself. Pass `--no-setup` to skip it, and `gw task setup <id>` to re-run it by hand.

**Per-project override.** A project that needs its own bootstrap gets a `<project-root>/.goblin/setup.toml`, which replaces the global `[setup]` table outright (the same "presence wins" rule `gw prompt` uses for `prompt.md`). Either spelling works there — a bare table, or one nested under `[setup]`:

```toml
# <project-root>/.goblin/setup.toml
copy = [".env"]
run  = ["pnpm install --frozen-lockfile"]
```

**Failures are loud.** Every step is journaled to `~/.local/share/goblin-watcher/logs/setup.jsonl` and printed as it happens; a failing `run` step skips the rest and stops `gw new` before it launches the agent, so you get a reported error instead of an agent quietly working in a half-built worktree. Fix the cause, then `gw task setup <id>` and `gw run <id>`.

**`copy`/`link` paths must stay inside the project root.** Absolute paths, `..` components, and symlinks pointing outside are all refused — that boundary is what keeps a setup table from reaching into `~/.ssh`.

## Configuration

Optional config at `~/.config/goblin-watcher/config.toml`. Edit it directly, or via `gw config`:

```bash
gw config show                       # resolved config + file path
gw config set defaults.agent codex   # validated before write
gw config unset defaults.agent       # back to the built-in default
gw config edit                       # $EDITOR, validated on save
```

```toml
[defaults]
agent = "claude"                  # "claude" | "codex" | "gemini" | "antigravity"
windowing = "inline"              # "inline" | "tmux" | "headless" (see Headless windowing below)
summary_ttl_seconds = 30          # how long a session summary is considered fresh
unsafe = true                     # spawn agents with their bypass-permission flag (see below)
activity_active_seconds = 120     # transcript touched within this → `● active`; older → `idle <age>`
activity_grace_seconds = 900      # how long a session stays on the `gw status --active` dashboard

[linear]
# Literal key, or an `op://vault/item/field` reference resolved via the 1Password CLI.
# Env var LINEAR_API_KEY takes precedence.
api_key = "op://Personal/Linear/api_key"

[tmux]
session_name = "goblin"           # tmux session that hosts every task
attach_on_spawn = true            # `gw` execs `tmux attach -t <session>` after spawning
split = "vertical"                # additional sessions on a task: "vertical" (top/bottom) or "horizontal" (side-by-side)

[sync]                            # background sync; inert until `gw sync install`
interval_seconds = 300            # also the worst-case staleness of cached indicators
prune = true                      # auto-prune merged AND clean tasks; never forces
scratch_prune_days = 0            # prune scratch spaces idle > N days (0 = off)
notify = "auto"                   # "auto" (macOS notifications on darwin) | "macos" | "command" | "off"
notify_command = []               # argv for notify = "command"; title and body are appended
notify_events = ["agent-idle", "pr-merged", "parent-merged", "checks-failed", "checks-passed", "prunable"]

[setup]                           # applied to every freshly materialized worktree (above)
copy = [".env"]                   # gitignored files to copy in from the project root
link = []                         # ... or symlink instead, e.g. "node_modules"
run = ["uv sync --extra dev"]     # bootstrap commands, run in the new worktree
timeout_seconds = 600             # per run step

[cost]                            # rates behind `gw status --cost` / `gw history --cost`
cache_read_multiplier = 0.1       # relative to the model's input rate
cache_write_multiplier = 1.25     # 5-minute cache
cache_write_1h_multiplier = 2.0   # 1-hour cache

# Merged over gw's built-in table, keyed by the model id in the agent's
# transcript. USD per million tokens. `gw config set` can't reach into a table —
# use `gw config edit` for these.
[cost.pricing."gpt-5-codex"]
input = 1.25
output = 10.0
```

## Tmux windowing

Set `windowing = "tmux"` in `config.toml`, or pass `--windowing tmux` on any spawn command:

- One tmux session (default name `goblin`) hosts everything.
- One window per task, named after `task.id` (e.g. `eng-123`).
- One pane per session — additional sessions on the same task `split-window` into the existing window. Default orientation is `vertical` (stacked top/bottom); set `tmux.split = "horizontal"` for side-by-side.
- If `gw` is run from outside tmux and `attach_on_spawn = true`, it `execvp`s into `tmux attach`. From inside, it `select-window`s.

Inline mode (default) just blocks on the agent process and returns when it exits.

## Headless windowing (unattended runs)

Set `windowing = "headless"`, or pass `--windowing headless` on a spawn command, to start an agent with no terminal at all:

```bash
gw new --issue 42 --windowing headless --prompt "fix this and open a PR"
gw run eng-123 --new --windowing headless --prompt "rerun the failing tests"
```

`gw` starts the agent in the print mode every supported CLI already has (`claude -p`, `codex exec`, `agy -p`, `gemini -p`), detaches it into its own process group, and returns immediately:

- stdout and stderr are appended to `<project>/.goblin/logs/<task>-<session>.log`, and the child's pid goes in a sibling `.pid` file. Both paths are printed at spawn.
- stdin is `/dev/null`, so an agent that asks for input gets EOF instead of hanging.
- The run survives the shell that launched it — a cron slot or a queue worker can exit straight away.

To be told when it finishes, install background sync (`gw sync install`) and keep the `agent-idle` event on: it fires once, on the edge, when the session's transcript goes quiet. That's the whole notification path — nothing waits on the agent, so its exit status isn't recorded. Check the log for a run that failed to start.

Three things headless mode won't do:

- **Resume.** It only starts fresh sessions; print mode runs one prompt to completion. Start another headless run with the follow-up as its prompt.
- **Take input.** `gw session send` refuses — there's no prompt sitting there to type into.
- **Ask permission.** Keep `unsafe = true` (the default). Without it the agent stops at its first approval prompt with nobody to answer, which looks like a hang; `gw` warns but doesn't refuse.

Also note that `agent-idle` needs a transcript gw can read, which today means claude and codex. A headless gemini or antigravity run completes silently.

### Talk to a running agent

```bash
gw session send eng-123 "also fix the tests"
gw session send eng-123 "..." --session <id>   # when the task has several live panes
gw session send eng-123 "" --no-enter          # type without submitting (--no-enter), or Enter alone ("")
```

The text is typed into the agent's pane and submitted, exactly as if you had attached and typed it — so you can steer six agents from one terminal instead of six.

`gw` labels each pane with its session id when it opens it, so `--session` addresses one conversation on a task running several. With a single live pane the session is unambiguous and `--session` is optional. Panes that predate this labelling (or an agent spawned by hand) still take input as long as they're the only pane on the task.

Inline windowing has no pane to address — the agent owns the terminal it was launched from — so `gw session send` says so rather than failing obscurely. Headless runs refuse for their own reason: stdin is `/dev/null`.

## Agent support

| Agent | Spawn | Resume | Session discovery |
|---|---|---|---|
| **claude** (Claude Code) | `claude --session-id <uuid> "<prompt>"` | `claude --resume <id>` / `--continue` | Full — reads `~/.claude/projects/<encoded-cwd>/*.jsonl` for ids, summaries, turn counts |
| **codex** | `codex "<prompt>"` | `codex resume` (codex's own picker) | Full — walks `~/.codex/sessions/**/rollout-*.jsonl`, matches tasks on `session_meta.cwd`, parses ids, summaries, turn counts |
| **gemini** | `gemini -p "<prompt>"` | `gemini --continue` | None — cwd-scoped checkpoints, no stable id; `list_sessions` / `read_transcript` are stubs, so sessions carry no summary |
| **antigravity** (Google Antigravity, binary `agy`) | `agy --prompt-interactive "<prompt>"` | `agy --conversation <id>` / `--continue` | Partial — conversation id recovered from `~/.gemini/antigravity-cli/cache/last_conversations.json`; transcripts live in SQLite and aren't parsed |

For unattended runs (`--windowing headless`) each agent is launched in its print mode instead of the spawn column above: `claude -p`, `codex exec`, `agy -p`, `gemini -p`.

Only claude can be handed its session id at spawn time; for the others `gw` records a synthesized placeholder and reconciles it to the agent's real id after the process exits. Tmux mode returns before the agent has written anything, so there the placeholder sticks — codex transcripts are then found by falling back to the newest rollout for the worktree, which is correct as long as one codex session per worktree is active.

`gw doctor` checks which binaries are on PATH and resolves the Linear key. It also warns, per agent, when `gw` can't parse that agent's transcripts — gemini and antigravity keep their history somewhere `gw` doesn't read, so their sessions have no rolling summary, no LLM description, no turn count, and never fire an `agent-idle` notification.

### Unsafe mode (skip permission prompts)

**On by default.** `gw` spawns each agent with its "skip the confirmation prompts" flag, so the agent will execute tool calls without asking. Opt out globally with `defaults.unsafe = false` in `config.toml`, or per-invocation with `--no-unsafe` on `gw new`, `gw run`, and `gw <LINEAR-ID>`. The flag injected per agent:

| Agent | Bypass flag |
|---|---|
| claude | `--dangerously-skip-permissions` |
| codex | `--dangerously-bypass-approvals-and-sandbox` |
| gemini | `--yolo` |
| antigravity | `--dangerously-skip-permissions` |

The agents will execute everything they decide to do without asking. If you'd rather be prompted, set `defaults.unsafe = false` in your config.

## Commands reference

```text
gw <LINEAR-ID> [...any `gw new` flag]       # sugar for `gw new --linear <ID>`
gw gh-<N>      [...any `gw new` flag]       # sugar for `gw new --issue <N>`
gw new --linear|--issue|--pr|--branch|--branch-name|--branch-auto|--dir
       [--title ...] [--from ...] [--project NAME] [--with-project NAME]
       [--repo URL] [--agent ...] [--prompt ...] [--research] [--adversarial-review]
       [--rm|--rm-force] [--no-launch] [--no-setup]
       [--windowing inline|tmux|headless] [--unsafe|--no-unsafe]
gw run [PATH|TASK-ID] [--session [ID]] [--new] [--agent ...] [--prompt ...]
       [--research] [--adversarial-review] [--address-review]
       [--project NAME] [--windowing ...] [--unsafe|--no-unsafe]
gw scratch [NAME] [--agent ...] [--prompt ...] [--no-launch] [--no-setup]
           [--windowing ...] [--unsafe|--no-unsafe]
gw cd  [PATH|TASK-ID] [--project NAME]      # prints worktree path; pair with spg's gwcd/gwcode/gwobsidian/gwfinder shell functions
gw status [--project NAME] [--no-linear] [--no-cache] [--cost]
          [--active] [--watch|-w] [--interval SECONDS]      # tree view of projects → tasks → sessions
gw sync                                     # run one background-sync pass now, verbosely
gw doctor                                   # binary + key resolution checks
gw history [--tail N|--all] [--json]        # audit log of every `gw` invocation (`gw history prune` trims it)
gw history --cost [--days N]                # day-by-day token + cost rollup across every session (0 = all time)
gw completion zsh|bash|fish [--dynamic]     # emit tab-completion script (the gwcd/gwcode/gwobsidian/gwfinder wrappers live in spg.toml)

gw project new|ls|info|rm
gw task ls|show|rename|setup|add-repo|archive|rm|prune   # all accept --project to scope to one project
                                        # `setup` re-runs the [setup] steps (also: --repo NAME)
                                        # `archive` drops the worktree, keeps branch + record (also: --force)
                                        # `prune` also: --dry-run/--force/--no-fetch/--scratch-older-than
gw session ls|show|send|refresh|rm|prune  # `send` types into a live pane (tmux); `prune` accepts --older-than/--agent/--task/--project/--dry-run/--force
gw pr open|status|checks                # all accept --project to disambiguate a task id shared across projects
gw sync run|watch|status|install|uninstall|prune-journal   # background refresh; `run` is what the scheduler calls
gw prompt show|set|edit|clear           # text appended to every fresh-spawn prompt
gw config show|get|set|unset|edit|path  # user config under ~/.config/goblin-watcher/
gw version
```

Always reach for `gw <command> --help` for the full option list.

## Storage layout

```text
~/.local/share/goblin-watcher/
├── state.json                            # registry of projects
├── state.lock                            # advisory lock for registry writes (ADR 0004)
├── sync/                                 # background-sync state + cached task indicators
└── logs/                                 # commands.jsonl, sync.jsonl, setup.jsonl

~/.config/goblin-watcher/
└── config.toml                           # user config (above)

<project-root>/
├── .goblin/
│   ├── project.json                      # this project's record
│   ├── setup.toml                        # optional; replaces the global [setup] table
│   ├── logs/                             # headless runs: <task>-<session>.log + .pid
│   └── tasks/
│       ├── eng-123.json                  # one file per task; carries sessions[]
│       └── .eng-123.lock                 # advisory lock sidecar for that task
└── .worktrees/
    └── eng-123/                          # one worktree per task (dir = task id; branch carries the slug)
```

`.goblin/` and `.worktrees/` are appended to `.git/info/exclude` so we never touch the user's tracked `.gitignore`.

## Loop-closing smoke (manual)

```bash
# Throwaway sandbox
cd "$(mktemp -d)"
git init -q -b main demo && cd demo && git config user.email t@t && git config user.name t
echo hi > README.md && git add . && git commit -qm init && cd ..

# Register, create a task, inspect
gw project new demo --dir "$PWD/demo"
gw new --branch-name spike/smoke --title "smoke" --no-launch
gw status
gw task show spike-smoke
```

Expected: `gw status` shows the `demo` project with one task on branch `spike/smoke`, no sessions yet.

## Development

See [AGENTS.md](AGENTS.md) for architecture, conventions, and safety boundaries. Local verification is `just verify` (lint + format + typecheck + tests). Design docs live under [`docs/designs/`](docs/designs/); ADRs under [`docs/adrs/`](docs/adrs/).

## License

Proprietary. All rights reserved.
