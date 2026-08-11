"""One sync pass: refresh, cache, prune, notify (ADR 0005).

A pass is short-lived and idempotent. It holds a machine-wide single-instance
lock so a scheduled firing that overlaps a still-running pass exits immediately
rather than doubling the work. Every step is isolated: a `GoblinError` is
journaled and the pass continues, because one unreachable remote must not stop
the other twenty tasks from being refreshed.

Steps that touch the network or the filesystem broadly run *outside* any task
lock; only narrow patches are applied under one (ADR 0004).
"""

from __future__ import annotations

import contextlib
import shutil
import uuid
from collections.abc import Callable
from datetime import UTC, datetime, timedelta

from goblin_watcher import config, description, gh, git, github_state, sessions, state
from goblin_watcher.commands.task import (
    destroy_task,
    dirty_worktrees,
    merge_detection,
    scratch_last_activity,
)
from goblin_watcher.errors import GoblinError, ProjectNotFoundError, TaskNotFoundError
from goblin_watcher.linear_state import LinearStateFetcher
from goblin_watcher.models import Project, Task, TaskRepo
from goblin_watcher.sync import journal, store
from goblin_watcher.sync.models import (
    DescriptionBackoff,
    IndicatorCache,
    PassReport,
    StepName,
    StepOutcome,
    SyncState,
    TaskIndicators,
)
from goblin_watcher.sync.notify import Notifier, resolve

# A session whose description has failed this many times is retried only once an
# hour instead of every pass — the negative-caching gap in the lazy path.
_BACKOFF_FAILURE_THRESHOLD = 3
_BACKOFF_RETRY_SECONDS = 3600

# Event key suffixes in `SyncState.last_seen`.
_SIG_PR_STATE = "pr-state"
_SIG_CHECKS = "checks"
_SIG_ACTIVITY = "activity"
_SIG_PRUNABLE = "prunable"


def _now() -> datetime:
    return datetime.now(UTC)


class _Steps:
    """Accumulates per-step ok/failed tallies for the pass report."""

    def __init__(self) -> None:
        self._outcomes: dict[StepName, StepOutcome] = {}

    def ok(self, step: StepName, n: int = 1) -> None:
        self._get(step).ok += n

    def failed(self, step: StepName, n: int = 1) -> None:
        self._get(step).failed += n

    def _get(self, step: StepName) -> StepOutcome:
        if step not in self._outcomes:
            self._outcomes[step] = StepOutcome(step=step)
        return self._outcomes[step]

    def as_list(self) -> list[StepOutcome]:
        return list(self._outcomes.values())


def run_pass(
    *,
    verbose: bool = False,
    on_event: Callable[[str, str], None] | None = None,
    project_name: str | None = None,
) -> PassReport:
    """Execute one sync pass. Returns a report; never raises `GoblinError`.

    `on_event(level, message)` receives a human-readable line per action so the
    foreground `gw sync` can narrate; the journal records the same events for
    scheduled runs.
    """
    from goblin_watcher import locks, paths

    pass_id = uuid.uuid4().hex[:12]
    report = PassReport(pass_id=pass_id)
    steps = _Steps()

    def emit(
        level: journal.Level,
        event: str,
        *,
        project: str | None = None,
        task: str | None = None,
        detail: str | None = None,
    ) -> None:
        journal.append(
            pass_id=pass_id,
            level=level,
            event=event,
            project=project,
            task=task,
            detail=detail,
        )
        if on_event is not None and verbose:
            where = f"{project}/{task}" if project and task else (project or "")
            line = f"{event}{f' [{where}]' if where else ''}"
            on_event(level, f"{line}{f' — {detail}' if detail else ''}")

    def finish() -> None:
        """Journal the outcome and record it as the last pass. Runs under the lock."""
        report.steps = steps.as_list()
        report.finished_at = _now()
        emit(
            "info",
            "pass-end",
            detail=f"{report.status}; {report.tasks} task(s) in {report.duration_seconds:.1f}s"
            if report.duration_seconds
            else report.status,
        )
        st = store.load_state()
        st.last_pass = report
        store.save_state(st)

    # Acquisition is the only thing that may report "skipped": a `LockTimeoutError`
    # raised from *inside* the pass means a wedged per-task lock, which is a real
    # error and must not be disguised as "another pass is running".
    with contextlib.ExitStack() as stack:
        try:
            stack.enter_context(locks.exclusive(paths.sync_lock_file(), timeout=0))
        except locks.LockTimeoutError:
            report.status = "skipped"
            report.finished_at = _now()
            emit("info", "pass-skipped", detail="another pass holds the single-instance lock")
            return report

        emit("info", "pass-start")
        try:
            _execute(
                report=report,
                steps=steps,
                emit=emit,
                project_name=project_name,
            )
        except Exception as e:
            # Not an expected per-step failure but a bug. Record it so the
            # journal and `gw sync status` show a crashed pass rather than
            # silently reporting the previous one, then let it propagate —
            # exit 1, with a traceback in the launchd log.
            report.status = "error"
            report.errors.append(f"{type(e).__name__}: {e}")
            emit("error", "pass-crashed", detail=f"{type(e).__name__}: {e}")
            finish()
            raise
        # A task-level crash already set "error"; don't downgrade it. "partial"
        # is for expected per-step failures (unreachable remote, absent `gh`).
        if report.errors and report.status == "ok":
            report.status = "partial"
        finish()
    return report


