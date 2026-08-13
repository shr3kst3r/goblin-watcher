# Inspecting changes

Current-state design of the read-only diff surface: `gw diff` and the
`gw status --diffstat` annotation. Both live in `commands/diff.py`.

## The problem

Reviewing what N parallel agents did meant N `cd`s into N worktrees and N
`git diff`s. Everything needed to answer it is already in the task record — the
branch, the base branch, the worktree path — so the question can be answered
from anywhere.

## Shape

```
gw diff [PATH|TASK-ID] [--project NAME] [--repo NAME] [--base REF]
        [--stat] [--uncommitted|--no-uncommitted] [--pager|--no-pager]
gw status --diffstat
```

Task resolution goes through `task_resolver.resolve_task`, the same chain
`gw cd` uses: explicit id or path → the cwd's task → project picker → task
picker. Multi-repo tasks render one section per repo (`Task.all_repos()`),
narrowable with `--repo`.

Per repo, `diff.collect` gathers, one git call per fact:

| fact | call |
| --- | --- |
| commits on the branch | `git.commits_between(root, base, branch)` |
| committed diffstat | `git.diff_range(root, f"{base}...{branch}", stat=True)` |
| committed patch (skipped under `--stat`) | `git.diff_range(root, range_spec)` |
| uncommitted diffstat + patch | `git.diff_range(worktree, "HEAD", …)` |
| untracked files | `git.untracked_files(worktree)` |

`git.diff_range` is the single primitive — `git diff [--stat] <range_spec>`,
returning `''` on any git error. `git.diffstat` (used by the PR body builder)
is a thin wrapper over it.

## Three decisions

**Three-dot, not two-dot.** The committed range is `base...branch`: the
merge-base comparison a PR shows. With two-dot, every commit the base branch
gained after the task started renders as a deletion — actively misleading for
"what did this agent change". `commands/pr._pr_body` still uses two-dot for its
own diffstat; that's untouched, and worth revisiting separately.

**Read from the project root, not the worktree.** Refs are shared between a
repo and its worktrees, so the committed diff works with no checkout at all.
That's what makes an archived task (`gw task archive`: worktree dropped, branch
and record kept) still diffable — only the uncommitted overlay needs the
worktree, and its absence is reported as a note rather than an error.

**Uncommitted work is a separate section.** A live agent usually hasn't
committed anything yet, so a view that only showed `base...branch` would report
"no changes" precisely when you most want to look. Untracked files are listed by
name because `git diff` cannot see them at all.

## Degradation

Nothing here raises on a repo in an unexpected state; each case is a flag on
`RepoChanges` that the renderer explains:

- **no worktree** (archived, or a checkout deleted behind gw's back) — committed
  diff renders; the missing uncommitted overlay is reported with a `gw run <id>`
  pointer. Keyed on the directory, not on `Task.archived`, so both cases are
  covered.
- **deleted branch** (merged and pruned) — says so, exits zero.
- **unresolvable base ref** (a parent task's branch that landed) — says so,
  suggests `--base main`.
- **scratch task** — refused up front with a `GoblinError`; a plain directory
  has no branch to compare.

## `gw status --diffstat`

`diff.status_suffix(proj, task)` appends `+N -N · N files` to a task's row,
summed across its repos. Deliberately narrower than `gw diff`:

- committed range only. Uncommitted work already has the `● uncommitted`
  indicator next to it, and including it would double the subprocess count per
  row.
- uncached, one `git diff --stat` per repo per render — including per tick under
  `--watch`, unlike the `gw sync`-cached indicators. Opt-in, so a plain
  `gw status` is unaffected.
- returns `''` for scratch tasks and for repos whose project record has gone
  missing, so a broken row still renders.

## Output

Rich all the way (`console`, never `print`). Patches are built as a single
`rich.text.Text` with a style per line, which also means markup inside the diff
is never interpreted. `soft_wrap=True` everywhere: Rich falls back to 80 columns
when stdout isn't a terminal, and wrapping a patch there would corrupt it.
Paging is on when stdout is a terminal (`console.pager`), off otherwise, so
redirecting to a file or a test runner just works.

The output is a human view — headings and totals interleaved with the patch —
not a clean patch stream. `git format-patch`-style output would be a separate
flag if someone needs it.
