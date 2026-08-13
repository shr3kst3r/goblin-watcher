"""`gw diff` — see what an agent changed on a task's branch, without cd-ing in.

Four agents running in parallel used to mean four `cd`s to find out what they
did. This renders the same thing from wherever you are: the commits on the
branch, the diffstat, and the patch, per repo for a multi-repo task.

Two deliberate choices:

* **Diffs are computed in the project root, not the worktree.** Refs are shared
  between a repo and its worktrees, so reading them from the main checkout means
  an archived task (worktree dropped, branch kept — `gw task archive`) still
  diffs fine. The worktree is only consulted for uncommitted work, and only when
  it's actually there.
* **The committed range is three-dot (`base...branch`).** That's the merge-base
  comparison a PR shows. Two-dot would report every commit the base branch has
  gained since as a reversion, which is exactly wrong for "what did this agent
  change". (`_pr_body` in `commands/pr.py` still uses two-dot for its diffstat;
  changing that is a separate call.)
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

import typer
from rich.markup import escape
from rich.text import Text

from goblin_watcher import git, state
from goblin_watcher.completion_enumerators import complete_projects, complete_tasks
from goblin_watcher.console import console
from goblin_watcher.errors import GoblinError
from goblin_watcher.models import Project, Task, TaskRepo
from goblin_watcher.task_resolver import resolve_task


@dataclass(frozen=True)
class Totals:
    """Files-changed / insertions / deletions, parsed out of a diffstat."""

    files: int
    insertions: int
    deletions: int

    def __add__(self, other: Totals) -> Totals:
        return Totals(
            files=self.files + other.files,
            insertions=self.insertions + other.insertions,
            deletions=self.deletions + other.deletions,
        )

    @property
    def one_line(self) -> str:
        noun = "file" if self.files == 1 else "files"
        return f"+{self.insertions} -{self.deletions} · {self.files} {noun}"


# git's diffstat footer: " 7 files changed, 120 insertions(+), 4 deletions(-)".
# Either count can be absent (a pure-addition diff has no deletions line), and a
# mode-change-only diff reports zero of both.
_SUMMARY = re.compile(
    r"^\s*(?P<files>\d+) files? changed"
    r"(?:,\s*(?P<insertions>\d+) insertions?\(\+\))?"
    r"(?:,\s*(?P<deletions>\d+) deletions?\(-\))?"
)


def parse_totals(stat: str) -> Totals | None:
    """Totals from the last line of `git diff --stat` output, or None if absent.

    None means "no changes" (git prints nothing at all for an empty diff) or
    output we don't recognize — both render as "nothing to show" rather than
    as zeroes, which would read like a real, empty result.
    """
    lines = [line for line in stat.splitlines() if line.strip()]
    if not lines:
        return None
    match = _SUMMARY.match(lines[-1])
    if match is None:
        return None
    return Totals(
        files=int(match.group("files")),
        insertions=int(match.group("insertions") or 0),
        deletions=int(match.group("deletions") or 0),
    )


@dataclass(frozen=True)
class RepoChanges:
    """One repo's changes on a task's branch, relative to `base`.

    `range_spec` is kept so the renderer can ask git for the full patch of the
    same range it already summarized, instead of `--stat` mode paying to
    generate a patch nobody asked for.
    """

    repo: TaskRepo
    root: Path
    base: str
    range_spec: str
    branch_present: bool
    base_present: bool
    commits: list[tuple[str, str, str]]
    stat: str
    totals: Totals | None
    uncommitted_wanted: bool
    worktree_present: bool
    dirty_stat: str
    dirty_totals: Totals | None
    untracked: list[str]

    @property
    def is_empty(self) -> bool:
        """Nothing committed and nothing uncommitted — no diff to show at all."""
        return not self.stat and not self.dirty_stat and not self.untracked


def _repo_root(repo: TaskRepo, proj: Project) -> Path:
    """Project root holding `repo`'s refs — the main checkout, not the worktree.

    Same lookup as `commands/pr._repo_root`, for a different reason: here it's
    what lets an archived task still be diffed.
    """
    if repo.project == proj.name:
        return proj.root
    return state.get_project(repo.project).root


def collect(
    repo: TaskRepo,
    root: Path,
    *,
    base: str | None = None,
    uncommitted: bool = True,
) -> RepoChanges:
    """Gather everything `gw diff` renders for one repo. One git call per fact.

    Degrades instead of raising: a deleted branch, a base ref that vanished when
    a parent task landed, and a missing worktree each come back as a flag on the
    result for the renderer to explain.
    """
    base_ref = base or repo.base_branch
    branch_present = git.branch_exists(root, repo.branch)
    base_present = git.commit_exists(root, base_ref)
    range_spec = f"{base_ref}...{repo.branch}"

    commits: list[tuple[str, str, str]] = []
    stat = ""
    if branch_present and base_present:
        commits = git.commits_between(root, base_ref, repo.branch)
        stat = git.diff_range(root, range_spec, stat=True)

    worktree_present = repo.worktree_path.is_dir() and git.is_git_repo(repo.worktree_path)
    dirty_stat = ""
    untracked: list[str] = []
    if uncommitted and worktree_present:
        dirty_stat = git.diff_range(repo.worktree_path, "HEAD", stat=True)
        untracked = git.untracked_files(repo.worktree_path)

    return RepoChanges(
        repo=repo,
        root=root,
        base=base_ref,
        range_spec=range_spec,
        branch_present=branch_present,
        base_present=base_present,
        commits=commits,
        stat=stat,
        totals=parse_totals(stat),
        uncommitted_wanted=uncommitted,
        worktree_present=worktree_present,
        dirty_stat=dirty_stat,
        dirty_totals=parse_totals(dirty_stat),
        untracked=untracked,
    )


def status_suffix(proj: Project, task: Task) -> str:
    """The `gw status --diffstat` fragment for one task's line. '' when there's nothing.

    Committed range only, one `git diff --stat` per repo. Uncommitted work is
    already flagged by the `● uncommitted` indicator rendered right next to it,
    and folding it in here would double the subprocess count per row. Multi-repo
    tasks report the sum across their repos.
    """
    if task.kind == "scratch":
        return ""
    total: Totals | None = None
    for repo in task.all_repos():
        try:
            root = _repo_root(repo, proj)
            stat = git.diff_range(root, f"{repo.base_branch}...{repo.branch}", stat=True)
        except GoblinError:
            # Unregistered secondary project, unreadable record — the row still
            # renders, just without this repo's numbers.
            continue
        repo_totals = parse_totals(stat)
        if repo_totals is not None:
            total = repo_totals if total is None else total + repo_totals
    if total is None:
        return ""
    return f"  [muted]{total.one_line}[/]"


# ---------- rendering -----------------------------------------------------------


def _line_style(line: str) -> str:
    """Rich style for one patch line. Prefix order matters: `+++` before `+`."""
    if line.startswith(("diff --git", "diff --cc")):
        return "bold"
    if line.startswith(("index ", "new file mode", "deleted file mode", "similarity index")):
        return "muted"
    if line.startswith("+++") or line.startswith("---"):
        return "bold"
    if line.startswith("@@"):
        return "cyan"
    if line.startswith("+"):
        return "green"
    if line.startswith("-"):
        return "red"
    return ""


def _patch_text(patch: str) -> Text:
    """`patch` as a styled `Text`. Built as one object so markup in the diff
    (a `[muted]` literal in someone's code) is never interpreted."""
    out = Text()
    for line in patch.splitlines():
        out.append(line + "\n", style=_line_style(line) or None)
    return out


def _print_patch(patch: str) -> None:
    # soft_wrap: Rich falls back to an 80-column width when stdout isn't a
    # terminal, and wrapping a patch there would corrupt it.
    console.print(_patch_text(patch), soft_wrap=True)


def _print_stat(stat: str) -> None:
    console.print(Text(stat), soft_wrap=True)


def _task_heading(task: Task) -> str:
    title = task.ticket_title
    suffix = f" · {escape(title)}" if title else ""
    return f"[bold]{escape(task.id)}[/]{suffix}  [muted]({task.status})[/]"


def _repo_heading(changes: RepoChanges) -> str:
    repo = changes.repo
    parts = [f"[bold]{escape(repo.project)}[/]", f"{escape(repo.branch)} vs {escape(changes.base)}"]
    if changes.commits:
        n = len(changes.commits)
        parts.append(f"{n} commit{'' if n == 1 else 's'}")
    if changes.totals is not None:
        parts.append(changes.totals.one_line)
    return "[muted] · [/]".join(parts)


def _render_repo(changes: RepoChanges, task: Task, *, stat_only: bool) -> None:
    console.print()
    console.print(_repo_heading(changes))

    if not changes.branch_present:
        console.print(
            f"[hint]Branch {changes.repo.branch!r} no longer exists in {changes.root}[/] "
            "[muted]— merged and pruned, or deleted by hand.[/]"
        )
    elif not changes.base_present:
        console.print(
            f"[hint]Base ref {changes.base!r} can't be resolved[/] [muted]— a parent task's "
            "branch that has since landed? Pass `--base main` to compare against something "
            "that exists.[/]"
        )

    for _sha, subject, _body in changes.commits:
        console.print(f"  [muted]·[/] {escape(subject)}")

    if changes.stat:
        console.print()
        _print_stat(changes.stat)
        if not stat_only:
            console.print()
            _print_patch(git.diff_range(changes.root, changes.range_spec))
    elif changes.branch_present and changes.base_present:
        console.print("[muted]No committed changes on this branch.[/]")

    _render_uncommitted(changes, task, stat_only=stat_only)


def _render_uncommitted(changes: RepoChanges, task: Task, *, stat_only: bool) -> None:
    """The working-tree overlay: what the agent has done but not committed yet.

    Rendered separately from the committed range because it is a different
    question with a different answer — and for a live agent it's usually where
    all the work is.

    With no worktree to read, say so rather than implying a clean tree. Keyed on
    the directory actually being there, not on `Task.archived`: a checkout
    deleted behind gw's back leaves the flag unset and the tree just as gone.
    """
    if not changes.uncommitted_wanted:
        return
    if not changes.worktree_present:
        console.print()
        if task.archived:
            console.print(
                "[muted]Archived, so there's no worktree to read uncommitted work from. "
                f"`gw run {escape(task.id)}` rematerializes it.[/]"
            )
        else:
            console.print(
                f"[muted]No worktree at {changes.repo.worktree_path}, so uncommitted work "
                f"can't be shown. `gw run {escape(task.id)}` recreates it.[/]"
            )
        return
    if not changes.dirty_stat and not changes.untracked:
        return
    console.print()
    label = "[bold yellow]Uncommitted[/]"
    if changes.dirty_totals is not None:
        label += f"[muted] · {changes.dirty_totals.one_line}[/]"
    console.print(label)
    if changes.dirty_stat:
        console.print()
        _print_stat(changes.dirty_stat)
        if not stat_only:
            console.print()
            _print_patch(git.diff_range(changes.repo.worktree_path, "HEAD"))
    if changes.untracked:
        console.print()
        console.print("[muted]Untracked (not in the diff above):[/]")
        for path in changes.untracked:
            console.print(f"  [green]?[/] {escape(path)}")


def diff(
    target: str | None = typer.Argument(
        None,
        help="Task id or path. Defaults to the cwd's task; opens a picker if there isn't one.",
        autocompletion=complete_tasks,
    ),
    project: str | None = typer.Option(
        None,
        "--project",
        help="Limit task lookup and the picker to a single project.",
        autocompletion=complete_projects,
    ),
    repo: str | None = typer.Option(
        None,
        "--repo",
        help="For a multi-repo task, diff only this repo's project.",
        autocompletion=complete_projects,
    ),
    base: str | None = typer.Option(
        None,
        "--base",
        help="Compare against this ref instead of the task's recorded base branch "
        "(e.g. `origin/main`).",
    ),
    stat: bool = typer.Option(
        False,
        "--stat",
        help="Summary only: commits and the diffstat, no patch.",
    ),
    uncommitted: bool = typer.Option(
        True,
        "--uncommitted/--no-uncommitted",
        help="Include the worktree's uncommitted changes (default on).",
    ),
    pager: bool = typer.Option(
        True,
        "--pager/--no-pager",
        help="Page the output when stdout is a terminal (default on).",
    ),
) -> None:
    """Show what a task's branch changed: commits, diffstat, and patch.

    Read from the project's main checkout, so an archived task still diffs — its
    branch outlives its worktree. Output is a human view, not a clean patch:
    `--stat` for the summary, `--no-uncommitted` for just the branch.
    """
    project_filter: str | None = None
    if project is not None:
        normalized = project.strip().lower()
        state.get_project(normalized)
        project_filter = normalized

    task = resolve_task(target, project_filter)
    if task.kind == "scratch":
        raise GoblinError(
            f"Task {task.id!r} is a scratch space — a plain directory with no git repo, "
            "so there's nothing to diff.",
            hint=f"`gw cd {task.id}` to look around it instead.",
        )
    proj = state.get_project(task.project)

    repos = task.all_repos()
    if repo is not None:
        wanted = repo.strip().lower()
        repos = [r for r in repos if r.project == wanted]
        if not repos:
            raise GoblinError(
                f"Task {task.id!r} has no repo for project {wanted!r}.",
                hint="Run `gw task show` to see the task's repos.",
            )

    collected = [collect(r, _repo_root(r, proj), base=base, uncommitted=uncommitted) for r in repos]

    def emit() -> None:
        console.print(_task_heading(task))
        for changes in collected:
            _render_repo(changes, task, stat_only=stat)
        if all(c.is_empty for c in collected):
            console.print()
            console.print("[muted]Nothing to show — no changes against the base branch.[/]")

    if pager and console.is_terminal:
        with console.pager(styles=True):
            emit()
    else:
        emit()