def _execute(
    *,
    report: PassReport,
    steps: _Steps,
    emit: Callable[..., None],
    project_name: str | None,
) -> None:
    cfg = config.load()
    st = store.load_state()
    cache = store.load_cache()
    notifier = resolve(cfg.sync)
    linear = LinearStateFetcher()
    # Degradations worth journaling once per pass rather than once per task
    # (a missing `gh` would otherwise emit a line for every PR-bearing task).
    warned: set[str] = set()

    projects = _resolve_projects(project_name, emit, report)
    report.projects = len(projects)
    # Everything actually visited this pass; the complement is dead state.
    live_tasks: set[str] = set()
    live_sessions: set[str] = set()

    try:
        for proj in projects:
            # 1. Git pre-warm, so merge detection and future worktrees see a
            #    fresh base. Skipped for scratch (no repo) and for local-only
            #    projects (`--dir` adoptions with no remote), where there is
            #    nothing to fetch from and a failure would be noise. A genuine
            #    network failure on a project that *does* have a remote is
            #    journaled but never fails the pass.
            if proj.kind != "scratch" and proj.repo_url:
                try:
                    git.fetch(proj.root)
                    steps.ok("fetch")
                except GoblinError as e:
                    steps.failed("fetch")
                    report.errors.append(f"{proj.name}: fetch: {e.message}")
                    emit("error", "fetch-failed", project=proj.name, detail=e.message)

            tasks = state.list_tasks(proj)
            # 1b. Every PR this project's tasks point at, looked up together.
            #     One batched query per repo replaces two `gh pr view` calls per
            #     task, so the pass's API cost stops scaling with task count.
            snapshots = _collect_pr_snapshots(tasks, emit, warned)

            for task in tasks:
                report.tasks += 1
                live_tasks.add(store.cache_key(proj.name, task.id))
                live_sessions.update(s.session_id for s in task.sessions)
                try:
                    _sync_task(
                        proj=proj,
                        task=task,
                        cfg=cfg,
                        st=st,
                        cache=cache,
                        notifier=notifier,
                        linear=linear,
                        snapshot=snapshots.get(task.pr_url or ""),
                        steps=steps,
                        emit=emit,
                        report=report,
                    )
                except Exception as e:
                    # Per-step `GoblinError`s are already handled inside
                    # `_sync_task`; reaching here means a bug or a corrupt
                    # record. Isolate it to this task so one bad record cannot
                    # wedge an unattended job, but flip the pass to `error` so
                    # it exits non-zero and is loud in the launchd log.
                    report.status = "error"
                    report.errors.append(f"{proj.name}/{task.id}: {type(e).__name__}: {e}")
                    emit(
                        "error",
                        "task-crashed",
                        project=proj.name,
                        task=task.id,
                        detail=f"{type(e).__name__}: {e}",
                    )

        # Full-scope passes own the whole keyspace, so anything not visited
        # above belongs to a task that no longer exists. Sweeping here is what
        # keeps derived state honest for deletions sync didn't perform itself
        # (`gw task rm`, `gw new --rm`, a hand-deleted record): without it a
        # later task reusing the id inherits stale indicators and — worse —
        # stale edge-trigger memory, which silently suppresses its first
        # notification. Skipped on a scoped pass, which only saw one project.
        if project_name is None:
            _sweep_dead_state(cache, st, live_tasks, live_sessions, emit)
    finally:
        if linear.disabled:
            emit(
                "info",
                "linear-disabled",
                detail="Linear API key unavailable; workflow-state refresh skipped this pass",
            )
        linear.close()
        store.save_cache(cache)
        # Persist edge-trigger memory and backoff even on a partial pass, so a
        # crash mid-pass doesn't re-notify everything next time.
        latest = store.load_state()
        latest.last_seen = st.last_seen
        latest.description_backoff = st.description_backoff
        store.save_state(latest)


