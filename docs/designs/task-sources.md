# Task Sources

Current-state design of how `gw new` creates a Task from one of its input sources.

## Why several sources

A Task always carries a branch + worktree + an optional tracking item (a Linear ticket or a GitHub issue). The input shapes match real workflows:

- **Linear ticket** (`--linear`) — the daily driver where work is tracked in Linear. The ticket title drives the branch slug; the body becomes the seed prompt.
- **GitHub issue** (`--issue`) — the same loop for repos tracked in GitHub Issues. Title drives the branch slug, body seeds the prompt, and `gw pr open` writes a `Closes` line so landing the PR closes the issue.
- **GitHub PR** (`--pr`) — reviewing or fixing up work that already exists; checks out the PR's head branch.
- **Fresh branch by name** (`--branch-name` / `--branch-auto`) — exploring an idea without a ticket; the user supplies a slug (or gw generates one) and, optionally, a base.
- **Existing branch** (`--branch`) — picking up a teammate's work or a review fix-up. The branch already exists locally or on the remote.
- **Existing directory** (`--dir`) — adopting a worktree someone else (or a previous gw run on a different machine) already created.

Every source collapses to the same Task shape, so downstream commands (`run`, `pr`, `task show`, `status`) don't need to care which one produced a task.

## Resolution flow

```
                          gw new ...
                              │
                      ┌───────┴───────┐
                      │ source flag?  │
                      └───────┬───────┘
    ┌───────┬─────────┬───────┼─────────┬─────────┬────────┐
--linear  --issue    --pr  --branch-name  --branch  --dir
    │        │         │       │            │         │
_from_    _from_   _from_pr  _from_    _from_existing │
linear    issue              new_branch  _branch      │
    │        │         │       │            │  _from_existing_dir
    └────────┴─────────┴───────┴────────────┴─────────┘
                              │
                      ┌───────┴───────┐
                      │ Task record   │
                      │   .goblin/    │
                      │   tasks/X.json│
                      └───────┬───────┘
                              │
                      ┌───────┴───────┐
                      │ Launch agent? │  (skip if --no-launch)
                      └───────────────┘
```

Exactly **one** source flag must resolve. Conflicting flags (`--linear ... --branch ...`) raise `GoblinError` before any side effects.

## Shorthand dispatch

`cli._rewrite_task_shortcut` turns a bare first positional into a `new` invocation before Click ever sees argv:

- `gw gh-42` → `gw new --issue 42` (matched first)
- `gw ENG-123` / `gw eng-123` → `gw new --linear ENG-123`

Reserving `gh-<digits>` for GitHub issues means a Linear team whose key is literally `GH` is unreachable via the shorthand and must use `gw new --linear GH-42`. That tradeoff is deliberate and noted next to the pattern in `cli.py`. The shorthand is same-repo only; a cross-repo issue goes through `gw new --issue owner/repo#42`.

## Per-source behaviour

### `--linear <ID>`

1. Parse `ENG-123` → `(team_key="ENG", number=123)` (`linear/client.parse_identifier`).
2. Resolve API key via `secrets.get_linear_api_key()` (env → config → `op://...`).
3. Fetch the issue via GraphQL: `issues(filter: {team: {key: {eq: $team}}, number: {eq: $number}})`.
4. Resolve project:
   - `--project <name>` wins if given.
   - Else find a registered project where `linear_team_key == team_key`.
   - Else `--repo <url>` clones + auto-registers a new project named after the lowercased team key.
   - Else raise `GoblinError` with a hint.
5. Branch name: `branch_slug(identifier, title, prefix)` → e.g. `eng-123-add-rate-limit`.
6. Task id: lowercased identifier (`eng-123`).
7. Base = `--from <branch>` or `project.default_branch`. Lets the user stack a Linear ticket on top of another PR's branch (auto-fetched if only on origin).
8. Worktree at `<repo>/.worktrees/eng-123/`; reused if it already exists.
9. Persist the `LinearIssue` snapshot on the task.

### `--issue <ref>`

Accepts three forms, parsed by `gh.parse_issue_ref`:

| Form | Example | Repo known up front? |
| --- | --- | --- |
| bare number | `42`, `#42` | no |
| qualified | `shr3kst3r/spg#3` | yes |
| URL | `https://github.com/shr3kst3r/spg/issues/3` | yes |

