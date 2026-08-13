"""State-drift detection and the safely-repairable subset (gh-29).

`gw doctor`'s other checks answer "is the toolchain installed". This module
answers the question that actually bites in practice: has gw's recorded state
diverged from what git and the filesystem say? A worktree removed by hand, a
branch deleted after a squash-merge, `.git/info/exclude` clobbered by a
`git init` redo — none of those are errors gw can see at the moment they happen,
because nothing gw ran caused them.

Detection is read-only and never raises for a project it can't inspect: drift
reporting that crashes on the first broken project is worse than one that skips
it. Repair is deliberately narrow. A finding is repairable only when applying
the fix cannot destroy work:

- `missing-exclude` — re-append gw's patterns to the repo's local exclude file.
- `orphan-record`   — drop a task record whose worktree *and* branch are both
                      gone (a scratch record whose directory is gone). There is
                      nothing left for the record to point at.
- `stale-indicator` — drop indicator-cache rows for tasks that no longer exist.
                      The cache is derived and regenerable by construction.

Everything else — an untracked worktree, a task whose worktree vanished while
its branch survives, a branch deleted from under a live worktree — stays a
report. Each of those still has commits or files somewhere, and the same posture
governs sync's prune step, which refuses to force-delete a branch it can't prove
is safe (`sync/engine._prune_blocker`).
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal

from goblin_watcher import git, paths, state
from goblin_watcher.errors import GoblinError, ProjectNotFoundError
from goblin_watcher.models import Project, Task
from goblin_watcher.sync import store

DriftKind = Literal[
    "orphan-worktree",
    "orphan-record",
    "missing-worktree",
    "missing-branch",
    "missing-exclude",
    "stale-indicator",
]

# The only kinds whose repair cannot lose work. Enforced in `Finding`, so a new
# kind can't quietly opt itself into being auto-fixed.
REPAIRABLE_KINDS: frozenset[DriftKind] = frozenset(
    {"missing-exclude", "orphan-record", "stale-indicator"}
)

# What `gw project new` / `gw new` append to `<repo>/.git/info/exclude`. Kept in
# sync by `tests/test_cli_doctor_drift.py`, which asserts a freshly registered
# project reports no `missing-exclude` drift.
LOCAL_EXCLUDE_PATTERNS: tuple[str, ...] = (".goblin/", ".worktrees/")


@dataclass(frozen=True)
class Finding:
    """One piece of drift, with everything `repair` needs to act on it.

    `where` is the human-facing location (a path or `<project>/<task>`);
    `targets` carries the machine-readable operands — exclude patterns for
    `missing-exclude`, cache keys for `stale-indicator`.
    """

    kind: DriftKind
    where: str
    detail: str
    repairable: bool = False
    project: str | None = None
    task_id: str | None = None
    root: Path | None = None
    targets: tuple[str, ...] = field(default_factory=tuple)

    def __post_init__(self) -> None:
        if self.repairable and self.kind not in REPAIRABLE_KINDS:
            raise ValueError(f"{self.kind!r} drift is never safe to repair automatically")


@dataclass(frozen=True)
class RepairOutcome:
    finding: Finding
    fixed: bool
    detail: str


def detect(projects: Sequence[Project] | None = None) -> list[Finding]:
    """Every piece of drift gw can see, cheapest checks first.

    `projects` defaults to the whole registry; pass a subset only when the
    caller genuinely owns just those projects — the indicator-cache sweep uses
    the registry to tell "task deleted" from "project simply not examined".
    """
    projects = list(projects) if projects is not None else _registry_projects()
    tasks = {p.name: state.list_tasks(p) for p in projects}

    findings: list[Finding] = []
    known = _known_worktrees(tasks.values())
    for proj in projects:
        if proj.kind != "git":
            # The reserved scratch project is a plain directory tree: no repo,
            # no worktrees, no exclude file to check.
            continue
        findings.extend(_exclude_findings(proj))
        findings.extend(_orphan_worktree_findings(proj, known))
    for proj in projects:
        for task in tasks[proj.name]:
            findings.extend(_task_findings(proj, task))
    findings.extend(_stale_indicator_findings(projects, tasks))
    return findings


def repair(findings: Iterable[Finding]) -> list[RepairOutcome]:
    """Apply the safe fixes. Non-repairable findings are skipped, not raised on.

    The indicator cache is loaded and saved once for the whole pass, so a
    hundred stale rows cost one write.
    """
    cache = store.load_cache()
    cache_dirty = False
    outcomes: list[RepairOutcome] = []

    for finding in findings:
        if not finding.repairable:
            continue
        try:
            match finding.kind:
                case "missing-exclude":
                    outcomes.append(_repair_exclude(finding))
                case "orphan-record":
                    outcome, dropped_key = _repair_orphan_record(finding)
                    outcomes.append(outcome)
                    if dropped_key is not None and cache.entries.pop(dropped_key, None) is not None:
                        cache_dirty = True
                case "stale-indicator":
                    for key in finding.targets:
                        if cache.entries.pop(key, None) is not None:
                            cache_dirty = True
                    outcomes.append(
                        RepairOutcome(
                            finding=finding,
                            fixed=True,
                            detail=f"dropped {len(finding.targets)} cached indicator row(s)",
                        )
                    )
                case _:  # pragma: no cover - REPAIRABLE_KINDS is exhaustive above
                    continue
        except (GoblinError, OSError) as e:
            outcomes.append(RepairOutcome(finding=finding, fixed=False, detail=str(e)))

    if cache_dirty:
        store.save_cache(cache)
    return outcomes


# ---------------------------------------------------------------------------
# Detection


def _registry_projects() -> list[Project]:
    out: list[Project] = []
    for name in sorted(state.load_global().projects):
        try:
            out.append(state.get_project(name))
        except GoblinError:
            # A registry entry whose project.json is gone or unreadable. Out of
            # scope here; skipping keeps the rest of the report intact.
            continue
    return out


def _known_worktrees(task_lists: Iterable[list[Task]]) -> set[Path]:
    """Every worktree path any task claims, across every project.

    Global rather than per-project on purpose: a multi-repo task's secondary
    worktree lives in *that* repo's `.worktrees/` while the record lives with
    the primary project, so a per-project view would call it an orphan.
    """
    known: set[Path] = set()
    for tasks in task_lists:
        for task in tasks:
            for repo in task.all_repos():
                known.add(repo.worktree_path.resolve())
            if task.workspace_path is not None:
                known.add(task.workspace_path.resolve())
    return known


def _exclude_findings(proj: Project) -> list[Finding]:
    git_dir = proj.root / ".git"
    if not git_dir.is_dir():
        # No main-checkout `.git` directory to inspect (missing root, or a root
        # that is itself a linked worktree). Nothing to assert.
        return []
    exclude = git_dir / "info" / "exclude"
    present = set()
    if exclude.exists():
        try:
            present = {line.strip() for line in exclude.read_text(errors="replace").splitlines()}
        except OSError:
            return []
    missing = tuple(p for p in LOCAL_EXCLUDE_PATTERNS if p not in present)
    if not missing:
        return []
    return [
        Finding(
            kind="missing-exclude",
            where=str(exclude),
            detail=(
                f"{', '.join(missing)} missing from .git/info/exclude — "
                "gw's directories will show up in `git status`"
            ),
            # `git.add_to_local_exclude` no-ops when `.git/info` is absent, so
            # only promise a fix when the write will actually land.
            repairable=(git_dir / "info").is_dir(),
            project=proj.name,
            root=proj.root,
            targets=missing,
        )
    ]


def _orphan_worktree_findings(proj: Project, known: set[Path]) -> list[Finding]:
    """Worktrees under the project's worktree root that no task record claims.

    Scoped to `<project>/.worktrees/` (or the configured override) deliberately:
    that directory is gw's own, so an unclaimed entry there is real drift, while
    a worktree the user made elsewhere is none of gw's business.
    """
    root = paths.worktree_root(proj.root, proj.worktree_root).resolve()
    try:
        entries = git.worktree_list(proj.root)
    except GoblinError:
        return []

    findings: list[Finding] = []
    main = proj.root.resolve()
    for entry in entries:
        raw = entry.get("worktree")
        if not raw:
            continue
        path = Path(raw).resolve()
        if path == main or root not in path.parents:
            continue
        if path in known:
            continue
        gone = not path.exists()
        detail = (
            "git still lists this worktree but the directory is gone — "
            "`git worktree prune` clears the metadata"
            if gone
            else "git worktree with no task record — it may hold work gw doesn't track"
        )
        findings.append(
            Finding(
                kind="orphan-worktree",
                where=str(path),
                detail=detail,
                project=proj.name,
                root=proj.root,
            )
        )
    return findings


def _task_findings(proj: Project, task: Task) -> list[Finding]:
    where = f"{proj.name}/{task.id}"
    if task.kind == "scratch":
        if task.worktree_path.exists():
            return []
        return [
            Finding(
                kind="orphan-record",
                where=where,
                detail=f"scratch directory {task.worktree_path} is gone; no branch to preserve",
                repairable=True,
                project=proj.name,
                task_id=task.id,
            )
        ]

    findings: list[Finding] = []
    all_gone = True
    for repo in task.all_repos():
        repo_root = _repo_root(proj, repo.project)
        worktree_gone = not repo.worktree_path.exists()
        # An unregistered project is "can't tell", not "gone": never let that
        # ambiguity be the reason a record gets dropped.
        branch_gone = repo_root is not None and not _branch_exists(repo_root, repo.branch)
        all_gone = all_gone and worktree_gone and branch_gone
        if worktree_gone and not branch_gone:
            findings.append(
                Finding(
                    kind="missing-worktree",
                    where=where,
                    detail=(
                        f"worktree {repo.worktree_path} is gone but branch {repo.branch!r} "
                        "still has it; recreate with `gw new --rm` or remove with `gw task rm`"
                    ),
                    project=proj.name,
                    task_id=task.id,
                )
            )
        elif branch_gone and not worktree_gone:
            findings.append(
                Finding(
                    kind="missing-branch",
                    where=where,
                    detail=(
                        f"branch {repo.branch!r} no longer exists in {repo_root} but the "
                        f"worktree at {repo.worktree_path} is still there"
                    ),
                    project=proj.name,
                    task_id=task.id,
                )
            )

    if all_gone:
        # Both halves of every repo are gone, so the per-repo rows above would
        # only restate it: report the record itself, and offer the drop.
        return [
            Finding(
                kind="orphan-record",
                where=where,
                detail="worktree and branch are both gone; the record points at nothing",
                repairable=True,
                project=proj.name,
                task_id=task.id,
            )
        ]
    return findings


def _stale_indicator_findings(
    projects: Sequence[Project], tasks: dict[str, list[Task]]
) -> list[Finding]:
    entries = store.load_cache().entries
    if not entries:
        return []
    live = {store.cache_key(p.name, t.id) for p in projects for t in tasks[p.name]}
    examined = {p.name for p in projects}
    registered = set(state.load_global().projects)

    stale = [
        key
        for key in sorted(entries)
        if key not in live
        # A key for a project we didn't look at is only stale when that project
        # is gone from the registry too.
        and (key.split("/", 1)[0] in examined or key.split("/", 1)[0] not in registered)
    ]
    if not stale:
        return []
    shown = ", ".join(stale[:3]) + (" …" if len(stale) > 3 else "")
    return [
        Finding(
            kind="stale-indicator",
            where=str(paths.sync_cache_file()),
            detail=f"{len(stale)} cached indicator row(s) for tasks that no longer exist: {shown}",
            repairable=True,
            targets=tuple(stale),
        )
    ]


def _repo_root(primary: Project, project_name: str) -> Path | None:
    if project_name == primary.name:
        return primary.root
    try:
        return state.get_project(project_name).root
    except ProjectNotFoundError:
        return None


def _branch_exists(repo_root: Path, branch: str) -> bool:
    if not (repo_root / ".git").exists():
        # The repo itself is gone; "branch missing" would be true but says
        # nothing useful, and git would just error.
        return False
    return git.branch_exists(repo_root, branch)


# ---------------------------------------------------------------------------
# Repair


def _repair_exclude(finding: Finding) -> RepairOutcome:
    root = finding.root
    if root is None:  # pragma: no cover - detection always sets it
        return RepairOutcome(finding=finding, fixed=False, detail="no project root recorded")
    for pattern in finding.targets:
        git.add_to_local_exclude(root, pattern)
    joined = ", ".join(finding.targets)
    return RepairOutcome(
        finding=finding, fixed=True, detail=f"re-appended {joined} to .git/info/exclude"
    )


def _repair_orphan_record(finding: Finding) -> tuple[RepairOutcome, str | None]:
    if finding.project is None or finding.task_id is None:  # pragma: no cover - always set
        return RepairOutcome(finding=finding, fixed=False, detail="no task recorded"), None
    proj = state.get_project(finding.project)
    state.delete_task_record(proj, finding.task_id)
    return (
        RepairOutcome(
            finding=finding, fixed=True, detail=f"dropped task record {finding.task_id!r}"
        ),
        store.cache_key(finding.project, finding.task_id),
    )