def _sweep_dead_state(
    cache: IndicatorCache,
    st: SyncState,
    live_tasks: set[str],
    live_sessions: set[str],
    emit: Callable[..., None],
) -> None:
    """Drop derived state for tasks and sessions that no longer exist.

    Only ever called from a full-scope pass, where `live_tasks` is the complete
    set of `<project>/<task_id>` keys on disk. Derived state is regenerable by
    construction, so discarding a row we shouldn't have costs one recompute on
    the next pass — the asymmetry that makes sweeping safe.
    """
    dead_entries = [k for k in cache.entries if k not in live_tasks]
    for key in dead_entries:
        del cache.entries[key]

    # `last_seen` keys are "<project>/<task_id>:<signal>[:<session_id>]", and
    # neither project names nor task ids contain a colon (both are slugified),
    # so everything before the first one is the task key.
    dead_signals = [k for k in st.last_seen if k.split(":", 1)[0] not in live_tasks]
    for key in dead_signals:
        del st.last_seen[key]

    dead_backoff = [k for k in st.description_backoff if k not in live_sessions]
    for key in dead_backoff:
        del st.description_backoff[key]

    total = len(dead_entries) + len(dead_signals) + len(dead_backoff)
    if total:
        emit(
            "info",
            "swept-dead-state",
            detail=f"dropped {len(dead_entries)} cached indicator(s), "
            f"{len(dead_signals)} edge-trigger key(s), {len(dead_backoff)} backoff entry(ies)",
        )


def _resolve_projects(
    project_name: str | None, emit: Callable[..., None], report: PassReport
) -> list[Project]:
    if project_name:
        try:
            return [state.get_project(project_name.strip().lower())]
        except GoblinError as e:
            report.errors.append(e.message)
            emit("error", "project-unresolved", project=project_name, detail=e.message)
            return []
    out: list[Project] = []
    for name in sorted(state.load_global().projects):
        try:
            out.append(state.get_project(name))
        except ProjectNotFoundError:
            emit("error", "project-metadata-missing", project=name)
    return out


def _collect_pr_snapshots(
    tasks: list[Task], emit: Callable[..., None], warned: set[str]
) -> dict[str, gh.PrSnapshot]:
    """State + checks for every PR these tasks point at, keyed by PR URL.

    The result is *total* over PR-bearing tasks: every such task gets an entry,
    even when the lookup produced nothing (an all-None snapshot). That is what
    lets `_sync_task` distinguish "already looked up, don't look again" from
    "no PR here", and keeps the prune step from re-fetching what this already
    fetched.

    Three tiers, cheapest first:

    * A task already recorded as `merged` is not looked up at all. MERGED is the
      one terminal PR state, and a merged PR's CI result is not actionable — so
      a repo whose tasks have all landed costs zero API calls, forever, instead
      of two per task per pass. (CLOSED is deliberately *not* terminal: a closed
      PR can be reopened, and it rides along in the batch for free.)
    * github.com PRs are grouped by repo and fetched in one query each.
    * Anything else — a GitHub Enterprise host, an unparseable URL — falls back
      to the per-PR `pr_view` pair, which is what this code did for everything
      before batching.
    """
    by_repo: dict[str, dict[int, str]] = {}
    out: dict[str, gh.PrSnapshot] = {}
    fallback: list[str] = []

    for task in tasks:
        url = task.pr_url
        if not url or task.kind == "scratch":
            continue
        if task.status == "merged":
            out[url] = gh.PrSnapshot(state="MERGED", checks=None)
            continue
        parsed = gh.parse_pr_url(url)
        if parsed is None:
            fallback.append(url)
            continue
        repo, number = parsed
        by_repo.setdefault(repo, {})[number] = url

    if not by_repo and not fallback:
        return out

    # Journal a missing `gh` once per pass rather than once per PR, but don't
    # branch on it: each lookup below already answers "no signal" on its own
    # when the binary is absent.
    if shutil.which("gh") is None and "gh" not in warned:
        warned.add("gh")
        emit(
            "info",
            "gh-unavailable",
            detail="`gh` is not on PATH; PR state and CI checks skipped this pass",
        )

    for repo, urls in by_repo.items():
        found = gh.pr_snapshots(repo, list(urls))
        for number, url in urls.items():
            out[url] = found.get(number, gh.PrSnapshot())

    for url in fallback:
        out[url] = gh.PrSnapshot(state=gh.pr_state(url), checks=gh.pr_checks(url))

    return out


