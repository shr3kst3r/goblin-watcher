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

from goblin_watcher import activity, config, description, gh, git, github_state, sessions, state
from goblin_watcher.commands.task import (
    busy_reasons,
    destroy_task,
    dirty_worktrees,
    merge_detection,
    scratch_last_activity,
)
from goblin_watcher.errors import GoblinError, ProjectNotFoundError, TaskNotFoundError
from goblin_watcher.linear_state import LinearStateFetcher
from goblin_watcher.models import Project, SessionRecord, Task, TaskRepo
from goblin_watcher.sync import actions, journal, store
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
    # Filled by `_fire` as edges trip; drained once the walk is over (ADR 0012).
    # Deferred rather than immediate so an action that deletes a task cannot
    # pull the record out from under the loop still iterating it.
    queued = actions.ActionQueue()
    # Every PR this pass looked up, so the prune action can answer "is it
    # merged?" without a round-trip the batch already paid for.
    snapshots_seen: dict[str, gh.PrSnapshot] = {}
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
            snapshots_seen.update(snapshots)

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
                        queued=queued,
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

        # Actions run after the whole walk, never inside it. A `prune` action
        # deletes the record the loop above is iterating, and re-reading each
        # task here means an action always sees what the pass just wrote rather
        # than the snapshot that tripped its edge.
        if len(queued):
            _run_actions(
                queued,
                cfg=cfg,
                st=st,
                cache=cache,
                snapshots=snapshots_seen,
                steps=steps,
                emit=emit,
                report=report,
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
        latest.action_runs = st.action_runs
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

    # Same key prefix, same reasoning: a recreated task must not inherit a dead
    # one's cooldown and sit out its first action.
    dead_runs = [k for k in st.action_runs if k.split(":", 1)[0] not in live_tasks]
    for key in dead_runs:
        del st.action_runs[key]

    dead_backoff = [k for k in st.description_backoff if k not in live_sessions]
    for key in dead_backoff:
        del st.description_backoff[key]

    total = len(dead_entries) + len(dead_signals) + len(dead_runs) + len(dead_backoff)
    if total:
        emit(
            "info",
            "swept-dead-state",
            detail=f"dropped {len(dead_entries)} cached indicator(s), "
            f"{len(dead_signals)} edge-trigger key(s), {len(dead_runs)} action cooldown(s), "
            f"{len(dead_backoff)} backoff entry(ies)",
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
    queued: actions.ActionQueue,
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
            queued=queued,
        )

    guard("indicators", _indicators)

    # 7. Prune, and 8. idle notification.
    guard(
        "notify", lambda: _notify_activity(proj, current, st, notifier, cfg, emit, report, queued)
    )
    if cfg.sync.prune:
        guard(
            "prune",
            lambda: _maybe_prune(
                proj, current, snapshot, cfg, st, cache, notifier, emit, report, queued
            ),
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
    queued: actions.ActionQueue,
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
        # An archived task has no checkout to read git facts from (gh-23); PR
        # state below still refreshes, since the branch and PR outlive the
        # worktree.
        if task.archived or not repo.worktree_path.exists():
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
            _fire_pr_transition(proj, task, pr_state, st, notifier, cfg, emit, report, queued)
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
            _fire_checks_transition(
                proj, task, snapshot.checks, st, notifier, cfg, emit, report, queued
            )

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


# `agent-idle` was the one "the agent stopped" event before the transcript
# classifier split it into finished-vs-blocked (ADR 0010). A config that asked
# for it is asking to hear about both.
_LEGACY_IDLE_ALIASES = frozenset({"agent-needs-you", "agent-done"})


def event_enabled(cfg: config.Config, event: str) -> bool:
    """Whether `event` is switched on in `sync.notify_events`."""
    events = cfg.sync.notify_events
    if event in events:
        return True
    return event in _LEGACY_IDLE_ALIASES and "agent-idle" in events


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
    queued: actions.ActionQueue,
) -> None:
    """Announce one edge, and queue whatever `[sync.on]` says to do about it.

    The two halves are independent switches. `notify_events` decides whether a
    human hears about it; `sync.on` decides whether the pass acts. Wanting a
    failing branch fixed without also getting a desktop banner about it is a
    perfectly ordinary setup, so the action is queued even when the
    notification is off.

    Every event in the system routes through here, which is what makes this the
    one place actions need to hook: the callers have already applied `_edge`, so
    an action inherits once-per-transition without any machinery of its own.
    """
    if event_enabled(cfg, event):
        report.notifications.append(f"{event}: {task.id}")
        emit("notify", event, project=proj.name, task=task.id, detail=body)
        notifier.send(title, body)
    queued.enqueue(cfg=cfg, proj=proj, task=task, event=event, body=body)


def _fire_pr_transition(
    proj: Project,
    task: Task,
    pr_state: str,
    st: SyncState,
    notifier: Notifier,
    cfg: config.Config,
    emit: Callable[..., None],
    report: PassReport,
    queued: actions.ActionQueue,
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
        queued=queued,
    )
    _fire_parent_merged(proj, task, notifier, cfg, emit, report, queued)


def _fire_parent_merged(
    proj: Project,
    parent: Task,
    notifier: Notifier,
    cfg: config.Config,
    emit: Callable[..., None],
    report: PassReport,
    queued: actions.ActionQueue,
) -> None:
    """Tell every task stacked on `parent` that the branch under it has landed.

    Rides the parent's own `pr-merged` edge, which gives once-per-transition for
    free and — because step 5 runs before step 7 — reaches the children while the
    parent record they point at still exists. The notification names the *child*:
    it is the branch that now needs rebasing (gh-20).

    Checking up front rather than leaving it to `_fire` keeps a merge from
    costing a full task-directory scan when nobody asked for the event — and it
    has to ask about *both* switches, since `sync.on` can want the restack acted
    on with the notification itself turned off.
    """
    if not (event_enabled(cfg, "parent-merged") or actions.actions_for(cfg, "parent-merged")):
        return
    for child in state.list_tasks(proj):
        if child.kind == "scratch" or child.parent_task != parent.id:
            continue
        _fire(
            event="parent-merged",
            title=f"{child.id}: base branch merged",
            body=(
                f"{parent.branch} (task {parent.id}) landed. Rebase {child.branch} "
                f"onto {parent.base_branch} and retarget its PR."
            ),
            cfg=cfg,
            notifier=notifier,
            proj=proj,
            task=child,
            emit=emit,
            report=report,
            queued=queued,
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
    queued: actions.ActionQueue,
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
            queued=queued,
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
            queued=queued,
        )


# Longest notification body we'll build out of a blocking question. Enough for
# the question itself; a macOS notification truncates well before this anyway.
_QUESTION_BODY_CHARS = 240


def _notify_activity(
    proj: Project,
    task: Task,
    st: SyncState,
    notifier: Notifier,
    cfg: config.Config,
    emit: Callable[..., None],
    report: PassReport,
    queued: actions.ActionQueue,
) -> None:
    """Notify when an agent stops, saying *why* it stopped (ADR 0010).

    The state comes from the shape of the transcript's tail, the same call
    `gw status` renders, so the dashboard and the notification never disagree:

    * `agent-needs-you` — the last turn ended on a question. The one worth
      interrupting for, and the body carries the question itself.
    * `agent-done` — the turn finished with nothing pending.
    * `agent-idle` — quiet, and the transcript can't say which of the two it
      is. Agents gw can't parse (gemini, managed) and sessions
      abandoned mid-tool-call land here; this is the old event's behaviour,
      kept for the cases that still can't do better.

    Edge-triggered on `Activity.edge_token`, which folds the classifying
    evidence in alongside the state name — so a *second* question fires a
    second notification instead of being swallowed as "still needs-you". A
    first sighting is recorded without notifying: an agent that has been
    blocked since yesterday is not news, and announcing every session on the
    first pass after an upgrade is precisely the fatigue this is meant to
    avoid.
    """
    for s in task.sessions:
        act = activity.classify(
            s,
            now=_now(),
            active_seconds=int(cfg.defaults.activity_active_seconds),
            stalled_after=int(cfg.defaults.activity_grace_seconds),
        )
        if act.state == "unknown":
            continue
        key = f"{proj.name}/{task.id}:{_SIG_ACTIVITY}:{s.session_id}"
        previous = st.last_seen.get(key)
        st.last_seen[key] = act.edge_token
        if previous is None or previous == act.edge_token or not act.is_terminal:
            continue
        event, title, body = _activity_notification(task, s, act)
        _fire(
            event=event,
            title=title,
            body=body,
            cfg=cfg,
            notifier=notifier,
            proj=proj,
            task=task,
            emit=emit,
            report=report,
            queued=queued,
        )


def _activity_notification(
    task: Task, session: SessionRecord, act: activity.Activity
) -> tuple[str, str, str]:
    """(event, title, body) for one terminal activity state."""
    if act.state == "needs-you":
        return (
            "agent-needs-you",
            f"{task.id}: {session.agent} needs you",
            _question_body(act.detail) or "Agent ended its turn with a question.",
        )
    context = session.description or session.summary or ""
    if act.state == "done":
        return (
            "agent-done",
            f"{task.id}: {session.agent} finished",
            context or "Turn complete, nothing pending.",
        )
    return (
        "agent-idle",
        f"{task.id}: {session.agent} is idle",
        context or "Agent went quiet — may be waiting for input.",
    )


def _question_body(detail: str | None) -> str | None:
    """The tail of a blocking turn, collapsed to one line for a notification."""
    if not detail:
        return None
    text = " ".join(detail.split())
    if not text:
        return None
    if len(text) <= _QUESTION_BODY_CHARS:
        return text
    return "…" + text[-(_QUESTION_BODY_CHARS - 1) :]


def _run_actions(
    queued: actions.ActionQueue,
    *,
    cfg: config.Config,
    st: SyncState,
    cache: IndicatorCache,
    snapshots: dict[str, gh.PrSnapshot],
    steps: _Steps,
    emit: Callable[..., None],
    report: PassReport,
) -> None:
    """Drain the action queue: decide *whether* each queued action runs (ADR 0012).

    `sync.actions` says what an action means; this says how often it is allowed
    to happen and what a failure costs. Three bounds, outermost first:

    * `max_actions_per_pass` — a global cap. Twenty branches going red at once
      is one CI outage, and spawning twenty agents for it is not a response.
      Overflow is journaled, so a capped pass never reads as a complete one.
    * `action_rate_limit_seconds` — a per task+event+action cooldown, behind the
      edge trigger rather than instead of it. The edge already stops a *steady*
      signal from re-firing; this is what catches one that genuinely flaps.
    * Isolation — a `GoblinError` fails one action and the pass continues, the
      same contract every other step has. Anything else is a bug and flips the
      pass to `error` so it is loud in the launchd log.

    A *declined* action (the task is gone, its agent is still working, its
    branch isn't merged) costs nothing: no cooldown starts and no budget is
    spent, so the next pass reconsiders it freely. Only work that actually
    happened is rate-limited.
    """
    now = _now()
    cap = cfg.sync.max_actions_per_pass
    window = cfg.sync.action_rate_limit_seconds
    ran = 0
    pending = queued.pending

    for index, item in enumerate(pending):
        if cap > 0 and ran >= cap:
            emit(
                "info",
                "actions-capped",
                detail=f"ran {ran} action(s) this pass (max_actions_per_pass={cap}); "
                f"{len(pending) - index} deferred to the next pass",
            )
            break

        if window > 0 and _in_cooldown(st, item.key, now, window):
            emit(
                "info",
                "action-rate-limited",
                project=item.project,
                task=item.task_id,
                detail=f"{item.action} on {item.event}: ran less than {window}s ago",
            )
            continue

        # Re-read rather than trusting the record that tripped the edge: steps 5
        # and 7 have run since, and the task may not exist at all any more.
        try:
            proj = state.get_project(item.project)
            task = state.load_task(proj, item.task_id)
        except GoblinError:
            emit(
                "info",
                "action-skipped",
                project=item.project,
                task=item.task_id,
                detail=f"{item.action}: the task no longer exists",
            )
            continue

        ctx = actions.ActionContext(
            proj=proj,
            task=task,
            pending=item,
            cfg=cfg,
            snapshot=snapshots.get(task.pr_url or ""),
            now=now,
        )
        try:
            result = actions.dispatch(item.action, ctx)
        except GoblinError as e:
            steps.failed("actions")
            report.errors.append(f"{item.project}/{item.task_id}: {item.action}: {e.message}")
            emit(
                "error",
                "action-failed",
                project=item.project,
                task=item.task_id,
                detail=f"{item.action}: {e.message}",
            )
            continue
        except Exception as e:
            report.status = "error"
            report.errors.append(
                f"{item.project}/{item.task_id}: {item.action}: {type(e).__name__}: {e}"
            )
            emit(
                "error",
                "action-crashed",
                project=item.project,
                task=item.task_id,
                detail=f"{item.action}: {type(e).__name__}: {e}",
            )
            continue

        if not result.ran:
            emit(
                "info",
                "action-skipped",
                project=item.project,
                task=item.task_id,
                detail=f"{item.action}: {result.detail}",
            )
            continue

        ran += 1
        steps.ok("actions")
        st.action_runs[item.key] = now
        report.actions.append(f"{item.action}: {item.task_id} ({item.event})")
        emit(
            "action",
            "action-ran",
            project=item.project,
            task=item.task_id,
            detail=f"{item.action} on {item.event} — {result.detail}",
        )
        if result.removed_task:
            report.pruned.append(f"{proj.name}/{task.id}")
            # Including the cooldown key just written: the task it addressed is
            # gone, and a future task reusing the id must start clean.
            _forget_task(cache, st, proj, task)


def _in_cooldown(st: SyncState, key: str, now: datetime, window: int) -> bool:
    last = st.action_runs.get(key)
    if last is None:
        return False
    if last.tzinfo is None:
        last = last.replace(tzinfo=UTC)
    return (now - last).total_seconds() < window


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
    queued: actions.ActionQueue,
) -> None:
    """Prune a task only when it is unambiguously safe (ADR 0005).

    Merged, clean, **and** nobody home. Never forces: a live agent, a dirty
    worktree, or — on a multi-repo task — a secondary repo still holding unmerged
    commits is reported via a `prunable` notification and left alone, because
    deleting in-flight work is not a cleanup this command is allowed to do.
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
        blocker = prune_blocker(proj, task, cfg=cfg)
        if blocker is not None:
            kind, detail = blocker
            headline = "merged but still busy" if kind == "busy" else "merged but not clean"
            if _edge(st, proj, task, _SIG_PRUNABLE, f"{kind}:{detail}"):
                _fire(
                    event="prunable",
                    title=f"{task.id}: {headline}",
                    body=f"Branch is merged but {detail}; not pruned automatically.",
                    cfg=cfg,
                    notifier=notifier,
                    proj=proj,
                    task=task,
                    emit=emit,
                    report=report,
                    queued=queued,
                )
            emit("info", "prune-skipped", project=proj.name, task=task.id, detail=detail)
            return

    destroy_task(proj, task, force=False)
    _forget_task(cache, st, proj, task)
    report.pruned.append(f"{proj.name}/{task.id}")
    emit("action", "task-pruned", project=proj.name, task=task.id, detail=reason)


def prune_blocker(
    proj: Project, task: Task, *, cfg: config.Config | None = None
) -> tuple[str, str] | None:
    """Why this merged task must not be auto-pruned, or None when it is safe.

    Three refusals:

    * an agent still running in the worktree (`busy_reasons`). A headless agent
      that opens and merges its own PR keeps going for a while afterwards, and by
      then it has committed everything — so this is the moment the dirty check is
      least able to help, and a pass that woke on the merge deleted the directory
      out from under a live agent (#56). Checked here rather than at the call
      sites so the periodic prune and the `[sync.on]` one cannot diverge.
    * uncommitted changes anywhere on the task.
    * `merge_detection` only inspects the primary repo, but `destroy_task`
      force-deletes *every* repo's branch (`git branch -D`). An unattended prune
      therefore has to check the secondaries itself: a branch carrying commits
      its base does not have is unmerged work, and losing it is not recoverable
      from the worktree deletion alone.

    Public because `sync.actions.prune` calls it too: the `[sync.on]` prune must
    not be able to be weaker than the periodic one, and sharing this is what
    guarantees that rather than hoping two copies stay in step. `cfg` only
    supplies the activity thresholds; omitting it costs a config read per task.
    """
    busy = busy_reasons(task, cfg=cfg)
    if busy:
        return ("busy", busy[0])
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
    for key in [k for k in st.action_runs if k.startswith(prefix)]:
        del st.action_runs[key]
    for s in task.sessions:
        st.description_backoff.pop(s.session_id, None)