1. Resolve the project the work happens in, **before** fetching (the bare form can't be looked up without a repo):
   - `--project <name>` wins if given.
   - Bare number → the project containing the cwd, else `--repo <url>`, else `resolve_project` (picker, auto-picking a lone project).
   - Qualified/URL → a registered project whose remote normalizes to the issue's `owner/repo`; else the project containing the cwd (this is the cross-repo case: the tracking issue names a repo that is *not* the one being worked in, so the surrounding worktree decides); else `--repo <url>`; else `GoblinError`.
   - `--repo <url>` reuses a registered project with that remote and otherwise clones + registers one, named after the **URL's** repo. Not the issue's — with a cross-repo tracking issue those differ, and `--repo` says where the work happens.
2. Fetch the issue via `gh issue view --json number,title,body,url,state,labels,assignees`, passing `--repo` for the qualified/URL forms so it resolves regardless of cwd.
3. Task id: `gh-<number>` (e.g. `gh-42`).
4. Branch name: `branch_slug("gh-42", title, prefix)` → e.g. `gh-42-add-rate-limit`.
5. Base = `--from <branch>` or `project.default_branch`, same as `--linear`.
6. Worktree at `<repo>/.worktrees/gh-42/`; reused if it already exists.
7. Persist a `GhIssue` snapshot (number, `owner/repo`, title, body, state, url, labels, assignees) on the task, plus a `github_issue_state_updated_at` stamp for the state-refresh TTL.

`gh-<number>` is unique per repo, not globally: a cross-repo issue #42 collides with the same-repo #42 already tracked in that project. gw refuses via the ordinary "task already exists" path (`--rm` to replace, `--branch-name` as the escape hatch) rather than inventing a disambiguating id for a collision that hasn't shown up in practice.

### `--branch-name <name>`

1. Resolve project (via `--project NAME`, or the interactive picker — `task_resolver.resolve_project` auto-picks when only one is registered).
2. Final branch = `{project.branch_prefix}{name}` with collision suffix (`-2`, `-3`, ...) if needed.
3. Base = `--from <branch>` or `project.default_branch`. If `--from` names a branch that's only on origin, gw fetches and creates a local tracking branch before the worktree add. If it names a branch gw already tracks, the stack is recorded — see [Stacked tasks](#stacked-tasks---from).
4. `git worktree add -b <branch> <dest> <base>` creates the worktree + branch.
5. Task id derived from the branch slug.

### `--branch <existing-name>`

1. Resolve project (via `--project NAME` or the picker, as above).
2. If the branch exists locally, check it out into a fresh worktree.
3. If it only exists on origin, `git fetch` then `git branch <name> origin/<name>` to create a tracking branch, then add the worktree.
4. If neither, raise `GoblinError` ("Branch does not exist locally or on origin.").
5. Worktree path reused if it already exists.

### `--dir <path>`

1. `path` must be a git checkout (`git rev-parse --git-dir` succeeds).
2. Find the registered project that "owns" this path:
   - Either it's inside `project.root` (a normal subdirectory).
   - Or it shares the same main repo via `git rev-parse --git-common-dir` (a worktree of a registered project sitting outside `project.root`).
3. Read the path's current branch via `git rev-parse --abbrev-ref HEAD`.
4. Task id derived from that branch. Worktree path is the adopted directory itself — we do **not** create a new worktree.

## Seed prompt construction

After Task creation (and unless `--no-launch`), the launcher needs a prompt string for `agent.spawn_command(prompt=...)`.

`agents/launcher.build_seed_prompt(task)` selects one of three templates and fills the same context slots in each — a **work brief** (`templates/spawn_prompt.md`, the default), a **research brief** (`templates/research_prompt.md`, when `research=True`), or an **address-review brief** (`templates/address_review_prompt.md`, when a `review` feed is passed). ADR 0006 records why a work mode is an alternate template rather than a canned `--prompt` or a slash-command bypass like `--adversarial-review`.

The `{ticket_id}` / `{title}` / `{description}` slots are filled from whichever tracking item the task carries (`Task.ticket_id` / `Task.ticket_title`):

- For `--linear` tasks: `ENG-123` + title + description + the Linear comment thread.
- For `--issue` tasks: `owner/repo#42` + title + a header line (state, URL, labels) + the issue body. The qualified form is used even same-repo, so a cross-repo tracking issue is never ambiguous.
- For `--branch-name` / `--branch` / `--dir` / `--pr`: id slot becomes `task.id.upper()`, title slot becomes `task.id`, description slot becomes `(no Linear issue or GitHub issue attached — fresh task)`.

This means a task with no tracking item gets a thinner prompt — that's correct; the user has more responsibility for orienting the agent when they didn't go through a tracker.

### Research mode (`--research`)

`gw new --research` and `gw run --research` seed the research brief instead. It carries the same ticket context and repos block, drops the work brief's standing "open a PR via `gw pr open`" instruction, spells out what the agent may and may not do, and asks for findings **in the session** rather than in a file. `--prompt` composes with it, narrowing the investigation's focus instead of replacing the trailer.

Three things to know:

- **It requires a tracking item.** `gw new --research` is refused for `--branch`, `--branch-name`, `--branch-auto`, `--dir`, and `--pr`; `gw run --research` is refused for any task carrying neither a Linear ticket nor a GitHub issue (scratch tasks included). A research brief about nothing is a silent no-op, so gw refuses loudly.
- **The read-only boundary is advisory, not enforced.** `defaults.unsafe = true` is the documented default, so the agent runs with bypassed permissions and could still push or comment. What research mode buys is that the agent is not *instructed* to mutate anything; gw gates no command on it.
- **The mode is a property of the session, not the task.** Nothing is persisted on `Task`, so a research session can be followed by an ordinary implementation session on the same task; `gw run --research` re-derives the mode from the flag each time.

### Address-review mode (`--address-review`)

`gw run --address-review` seeds the address-review brief: the same task context, plus the PR's outstanding feedback embedded verbatim, plus a brief to adjudicate each item against the code before changing anything. `--prompt` composes, narrowing the focus. ADR 0008 records why gw fetches the feedback instead of instructing the agent to.

`review_feed.collect(task)` does the gathering, once per repo on the task:

1. Resolve a PR URL — `TaskRepo.pr_url` when the record has one, else `gh.pr_for_branch` (a PR opened by hand is invisible on the record until something backfills it).
2. `gh.pr_review(repo, number)` — one hand-written GraphQL query returning unresolved review threads (with diff hunks and full reply chains), `CHANGES_REQUESTED`/`COMMENTED` review bodies, and the head commit's check rollup. `reviewThreads` is not reachable through `gh pr view --json`, which is why the query is hand-written rather than a `gh` convenience call.
3. `gh.check_run_log(repo, details_url)` per failing check — `gh run view --job <id> --log-failed`, with the job id parsed out of the Actions details URL. Non-Actions status contexts keep their URL and contribute no log.

Fetched logs are first cleaned (`review_feed.clean_log` drops `gh`'s per-line job/step/timestamp stamp, its BOM, and ANSI colour), then everything embedded is bounded by the `MAX_*` constants in `review_feed`: log tails, comment-body heads, diff-hunk tails, and the number of checks that get a log fetch at all. Cleaning before clipping is deliberate — otherwise most of the budget is spent on scaffolding. Each clip leaves a visible marker.

Four things to know:

- **It requires a PR with something outstanding.** No PR, an unreadable PR, and a fully-clean PR are three distinct refusals. Pending checks are not failing checks. Scratch tasks are rejected outright.
- **Unlike `--research`, it does not require a tracking item** — the input is the PR, so a `--branch`- or `--pr`-sourced task is a valid target.
- **The brief forbids writing to GitHub.** The agent reports in-session and pushes with `gw pr open` (idempotent for an open PR). Replying to and resolving threads stays the user's, in keeping with how `--notify-linear` gates the only other external write.
- **Plain PR conversation comments are excluded** — only review threads carry resolved/unresolved state, and without it there is no way to tell standing feedback from feedback already handled.

## Tracking-item state refresh

A task's cached tracking state goes stale as soon as someone moves the ticket. Both trackers refresh the same way — lazily on the `gw status` render path and again in every background sync pass — behind a TTL stamped on the task:

| Tracker | Module | Task fields | TTL config key |
| --- | --- | --- | --- |
| Linear | `linear_state.py` | `linear.state`, `linear_state_updated_at` | `defaults.linear_state_ttl_seconds` |
| GitHub issue | `github_state.py` | `github_issue.state`, `github_issue_state_updated_at` | `defaults.github_issue_state_ttl_seconds` |

Both write through a narrow patch under the task lock (ADR 0004) and degrade to the cached value on any failure — a missing `gh`, an unresolvable secret, an unreachable API — leaving the timestamp untouched so the next pass retries. `gw status --no-linear` (alias `--no-tickets`) skips both.

## PR linking

`gw pr open` titles the PR from `Task.ticket_title` and, for an issue-backed task, prepends a close line built by `commands/pr._closes_line`:

- Same repo (the PR's `origin` normalizes to the issue's `owner/repo`) → `Closes #42 — <title>`.
- Different repo, or no GitHub remote to compare against → `Closes owner/repo#42 — <title>`.

GitHub only auto-closes across repositories when the PR author can write to the issue's repo, so the qualified form may land as a plain reference. That's the honest failure mode: the link is correct either way, and gw doesn't promise a close it can't make happen.

The issue body is deliberately **not** copied into the PR body (unlike the Linear path, where reviewers may not have Linear access) — on GitHub the linked issue is one click away.

## Stacked tasks (`--from`)

`--from <branch>` bases a fresh branch on something other than `project.default_branch`. When that base turns out to be another task's **primary** branch, `commands/new._parent_task_for_base` records it as `Task.parent_task` — otherwise nothing is recorded (see the rules below). It applies wherever a base is chosen: `--linear`, `--issue`, `--branch-name`, `--branch-auto`, and `--pr` (whose base comes from the PR itself, so a stacked PR is detected without any flag).

Two rules keep the link honest:

- **The default branch is never a parent.** "Stacked on `main`" is the ordinary case; treating it as a stack would nest every task under whichever one happens to own that branch.
- **An untracked base records nothing.** Stacking on a teammate's branch is legitimate and common, and there is no task to point at.

The recorded id is what four surfaces read:

| Surface | What it does |
|---|---|
| `gw status` | nests the child under its parent (`_stack_order` topologically sorts so parents render first) and appends `⤴ restack: <parent> merged` once the parent lands |
| `gw task show` | prints a `stacked on <id> (branch …)` line |
| `gw pr open` | adds a "Stacked on `<branch>`" section citing the parent's task and PR URL — primary repo only |
| `gw sync` | fires `parent-merged` off the parent's `pr-merged` edge, naming the child |

It stores an id rather than a branch name because the branch is exactly what disappears when the parent lands. That makes a dangling `parent_task` normal, not an error: `state.find_parent_task` returns `None` and the surfaces degrade to "no longer tracked" / omit the section. `gw task rename` repoints children (`commands/task._repoint_children`) so a record-only rename doesn't fake an orphan.

Nothing rebases automatically. Rewriting history in a worktree that may have a live agent in it is not a cleanup an unattended pass is allowed to do (same reasoning as "pruning never forces", ADR 0005), so the child gets told and the human decides. `gw task restack` is the obvious follow-on and deliberately not built yet.

`parent_task` lives on `Task`, not `TaskRepo`: it describes the primary branch. A secondary repo in a multi-repo workspace sits on its own project's default branch, so the PR body skips the stack note for it.

## Multi-repo tasks

A task can span more than one repository (ADR 0003). The extra repos are
additive on top of any of the branch-creating sources above:

- **At create time:** `gw new --project alpha --with-project beta [--with-project ...]`.
  The `--project` repo is *primary*; each `--with-project` is added afterward.
  Not valid with `--dir` or `--pr`.
- **Incrementally:** `gw task add-repo <task> <project>` attaches a repo to an
  existing task.

Mechanics live in `workspace.py`:

1. The first time a second repo joins, the task is *promoted* to a workspace:
   a directory under `$XDG_DATA_HOME/goblin-watcher/workspaces/<task-id>/` is
   created and the primary worktree is `git worktree move`d into it as a
   subdir. Promotion refuses if the primary worktree is dirty (it would
   relocate live work).
2. Each added repo gets a branch (shared slug, honoring that project's
   `branch_prefix`; `--from` / `--branch-name` override) and a worktree at
   `<workspace>/<project>/`.
3. The agent launches with `cwd = workspace_path`, seeing every repo as a
   sibling subdirectory.

The task record stays in the **primary** project's `.goblin/tasks/`. Downstream
commands iterate `task.all_repos()` (primary first): `gw pr open` pushes and
opens a PR per repo (cross-linking siblings; `--repo <project>` targets one),
and `gw task rm` tears down every worktree + the workspace directory.

## Edge cases worth knowing

- **Branch collisions on `--linear` reruns.** `_ensure_unique_branch` appends `-2`, `-3` so reruns don't blow up the worktree, but they also don't reuse it — by design, a second `gw ENG-123` after a name collision creates `eng-123-..-2`. If you actually want to resume, use `gw run eng-123` instead of re-creating.
- **`--dir` outside any registered project.** Without `git-common-dir` matching a registered `project.root`, gw can't know where Task state should live. We raise `GoblinError` and tell the user to `gw project new` first.
- **`--from` ignored for `--branch`.** Existing branches have a base whether we like it or not. `--from` applies to fresh-branch creation — `--branch-name`, `--branch-auto`, `--linear`, and `--issue` (all of which create a fresh branch from the ticket slug).
- **`gw gh-42` from outside a project.** The bare-number form has no repo of its own, so it resolves through the cwd's project and then the picker. Run it from anywhere with exactly one project registered and it still works; with several and no cwd match, you get the picker.
- **`base_branch` field for non-Linear sources.** Set to `project.default_branch`, even though the actual base is unknowable for `--branch` / `--dir`. Good enough for the PR body template; if it ever matters for behaviour we'll need to track it more carefully.

## Code map

- `src/goblin_watcher/cli.py` — `_rewrite_task_shortcut` (the `gw gh-42` / `gw ENG-123` shorthands).
- `src/goblin_watcher/commands/new.py` — entry point, source dispatch, `--with-project`.
- `src/goblin_watcher/commands/task.py` — `gw task add-repo`, multi-repo teardown.
- `src/goblin_watcher/workspace.py` — workspace promotion + repo attachment.
- `src/goblin_watcher/slug.py` — `branch_slug`, `slugify`.
- `src/goblin_watcher/git.py` — `branch_exists`, `remote_branch_exists`, `create_branch_from_remote`, `worktree_add`, `main_repo_root`.
- `src/goblin_watcher/linear/client.py` — `parse_identifier`, `LinearClient.fetch_issue`.
- `src/goblin_watcher/gh.py` — `parse_issue_ref`, `issue_view`, `issue_state`, `normalize_repo`, `pr_review`, `check_run_log`.
- `src/goblin_watcher/github_state.py` — TTL-cached issue-state refresh.
- `src/goblin_watcher/review_feed.py` — PR-feedback gathering + the embedding bounds (ADR 0008).
- `src/goblin_watcher/agents/launcher.py` — `build_seed_prompt`, `format_review_block`.
- `src/goblin_watcher/templates/spawn_prompt.md` — the work brief.
- `src/goblin_watcher/templates/research_prompt.md` — the `--research` brief (ADR 0006).
- `src/goblin_watcher/templates/address_review_prompt.md` — the `--address-review` brief (ADR 0008).

## Tests

- `tests/test_slug.py` — slug rules for the branch name builder.
- `tests/test_stacked_tasks.py` — `--from` parent resolution and every surface that reads it.
- `tests/test_cli_new_sources.py` — end-to-end for `--branch-name`, `--branch`, `--dir`, `--pr`.
- `tests/test_cli_linear_flow.py` — end-to-end for `--linear` (with `pytest-httpx` mock).
- `tests/test_cli_issue_flow.py` — end-to-end for `--issue` and the `gw gh-42` shorthand.
- `tests/test_launcher.py` — `build_seed_prompt` for all three briefs.
- `tests/test_cli_run.py` — `gw run`, including `--research` and `--address-review` validation and seeding.
- `tests/test_review_feed.py` — PR resolution, the refusal cases, and the embedding bounds.
- `tests/test_gh_pr_review.py` — GraphQL response parsing for `pr_review` + `check_run_log`.
- `tests/test_gh_issues.py` — reference parsing and the `gh issue` wrappers.
- `tests/test_github_state.py` — the issue-state TTL + `gw status` rendering.
- `tests/test_git_worktree.py` — the `worktree_add` / branch-existence primitives.