def _sync_task(
    *,
    proj: Project,
    task: Task,
    cfg: config.Config,
    st: SyncState,
    cache: IndicatorCache,
    notifier: Notifier,
    linear: LinearStateFetcher,
    snapshot: gh.PrSnapshot | None,
    steps: _Steps,
    emit: Callable[..., None],
    report: PassReport,
) -> None:
    def guard(step: StepName, fn: Callable[[], None]) -> None:
        """Run one step; journal a failure and carry on."""
        try:
            fn()
            steps.ok(step)
        except GoblinError as e:
            steps.failed(step)
            report.errors.append(f"{proj.name}/{task.id}: {step}: {e.message}")
            emit("error", f"{step}-failed", project=proj.name, task=task.id, detail=e.message)

    current = task

    # 2. Linear workflow state (TTL-gated inside the fetcher).
    if current.linear is not None:

        def _linear() -> None:
            nonlocal current
            current = linear.refresh(proj, current)

        guard("linear", _linear)

    # 2b. GitHub issue open/closed state (TTL-gated inside the module).
    if current.github_issue is not None:

        def _github_issue() -> None:
            nonlocal current
            current = github_state.refresh(proj, current)

        guard("github-issue", _github_issue)

    # 3. Reconcile + snippet summaries. Discovery is outside the task lock.
    def _reconcile() -> None:
        nonlocal current
        plan = sessions.plan_reconciliation(current)
        refreshed = sessions.refresh_task_summaries(sessions.apply_reconciliation(current, plan))
        current = sessions.persist_refresh(proj, refreshed, plan if not plan.is_empty else None)

    guard("reconcile", _reconcile)

    # 4. Descriptions, inline (sync is already the background) with backoff.
    def _descriptions() -> None:
        nonlocal current
        current = _refresh_descriptions(proj, current, st, emit)

    guard("descriptions", _descriptions)

    # 5 + 6. PR state, checks, and the cached git indicators.
    def _indicators() -> None:
        nonlocal current
        current = _refresh_pr_and_indicators(
            proj=proj,
            task=current,
            snapshot=snapshot,
            cache=cache,
            st=st,
            notifier=notifier,
            cfg=cfg,
            emit=emit,
            report=report,
        )

    guard("indicators", _indicators)

    # 7. Prune, and 8. idle notification.
    guard("notify", lambda: _notify_activity(proj, current, st, notifier, cfg, emit, report))
    if cfg.sync.prune:
        guard(
            "prune",
            lambda: _maybe_prune(proj, current, snapshot, cfg, st, cache, notifier, emit, report),
        )


