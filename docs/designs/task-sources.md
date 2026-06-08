# Task Sources

Current-state design of how `gw new` creates a Task from one of four input sources.

## Why four sources

A Task always carries a branch + worktree + optional Linear issue. The four input shapes match real workflows:

- **Linear ticket** — the daily driver. The ticket title drives the branch slug; the body becomes the seed prompt.
- **Fresh branch by name** — exploring an idea without a ticket; the user supplies a slug and (optionally) a base.
- **Existing branch** — picking up a teammate's work or a review fix-up. The branch already exists locally or on the remote.
- **Existing directory** — adopting a worktree someone else (or a previous gw run on a different machine) already created.

The four sources collapse to the same Task shape, so downstream commands (`run`, `pr`, `task show`, `status`) don't need to care which source produced a task.

## Resolution flow

```
                    gw new ...
                        │
                ┌───────┴───────┐
                │ source flag?  │
                └───────┬───────┘
        ┌───────┬───────┼───────┬────────┐
   --linear  --branch-name  --branch  --dir
        │       │           │         │
   _from_linear │       _from_existing │
        │   _from_new_branch  _branch  │
        │       │           │   _from_existing_dir
        └───────┴───────┬───┴─────────┘
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

### `--branch-name <name>`

1. Resolve project (via `--project NAME`, or the interactive picker — `task_resolver.resolve_project` auto-picks when only one is registered).
2. Final branch = `{project.branch_prefix}{name}` with collision suffix (`-2`, `-3`, ...) if needed.
3. Base = `--from <branch>` or `project.default_branch`. If `--from` names a branch that's only on origin, gw fetches and creates a local tracking branch before the worktree add.
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

`agents/launcher.build_seed_prompt(task)` uses `templates/spawn_prompt.md`:

- For `--linear` tasks: identifier + title + description.
- For `--branch-name` / `--branch` / `--dir`: identifier slot becomes `task.id.upper()`, title slot becomes `task.id`, description slot becomes `(no Linear issue attached — fresh task)` plus whatever the user passed as `--title`.

This means a non-Linear task gets a thinner prompt — that's correct; the user has more responsibility for orienting the agent when they didn't go through Linear.

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
- **`--from` ignored for `--branch`.** Existing branches have a base whether we like it or not. `--from` applies to fresh-branch creation — `--branch-name`, `--branch-auto`, and `--linear` (which always creates a fresh branch from the ticket slug).
- **`base_branch` field for non-Linear sources.** Set to `project.default_branch`, even though the actual base is unknowable for `--branch` / `--dir`. Good enough for the PR body template; if it ever matters for behaviour we'll need to track it more carefully.

## Code map

- `src/goblin_watcher/commands/new.py` — entry point, source dispatch, `--with-project`.
- `src/goblin_watcher/commands/task.py` — `gw task add-repo`, multi-repo teardown.
- `src/goblin_watcher/workspace.py` — workspace promotion + repo attachment.
- `src/goblin_watcher/slug.py` — `branch_slug`, `slugify`.
- `src/goblin_watcher/git.py` — `branch_exists`, `remote_branch_exists`, `create_branch_from_remote`, `worktree_add`, `main_repo_root`.
- `src/goblin_watcher/linear/client.py` — `parse_identifier`, `LinearClient.fetch_issue`.
- `src/goblin_watcher/agents/launcher.py` — `build_seed_prompt`.

## Tests

- `tests/test_slug.py` — slug rules for the branch name builder.
- `tests/test_cli_new_sources.py` — end-to-end for `--branch-name`, `--branch`, `--dir`.
- `tests/test_cli_linear_flow.py` — end-to-end for `--linear` (with `pytest-httpx` mock).
- `tests/test_git_worktree.py` — the `worktree_add` / branch-existence primitives.