def _refresh_descriptions(
    proj: Project, task: Task, st: SyncState, emit: Callable[..., None]
) -> Task:
    """Refresh stale session descriptions inline, backing off persistent failures.

    Progress is measured, not inferred from the exit code: `description.apply`
    returns 0 both for a real refresh *and* for a graceful give-up (LLM
    unreachable, transcript unreadable, describe agent disabled) — which is
    exactly the failure the backoff exists to stop retrying every pass. The
    only reliable signal is whether `description_updated_at` actually moved.

    Returns the task as re-read after the attempts, so later steps see the
    descriptions this one wrote.
    """
    now = _now()
    attempted: list[tuple[str, datetime | None]] = []
    for s in task.sessions:
        backoff = st.description_backoff.get(s.session_id)
        if backoff and backoff.failures >= _BACKOFF_FAILURE_THRESHOLD:
            last = backoff.last_attempt
            if last.tzinfo is None:
                last = last.replace(tzinfo=UTC)
            if (now - last).total_seconds() < _BACKOFF_RETRY_SECONDS:
                continue
        if not description.should_refresh(s):
            continue
        description.apply(proj.name, task.id, s.session_id)
        attempted.append((s.session_id, s.description_updated_at))

    if not attempted:
        return task
    try:
        latest = state.load_task(proj, task.id)
    except TaskNotFoundError:
        return task

    by_id = {s.session_id: s for s in latest.sessions}
    for session_id, before in attempted:
        after = by_id.get(session_id)
        if after is None:
            # Session was removed while we ran; nothing left to back off from.
            st.description_backoff.pop(session_id, None)
            continue
        if after.description_updated_at != before:
            st.description_backoff.pop(session_id, None)
            emit("action", "description-refreshed", project=proj.name, task=task.id)
            continue
        prior = st.description_backoff.get(session_id)
        attempts = (prior.failures if prior else 0) + 1
        st.description_backoff[session_id] = DescriptionBackoff(failures=attempts, last_attempt=now)
        emit(
            "error",
            "description-failed",
            project=proj.name,
            task=task.id,
            detail=f"attempt {attempts}",
        )
    return latest


def _refresh_pr_and_indicators(
    *,
    proj: Project,
    task: Task,
    snapshot: gh.PrSnapshot | None,
    cache: IndicatorCache,
    st: SyncState,
    notifier: Notifier,
    cfg: config.Config,
    emit: Callable[..., None],
    report: PassReport,
) -> Task:
    """Recompute this task's derived indicators and cache them.

    Scratch tasks have no repo, branch, or PR — they get no cache entry.
    """
    if task.kind == "scratch":
        return task

    indicators = TaskIndicators(computed_at=_now())

    # git facts, per repo; a multi-repo task rolls up (any dirty → dirty, sum of
    # ahead counts) so `gw status` can render one line per task. Ref questions go
    # to each repo's *own* project root — a secondary repo's branch does not
    # exist in the primary's object store (ADR 0003).
    for repo in task.all_repos():
        if not repo.worktree_path.exists():
            continue
        with contextlib.suppress(GoblinError):
            if git.has_uncommitted_changes(repo.worktree_path):
                indicators.uncommitted = True
        owner = _owning_project(proj, repo.project)
        if owner is None:
            continue
        with contextlib.suppress(GoblinError):
            if owner.repo_url and git.remote_branch_exists(owner.root, repo.branch):
                indicators.ahead += git.rev_list_count(
                    owner.root, f"origin/{repo.branch}..{repo.branch}"
                )
                indicators.ahead_vs_remote = True
            else:
                indicators.ahead += git.rev_list_count(
                    owner.root, f"{repo.base_branch}..{repo.branch}"
                )

    # PR state + checks, from the pass's batched lookup. Either field is None
    # when there was no signal (gh absent, API unreachable, PR deleted), which
    # leaves the previous edge-trigger value alone.
    updated = task
    if snapshot is not None:
        pr_state = snapshot.state
        if pr_state is not None:
            indicators.pr_state = pr_state
            _fire_pr_transition(proj, task, pr_state, st, notifier, cfg, emit, report)
            new_status = _status_from_pr_state(pr_state, task.status)
            if new_status != task.status:
                with contextlib.suppress(GoblinError):
                    updated = state.update_task(
                        proj,
                        task.id,
                        lambda latest, s=new_status: latest.model_copy(update={"status": s}),
                    )
                emit(
                    "action",
                    "task-status-updated",
                    project=proj.name,
                    task=task.id,
                    detail=new_status,
                )
        if snapshot.checks is not None:
            indicators.checks = snapshot.checks
            _fire_checks_transition(proj, task, snapshot.checks, st, notifier, cfg, emit, report)

    cache.entries[store.cache_key(proj.name, task.id)] = indicators
    return updated


def _owning_project(primary: Project, repo_project: str) -> Project | None:
    """Registered project that owns one repo on a task, or None if unregistered."""
    if repo_project == primary.name:
        return primary
    try:
        return state.get_project(repo_project)
    except GoblinError:
        return None


def _status_from_pr_state(pr_state: str, current: str) -> str:
    """Mirror of `commands.task._status_from_pr_state`, preserving terminal states."""
    if pr_state == "MERGED":
        return "merged"
    if pr_state == "CLOSED":
        return current if current == "merged" else "closed"
    if pr_state == "OPEN":
        return current if current in {"merged", "closed"} else "pr-open"
    return current


def _edge(st: SyncState, proj: Project, task: Task, signal: str, value: str) -> bool:
    """True when `value` differs from the last observed value for this signal.

    Records the new value either way, so an event fires exactly once per
    transition. A first observation counts as a transition only when it carries
    news — callers decide by checking the value itself.
    """
    key = f"{proj.name}/{task.id}:{signal}"
    previous = st.last_seen.get(key)
    st.last_seen[key] = value
    return previous != value


def _fire(
    *,
    event: str,
    title: str,
    body: str,
    cfg: config.Config,
    notifier: Notifier,
    proj: Project,
    task: Task,
    emit: Callable[..., None],
    report: PassReport,
) -> None:
    if event not in cfg.sync.notify_events:
        return
    report.notifications.append(f"{event}: {task.id}")
    emit("notify", event, project=proj.name, task=task.id, detail=body)
    notifier.send(title, body)


def _fire_pr_transition(
    proj: Project,
    task: Task,
    pr_state: str,
    st: SyncState,
    notifier: Notifier,
    cfg: config.Config,
    emit: Callable[..., None],
    report: PassReport,
) -> None:
    if not _edge(st, proj, task, _SIG_PR_STATE, pr_state):
        return
    if pr_state != "MERGED":
        return
    _fire(
        event="pr-merged",
        title=f"{task.id}: PR merged",
        body=f"{task.branch} was merged. `gw task prune` will clean it up.",
        cfg=cfg,
        notifier=notifier,
        proj=proj,
        task=task,
        emit=emit,
        report=report,
    )


def _fire_checks_transition(
    proj: Project,
    task: Task,
    checks: str,
    st: SyncState,
    notifier: Notifier,
    cfg: config.Config,
    emit: Callable[..., None],
    report: PassReport,
) -> None:
    if not _edge(st, proj, task, _SIG_CHECKS, checks):
        return
    if checks == "failing":
        _fire(
            event="checks-failed",
            title=f"{task.id}: checks failing",
            body=f"CI is failing on {task.branch}.",
            cfg=cfg,
            notifier=notifier,
            proj=proj,
            task=task,
            emit=emit,
            report=report,
        )
    elif checks == "passing":
        _fire(
            event="checks-passed",
            title=f"{task.id}: checks passing",
            body=f"CI is green on {task.branch}.",
            cfg=cfg,
            notifier=notifier,
            proj=proj,
            task=task,
            emit=emit,
            report=report,
        )


def _notify_activity(
    proj: Project,
    task: Task,
    st: SyncState,
    notifier: Notifier,
    cfg: config.Config,
    emit: Callable[..., None],
    report: PassReport,
) -> None:
    """Notify when an agent that *was* producing output has gone quiet.

    Derived from transcript mtime, the same signal as the `● active` badge. Only
    the active→idle edge fires: a session that has been idle for days is not
    news, and a first sighting is recorded without notifying.
    """
    threshold = cfg.defaults.activity_active_seconds
    now = _now()
    for s in task.sessions:
        mtime = description.transcript_mtime(s)
        if mtime is None:
            continue
        state_now = "active" if (now - mtime).total_seconds() <= threshold else "idle"
        key = f"{proj.name}/{task.id}:{_SIG_ACTIVITY}:{s.session_id}"
        previous = st.last_seen.get(key)
        st.last_seen[key] = state_now
        if previous == "active" and state_now == "idle":
            _fire(
                event="agent-idle",
                title=f"{task.id}: {s.agent} is idle",
                body=(s.description or s.summary or "Agent went quiet — may be waiting for input."),
                cfg=cfg,
                notifier=notifier,
                proj=proj,
                task=task,
                emit=emit,
                report=report,
            )


def _maybe_prune(
    proj: Project,
    task: Task,
    snapshot: gh.PrSnapshot | None,
    cfg: config.Config,
    st: SyncState,
    cache: IndicatorCache,
    notifier: Notifier,
    emit: Callable[..., None],
    report: PassReport,
) -> None:
    """Prune a task only when it is unambiguously safe (ADR 0005).

    Merged **and** clean. Never forces: a dirty worktree — or, on a multi-repo
    task, a secondary repo still holding unmerged commits — is reported via a
    `prunable` notification and left alone, because deleting in-flight work is
    not a cleanup this command is allowed to do.
    """
    if task.kind == "scratch":
        days = cfg.sync.scratch_prune_days
        if days <= 0:
            return
        idle = _now() - scratch_last_activity(task)
        if idle < timedelta(days=days):
            return
        reason = f"idle {idle.days}d"
    else:
        # The batched lookup above already answered "is this PR merged?" for
        # this exact task; re-asking `gh` here is the duplicate round-trip that
        # made every PR-bearing task cost three calls a pass instead of two.
        detected = merge_detection(proj, task, snapshot=snapshot)
        if detected is None:
            return
        reason = detected
        blocker = _prune_blocker(proj, task)
        if blocker is not None:
            kind, detail = blocker
            if _edge(st, proj, task, _SIG_PRUNABLE, f"{kind}:{detail}"):
                _fire(
                    event="prunable",
                    title=f"{task.id}: merged but not clean",
                    body=f"Branch is merged but {detail}; not pruned automatically.",
                    cfg=cfg,
                    notifier=notifier,
                    proj=proj,
                    task=task,
                    emit=emit,
                    report=report,
                )
            emit("info", "prune-skipped", project=proj.name, task=task.id, detail=detail)
            return

    destroy_task(proj, task, force=False)
    _forget_task(cache, st, proj, task)
    report.pruned.append(f"{proj.name}/{task.id}")
    emit("action", "task-pruned", project=proj.name, task=task.id, detail=reason)


def _prune_blocker(proj: Project, task: Task) -> tuple[str, str] | None:
    """Why this merged task must not be auto-pruned, or None when it is safe.

    `merge_detection` only inspects the primary repo, but `destroy_task`
    force-deletes *every* repo's branch (`git branch -D`). An unattended prune
    therefore has to check the secondaries itself: a branch carrying commits its
    base does not have is unmerged work, and losing it is not recoverable from
    the worktree deletion alone.
    """
    dirty = dirty_worktrees(task)
    if dirty:
        return ("dirty", f"the worktree has uncommitted changes ({dirty[0]})")
    unmerged = [r.project for r in task.secondary_repos if _has_unmerged_work(proj, r)]
    if unmerged:
        return ("unmerged", f"these repos still hold unmerged commits: {', '.join(unmerged)}")
    return None


def _has_unmerged_work(proj: Project, repo: TaskRepo) -> bool:
    """True when `repo`'s branch carries commits its base branch does not have.

    An unregistered project counts as unmerged: we cannot check, so we refuse.
    A branch with nothing unique on it is safe to delete even though
    `is_branch_merged` calls it "not merged" (it never diverged).
    """
    owner = _owning_project(proj, repo.project)
    if owner is None:
        return True
    if git.is_branch_merged(owner.root, repo.branch, repo.base_branch):
        return False
    return git.rev_list_count(owner.root, f"{repo.base_branch}..{repo.branch}") > 0


def _forget_task(cache: IndicatorCache, st: SyncState, proj: Project, task: Task) -> None:
    """Drop derived state for a task that no longer exists.

    Without this the indicator cache and the edge-trigger map grow without bound
    as tasks churn, and a later task reusing the same id inherits stale
    edge-trigger memory (a merged PR that would never re-notify).
    """
    cache.entries.pop(store.cache_key(proj.name, task.id), None)
    prefix = f"{proj.name}/{task.id}:"
    for key in [k for k in st.last_seen if k.startswith(prefix)]:
        del st.last_seen[key]
    for s in task.sessions:
        st.description_backoff.pop(s.session_id, None)
