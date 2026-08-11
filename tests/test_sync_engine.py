"""The sync pass: PR transitions, edge-triggered notifications, prune, backoff.

Uses real git repos (like tests/test_cli_task.py) but never touches the network:
`gh` and the notifier are patched at their module boundaries.
"""

from __future__ import annotations

import subprocess
from datetime import UTC, datetime, timedelta
from pathlib import Path
from unittest.mock import patch

import pytest

from goblin_watcher import state
from goblin_watcher.models import Project, SessionRecord, Task, TaskRepo
from goblin_watcher.sync import engine, journal, store
from goblin_watcher.sync.models import SyncState


def _git(repo: Path, *args: str) -> None:
    subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True)


def _init_repo(root: Path) -> None:
    root.mkdir(parents=True, exist_ok=True)
    _git(root, "init", "-q", "-b", "main")
    _git(root, "config", "user.email", "t@example.com")
    _git(root, "config", "user.name", "T")
    (root / "README.md").write_text("hi\n")
    _git(root, "add", "-A")
    _git(root, "commit", "-qm", "init")


class _RecordingNotifier:
    name = "recording"

    def __init__(self) -> None:
        self.sent: list[tuple[str, str]] = []

    def send(self, title: str, body: str) -> bool:
        self.sent.append((title, body))
        return True


@pytest.fixture
def demo(isolated_xdg: Path, tmp_path: Path) -> tuple[Project, Task]:
    """A registered project with one task on a real branch + worktree."""
    root = tmp_path / "repo"
    _init_repo(root)
    project = Project(
        name="demo",
        root=root,
        repo_url=None,
        default_branch="main",
        created_at=datetime.now(UTC),
    )
    state.register_project(project)

    worktree = root / ".worktrees" / "demo-1"
    _git(root, "worktree", "add", "-q", "-b", "demo-1", str(worktree))
    task = Task(
        id="demo-1",
        project="demo",
        branch="demo-1",
        worktree_path=worktree,
        base_branch="main",
        created_at=datetime.now(UTC),
    )
    state.save_task(project, task)
    return project, task


@pytest.fixture
def notifier() -> _RecordingNotifier:
    return _RecordingNotifier()


def _run(notifier: _RecordingNotifier, **gh_returns: object):  # type: ignore[no-untyped-def]
    """Run one pass with `gh` stubbed and notifications captured."""
    return patch.multiple(
        "goblin_watcher.sync.engine.gh",
        pr_state=lambda url: gh_returns.get("pr_state"),
        pr_checks=lambda url: gh_returns.get("pr_checks"),
    ), patch("goblin_watcher.sync.engine.resolve", return_value=notifier)


def test_pass_over_clean_task_reports_ok(demo, notifier) -> None:  # type: ignore[no-untyped-def]
    with _run(notifier)[1]:
        report = engine.run_pass()
    assert report.status == "ok"
    assert report.projects == 1
    assert report.tasks == 1


def test_indicators_are_cached_for_a_dirty_worktree(demo, notifier) -> None:  # type: ignore[no-untyped-def]
    _project, task = demo
    (task.worktree_path / "scratch.txt").write_text("wip\n")

    with _run(notifier)[1]:
        engine.run_pass()

    entry = store.get_indicators("demo", "demo-1", max_age_seconds=3600)
    assert entry is not None
    assert entry.uncommitted is True


def test_merged_pr_updates_status_and_notifies_once(demo, notifier) -> None:  # type: ignore[no-untyped-def]
    """Edge-triggered: the same MERGED state on a second pass must not re-notify."""
    project, task = demo
    state.update_task(
        project, task.id, lambda t: t.model_copy(update={"pr_url": "https://gh/pr/1"})
    )

    gh_patch, notify_patch = _run(notifier, pr_state="MERGED")
    # Prune is disabled here so the task survives for the second pass; prune is
    # covered separately below.
    with (
        gh_patch,
        notify_patch,
        patch("goblin_watcher.sync.engine.merge_detection", return_value=None),
    ):
        first = engine.run_pass()
        assert state.load_task(project, task.id).status == "merged"
        assert [t for t, _b in notifier.sent if "PR merged" in t]
        assert len(first.notifications) == 1

        notifier.sent.clear()
        second = engine.run_pass()

    assert notifier.sent == [], "merged PR notified twice"
    assert second.notifications == []


def test_checks_failing_then_passing_fires_both_edges(demo, notifier) -> None:  # type: ignore[no-untyped-def]
    project, task = demo
    state.update_task(
        project, task.id, lambda t: t.model_copy(update={"pr_url": "https://gh/pr/1"})
    )

    gh_patch, notify_patch = _run(notifier, pr_state="OPEN", pr_checks="failing")
    with gh_patch, notify_patch:
        engine.run_pass()
    assert any("checks failing" in t for t, _b in notifier.sent)

    notifier.sent.clear()
    gh_patch, notify_patch = _run(notifier, pr_state="OPEN", pr_checks="passing")
    with gh_patch, notify_patch:
        engine.run_pass()
    assert any("checks passing" in t for t, _b in notifier.sent)


def test_disabled_event_is_silent_but_still_journaled(demo, notifier) -> None:  # type: ignore[no-untyped-def]
    from goblin_watcher import config

    project, task = demo
    state.update_task(
        project, task.id, lambda t: t.model_copy(update={"pr_url": "https://gh/pr/1"})
    )
    cfg = config.Config()
    cfg.sync.notify_events = ["agent-idle"]  # pr-merged deliberately excluded

    gh_patch, notify_patch = _run(notifier, pr_state="MERGED")
    with (
        gh_patch,
        notify_patch,
        patch("goblin_watcher.sync.engine.config.load", return_value=cfg),
        patch("goblin_watcher.sync.engine.merge_detection", return_value=None),
    ):
        report = engine.run_pass()

    assert notifier.sent == []
    assert report.notifications == []


def test_prune_removes_merged_and_clean_task(demo, notifier) -> None:  # type: ignore[no-untyped-def]
    project, _task = demo
    gh_patch, notify_patch = _run(notifier)
    with (
        gh_patch,
        notify_patch,
        patch("goblin_watcher.sync.engine.merge_detection", return_value="ancestry"),
    ):
        report = engine.run_pass()

    assert report.pruned == ["demo/demo-1"]
    assert state.list_tasks(project) == []


def test_prune_never_forces_on_a_dirty_worktree(demo, notifier) -> None:  # type: ignore[no-untyped-def]
    """A merged branch with uncommitted work is reported, never deleted."""
    project, task = demo
    (task.worktree_path / "wip.txt").write_text("precious\n")

    gh_patch, notify_patch = _run(notifier)
    with (
        gh_patch,
        notify_patch,
        patch("goblin_watcher.sync.engine.merge_detection", return_value="ancestry"),
    ):
        report = engine.run_pass()

    assert report.pruned == []
    assert [t.id for t in state.list_tasks(project)] == ["demo-1"]
    assert (task.worktree_path / "wip.txt").exists()
    assert any("not clean" in title for title, _b in notifier.sent)


def test_prune_disabled_by_config(demo, notifier) -> None:  # type: ignore[no-untyped-def]
    from goblin_watcher import config

    project, _task = demo
    cfg = config.Config()
    cfg.sync.prune = False

    gh_patch, notify_patch = _run(notifier)
    with (
        gh_patch,
        notify_patch,
        patch("goblin_watcher.sync.engine.config.load", return_value=cfg),
        patch("goblin_watcher.sync.engine.merge_detection", return_value="ancestry"),
    ):
        report = engine.run_pass()

    assert report.pruned == []
    assert [t.id for t in state.list_tasks(project)] == ["demo-1"]


def test_second_pass_is_skipped_while_one_is_running(demo, notifier) -> None:  # type: ignore[no-untyped-def]
    from goblin_watcher import locks, paths

    with locks.exclusive(paths.sync_lock_file()):
        report = engine.run_pass()

    assert report.status == "skipped"


def test_description_backoff_stops_retrying_a_failing_session(demo, notifier) -> None:  # type: ignore[no-untyped-def]
    project, task = demo
    now = datetime.now(UTC)
    session = SessionRecord(
        agent="claude",
        session_id="sess-1",
        created_at=now,
        last_used_at=now,
    )
    state.update_task(project, task.id, lambda t: t.model_copy(update={"sessions": [session]}))

    calls: list[str] = []

    def failing_apply(project_name: str, task_id: str, session_id: str) -> int:
        calls.append(session_id)
        return 1  # persistent failure, e.g. the `claude` binary is missing

    gh_patch, notify_patch = _run(notifier)
    with (
        gh_patch,
        notify_patch,
        patch("goblin_watcher.sync.engine.description.should_refresh", return_value=True),
        patch("goblin_watcher.sync.engine.description.apply", side_effect=failing_apply),
        patch("goblin_watcher.sync.engine.merge_detection", return_value=None),
    ):
        for _ in range(5):
            engine.run_pass()

    # Three attempts, then the backoff window suppresses the rest — the
    # negative-caching gap the lazy path leaves open.
    assert len(calls) == 3
    assert store.load_state().description_backoff["sess-1"].failures == 3


def test_description_backoff_expires_after_the_window(demo, notifier) -> None:  # type: ignore[no-untyped-def]
    project, task = demo
    now = datetime.now(UTC)
    state.update_task(
        project,
        task.id,
        lambda t: t.model_copy(
            update={
                "sessions": [
                    SessionRecord(agent="claude", session_id="s1", created_at=now, last_used_at=now)
                ]
            }
        ),
    )
    st = SyncState()
    st.description_backoff["s1"] = engine.DescriptionBackoff(
        failures=9, last_attempt=now - timedelta(hours=2)
    )
    store.save_state(st)

    calls: list[str] = []

    def succeeding_apply(project_name: str, task_id: str, session_id: str) -> int:
        # A *real* success writes the description; a stub that only returns 0
        # would model the graceful-give-up path instead (covered below).
        calls.append(session_id)
        proj = state.get_project(project_name)
        state.update_task(
            proj,
            task_id,
            lambda latest: latest.model_copy(
                update={
                    "sessions": [
                        s.model_copy(
                            update={
                                "description": "did a thing",
                                "description_updated_at": datetime.now(UTC),
                            }
                        )
                        for s in latest.sessions
                    ]
                }
            ),
        )
        return 0

    gh_patch, notify_patch = _run(notifier)
    with (
        gh_patch,
        notify_patch,
        patch("goblin_watcher.sync.engine.description.should_refresh", return_value=True),
        patch("goblin_watcher.sync.engine.description.apply", side_effect=succeeding_apply),
        patch("goblin_watcher.sync.engine.merge_detection", return_value=None),
    ):
        engine.run_pass()

    assert calls == ["s1"], "backoff should expire after the retry window"
    assert "s1" not in store.load_state().description_backoff


def test_description_backoff_counts_a_graceful_give_up_as_a_failure(demo, notifier) -> None:  # type: ignore[no-untyped-def]
    """`description.apply` exits 0 when the LLM call fails — the common case.

    Trusting the exit code would leave the backoff permanently disarmed against
    the exact failure ADR 0005 wants negative-cached: a doomed refresh retried
    every single pass. Progress is measured from `description_updated_at`.
    """
    project, task = demo
    now = datetime.now(UTC)
    state.update_task(
        project,
        task.id,
        lambda t: t.model_copy(
            update={
                "sessions": [
                    SessionRecord(agent="claude", session_id="s1", created_at=now, last_used_at=now)
                ]
            }
        ),
    )

    calls: list[str] = []

    def graceful_give_up(project_name: str, task_id: str, session_id: str) -> int:
        calls.append(session_id)
        return 0  # e.g. `_invoke_llm` returned None: nothing written, exit 0

    gh_patch, notify_patch = _run(notifier)
    with (
        gh_patch,
        notify_patch,
        patch("goblin_watcher.sync.engine.description.should_refresh", return_value=True),
        patch("goblin_watcher.sync.engine.description.apply", side_effect=graceful_give_up),
        patch("goblin_watcher.sync.engine.merge_detection", return_value=None),
    ):
        for _ in range(5):
            engine.run_pass()

    assert len(calls) == 3, "an exit-0 failure must still arm the backoff"
    assert store.load_state().description_backoff["s1"].failures == 3


def test_pass_continues_after_a_step_error(demo, notifier) -> None:  # type: ignore[no-untyped-def]
    """One failing step must not abort the pass (ADR 0005)."""
    from goblin_watcher.errors import GoblinError

    gh_patch, notify_patch = _run(notifier)
    with (
        gh_patch,
        notify_patch,
        patch(
            "goblin_watcher.sync.engine.sessions.plan_reconciliation",
            side_effect=GoblinError("agent store unreadable"),
        ),
        patch("goblin_watcher.sync.engine.merge_detection", return_value=None),
    ):
        report = engine.run_pass()

    assert report.status == "partial"
    assert any("agent store unreadable" in e for e in report.errors)
    # The later indicator step still ran.
    assert store.get_indicators("demo", "demo-1", max_age_seconds=3600) is not None


def test_scratch_tasks_are_not_pruned_without_a_threshold(
    isolated_xdg: Path, tmp_path: Path, notifier
) -> None:  # type: ignore[no-untyped-def]
    scratch_root = tmp_path / "scratch"
    scratch_root.mkdir()
    project = Project(
        name="scratch",
        kind="scratch",
        root=scratch_root,
        repo_url=None,
        default_branch="main",
        created_at=datetime.now(UTC),
    )
    state.register_project(project)
    space = scratch_root / "pad"
    space.mkdir()
    task = Task(
        id="pad",
        kind="scratch",
        project="scratch",
        branch="pad",
        worktree_path=space,
        base_branch="main",
        created_at=datetime.now(UTC) - timedelta(days=90),
    )
    state.save_task(project, task)

    gh_patch, notify_patch = _run(notifier)
    with gh_patch, notify_patch:
        report = engine.run_pass()

    assert report.pruned == []
    assert space.exists()


def test_scratch_pruned_when_idle_past_threshold(
    isolated_xdg: Path, tmp_path: Path, notifier
) -> None:  # type: ignore[no-untyped-def]
    from goblin_watcher import config

    scratch_root = tmp_path / "scratch"
    scratch_root.mkdir()
    project = Project(
        name="scratch",
        kind="scratch",
        root=scratch_root,
        repo_url=None,
        default_branch="main",
        created_at=datetime.now(UTC),
    )
    state.register_project(project)
    space = scratch_root / "pad"
    space.mkdir()
    state.save_task(
        project,
        Task(
            id="pad",
            kind="scratch",
            project="scratch",
            branch="pad",
            worktree_path=space,
            base_branch="main",
            created_at=datetime.now(UTC) - timedelta(days=90),
        ),
    )

    cfg = config.Config()
    cfg.sync.scratch_prune_days = 30

    gh_patch, notify_patch = _run(notifier)
    with gh_patch, notify_patch, patch("goblin_watcher.sync.engine.config.load", return_value=cfg):
        report = engine.run_pass()

    assert report.pruned == ["scratch/pad"]
    assert not space.exists()


# ---------------------------------------------------------------------------
# Multi-repo tasks (ADR 0003): every per-repo step must use that repo's own root.


@pytest.fixture
def multi(isolated_xdg: Path, tmp_path: Path) -> tuple[Project, Project, Task]:
    """A two-repo task: `alpha` (primary, merged) plus `beta` (secondary)."""
    alpha_root = tmp_path / "alpha"
    beta_root = tmp_path / "beta"
    _init_repo(alpha_root)
    _init_repo(beta_root)
    now = datetime.now(UTC)
    alpha = Project(
        name="alpha", root=alpha_root, repo_url=None, default_branch="main", created_at=now
    )
    beta = Project(
        name="beta", root=beta_root, repo_url=None, default_branch="main", created_at=now
    )
    state.register_project(alpha)
    state.register_project(beta)

    ws = tmp_path / "ws"
    ws.mkdir()
    _git(alpha_root, "worktree", "add", "-q", "-b", "shared", str(ws / "alpha"))
    _git(beta_root, "worktree", "add", "-q", "-b", "shared", str(ws / "beta"))

    task = Task(
        id="shared",
        project="alpha",
        branch="shared",
        worktree_path=ws / "alpha",
        base_branch="main",
        workspace_path=ws,
        secondary_repos=[
            TaskRepo(project="beta", branch="shared", worktree_path=ws / "beta", base_branch="main")
        ],
        created_at=now,
    )
    state.save_task(alpha, task)
    return alpha, beta, task


def _commit(worktree: Path, name: str) -> None:
    (worktree / name).write_text("work\n")
    _git(worktree, "add", "-A")
    _git(worktree, "commit", "-qm", name)


def test_indicators_count_secondary_repo_commits(multi, notifier) -> None:  # type: ignore[no-untyped-def]
    """`ahead` must be measured in each repo, not against the primary's root.

    `beta`'s branch does not exist in `alpha`'s object store, so resolving the
    range there silently yields 0 and the secondary's work vanishes from the
    rollup.
    """
    _alpha, _beta, task = multi
    _commit(task.worktree_path, "a.txt")
    _commit(task.secondary_repos[0].worktree_path, "b.txt")
    _commit(task.secondary_repos[0].worktree_path, "c.txt")

    gh_patch, notify_patch = _run(notifier)
    with (
        gh_patch,
        notify_patch,
        patch("goblin_watcher.sync.engine.merge_detection", return_value=None),
    ):
        engine.run_pass()

    entry = store.get_indicators("alpha", "shared", max_age_seconds=3600)
    assert entry is not None
    assert entry.ahead == 3, "secondary repo's commits were dropped from the rollup"


def test_prune_refuses_a_task_whose_secondary_repo_is_unmerged(multi, notifier) -> None:  # type: ignore[no-untyped-def]
    """`destroy_task` force-deletes every repo's branch; unmerged work is not ours to delete."""
    alpha, _beta, task = multi
    _commit(task.secondary_repos[0].worktree_path, "precious.txt")

    gh_patch, notify_patch = _run(notifier)
    with (
        gh_patch,
        notify_patch,
        patch("goblin_watcher.sync.engine.merge_detection", return_value="PR"),
    ):
        report = engine.run_pass()

    assert report.pruned == []
    assert [t.id for t in state.list_tasks(alpha)] == ["shared"]
    assert any("not clean" in title for title, _b in notifier.sent)
    assert any("beta" in body for _t, body in notifier.sent)


def test_prune_allows_a_multi_repo_task_with_nothing_unique_on_the_secondary(
    multi, notifier
) -> None:  # type: ignore[no-untyped-def]
    """A secondary branch sitting on its base has nothing to lose — prune proceeds."""
    alpha, _beta, _task = multi

    gh_patch, notify_patch = _run(notifier)
    with (
        gh_patch,
        notify_patch,
        patch("goblin_watcher.sync.engine.merge_detection", return_value="PR"),
    ):
        report = engine.run_pass()

    assert report.pruned == ["alpha/shared"]
    assert state.list_tasks(alpha) == []


# ---------------------------------------------------------------------------
# Pass-level bookkeeping and observability.


def test_pruning_forgets_the_task_s_derived_state(demo, notifier) -> None:  # type: ignore[no-untyped-def]
    """Cache rows and edge-trigger keys must not outlive the task they describe."""
    project, task = demo
    state.update_task(
        project, task.id, lambda t: t.model_copy(update={"pr_url": "https://gh/pr/1"})
    )

    gh_patch, notify_patch = _run(notifier, pr_state="MERGED")
    with (
        gh_patch,
        notify_patch,
        patch("goblin_watcher.sync.engine.merge_detection", return_value="PR"),
    ):
        report = engine.run_pass()

    assert report.pruned == ["demo/demo-1"]
    assert store.get_indicators("demo", "demo-1", max_age_seconds=3600) is None
    assert store.load_state().last_seen == {}


def test_a_crashing_pass_is_recorded_not_silently_dropped(demo, notifier) -> None:  # type: ignore[no-untyped-def]
    """An unexpected exception must fail loudly *and* leave a trace.

    Reporting the previous pass as the last one would make a permanently broken
    scheduled job look healthy in `gw sync status`.
    """
    gh_patch, notify_patch = _run(notifier)
    with (
        gh_patch,
        notify_patch,
        patch(
            "goblin_watcher.sync.engine.state.list_tasks",
            side_effect=RuntimeError("bad task json"),
        ),
        pytest.raises(RuntimeError),
    ):
        engine.run_pass()

    last = store.load_state().last_pass
    assert last is not None
    assert last.status == "error"
    assert any("bad task json" in e for e in last.errors)
    assert "pass-crashed" in [e["event"] for e in journal.read_entries()]


def test_a_skipped_pass_says_so_in_the_journal(demo, notifier) -> None:  # type: ignore[no-untyped-def]
    """A wedged pass holding the lock must show up as repeated skips, not silence."""
    from goblin_watcher import locks, paths

    with locks.exclusive(paths.sync_lock_file()):
        report = engine.run_pass()

    assert report.status == "skipped"
    assert "pass-skipped" in [e["event"] for e in journal.read_entries()]


def test_a_lock_timeout_inside_the_pass_is_not_reported_as_skipped(demo, notifier) -> None:  # type: ignore[no-untyped-def]
    """Only *acquisition* means "another pass is running"; anything else is an error."""
    from goblin_watcher import locks

    gh_patch, notify_patch = _run(notifier)
    with (
        gh_patch,
        notify_patch,
        patch(
            "goblin_watcher.sync.engine.sessions.plan_reconciliation",
            side_effect=locks.LockTimeoutError("task record is wedged"),
        ),
        patch("goblin_watcher.sync.engine.merge_detection", return_value=None),
    ):
        report = engine.run_pass()

    assert report.status == "partial"
    assert any("wedged" in e for e in report.errors)


def test_fetch_failure_is_surfaced_in_the_pass_report(demo, notifier) -> None:  # type: ignore[no-untyped-def]
    from goblin_watcher.errors import GoblinError

    project, _task = demo
    state.register_project(project.model_copy(update={"repo_url": "git@example.com:x/y.git"}))

    gh_patch, notify_patch = _run(notifier)
    with (
        gh_patch,
        notify_patch,
        patch("goblin_watcher.sync.engine.git.fetch", side_effect=GoblinError("no such remote")),
        patch("goblin_watcher.sync.engine.merge_detection", return_value=None),
    ):
        report = engine.run_pass()

    assert report.status == "partial", "a failed pre-warm must not report as a clean pass"
    assert any("no such remote" in e for e in report.errors)


# ---------------------------------------------------------------------------
# Self-healing sweep + per-task crash isolation


def test_sweep_drops_state_for_tasks_deleted_outside_sync(demo, notifier) -> None:  # type: ignore[no-untyped-def]
    """`gw task rm` doesn't touch sync state; the next full pass must clean up.

    Left behind, a task id reused later inherits stale indicators and stale
    edge-trigger memory — which silently suppresses its first notification.
    """
    project, task = demo
    state.update_task(
        project, task.id, lambda t: t.model_copy(update={"pr_url": "https://gh/pr/1"})
    )
    gh_patch, notify_patch = _run(notifier, pr_state="MERGED")
    with (
        gh_patch,
        notify_patch,
        patch("goblin_watcher.sync.engine.merge_detection", return_value=None),
    ):
        engine.run_pass()

    assert store.get_indicators("demo", "demo-1", max_age_seconds=3600) is not None
    assert any(k.startswith("demo/demo-1:") for k in store.load_state().last_seen)

    # Delete the record the way `gw task rm` does — sync knows nothing about it.
    state.delete_task_record(project, task.id)

    gh_patch, notify_patch = _run(notifier)
    with gh_patch, notify_patch:
        engine.run_pass()

    assert store.get_indicators("demo", "demo-1", max_age_seconds=3600) is None
    assert not any(k.startswith("demo/demo-1:") for k in store.load_state().last_seen)


def test_recreated_task_id_still_notifies_after_a_sweep(demo, notifier) -> None:  # type: ignore[no-untyped-def]
    """The consequence the sweep exists to prevent: a missed pr-merged notify."""
    project, task = demo
    state.update_task(
        project, task.id, lambda t: t.model_copy(update={"pr_url": "https://gh/pr/1"})
    )
    gh_patch, notify_patch = _run(notifier, pr_state="MERGED")
    with (
        gh_patch,
        notify_patch,
        patch("goblin_watcher.sync.engine.merge_detection", return_value=None),
    ):
        engine.run_pass()
    assert any("PR merged" in t for t, _b in notifier.sent)

    state.delete_task_record(project, task.id)
    gh_patch, notify_patch = _run(notifier)
    with gh_patch, notify_patch:
        engine.run_pass()  # sweeps the dead edge-trigger memory

    # Same id comes back (reopened ticket, re-created task) with a merged PR.
    state.save_task(
        project,
        task.model_copy(update={"pr_url": "https://gh/pr/1", "status": "open"}),
    )
    notifier.sent.clear()
    gh_patch, notify_patch = _run(notifier, pr_state="MERGED")
    with (
        gh_patch,
        notify_patch,
        patch("goblin_watcher.sync.engine.merge_detection", return_value=None),
    ):
        engine.run_pass()

    assert any("PR merged" in t for t, _b in notifier.sent), (
        "recreated task inherited stale edge-trigger memory and never notified"
    )


def test_scoped_pass_does_not_sweep(demo, notifier) -> None:  # type: ignore[no-untyped-def]
    """A --project pass only saw one project; it must not delete others' state."""
    project, task = demo
    gh_patch, notify_patch = _run(notifier)
    with (
        gh_patch,
        notify_patch,
        patch("goblin_watcher.sync.engine.merge_detection", return_value=None),
    ):
        engine.run_pass()
    assert store.get_indicators("demo", "demo-1", max_age_seconds=3600) is not None

    state.delete_task_record(project, task.id)
    gh_patch, notify_patch = _run(notifier)
    with gh_patch, notify_patch:
        engine.run_pass(project_name="demo")

    assert store.get_indicators("demo", "demo-1", max_age_seconds=3600) is not None


def test_one_crashing_task_does_not_wedge_the_pass(
    isolated_xdg: Path, tmp_path: Path, notifier
) -> None:  # type: ignore[no-untyped-def]
    """An unexpected error on one task must not stop the others being refreshed."""
    root = tmp_path / "repo"
    _init_repo(root)
    project = Project(
        name="demo",
        root=root,
        repo_url=None,
        default_branch="main",
        created_at=datetime.now(UTC),
    )
    state.register_project(project)
    for name in ("demo-1", "demo-2", "demo-3"):
        worktree = root / ".worktrees" / name
        _git(root, "worktree", "add", "-q", "-b", name, str(worktree))
        state.save_task(
            project,
            Task(
                id=name,
                project="demo",
                branch=name,
                worktree_path=worktree,
                base_branch="main",
                created_at=datetime.now(UTC),
            ),
        )

    real_plan = engine.sessions.plan_reconciliation

    def explode_on_two(task):  # type: ignore[no-untyped-def]
        if task.id == "demo-2":
            raise RuntimeError("corrupt record")
        return real_plan(task)

    gh_patch, notify_patch = _run(notifier)
    with (
        gh_patch,
        notify_patch,
        patch(
            "goblin_watcher.sync.engine.sessions.plan_reconciliation",
            side_effect=explode_on_two,
        ),
        patch("goblin_watcher.sync.engine.merge_detection", return_value=None),
    ):
        report = engine.run_pass()

    # Loud, but not wedged: the bad task is reported and the others still ran.
    assert report.status == "error"
    assert any("demo-2" in e and "RuntimeError" in e for e in report.errors)
    assert report.tasks == 3
    for good in ("demo-1", "demo-3"):
        assert store.get_indicators("demo", good, max_age_seconds=3600) is not None
    events = [e["event"] for e in journal.read_entries()]
    assert "task-crashed" in events
    assert "pass-end" in events, "pass must still finish and record itself"


def test_sync_refreshes_a_github_issue_state(demo, notifier) -> None:  # type: ignore[no-untyped-def]
    """The issue-state step runs for issue-backed tasks and reports as a step."""
    from goblin_watcher.models import GhIssue

    project, task = demo
    state.save_task(
        project,
        task.model_copy(
            update={
                "github_issue": GhIssue(
                    number=42,
                    repo="org/repo",
                    title="Add rate limit",
                    state="OPEN",
                    url="https://github.com/org/repo/issues/42",
                )
            }
        ),
    )

    with (
        _run(notifier)[1],
        patch("goblin_watcher.github_state.gh.issue_state", return_value="CLOSED"),
    ):
        report = engine.run_pass()

    assert report.status == "ok"
    assert any(s.step == "github-issue" and s.ok == 1 for s in report.steps)
    refreshed = state.load_task(project, "demo-1")
    assert refreshed.github_issue is not None
    assert refreshed.github_issue.state == "CLOSED"


def test_sync_skips_the_issue_step_without_an_issue(demo, notifier) -> None:  # type: ignore[no-untyped-def]
    with (
        _run(notifier)[1],
        patch("goblin_watcher.github_state.gh.issue_state") as lookup,
    ):
        report = engine.run_pass()

    lookup.assert_not_called()
    assert not any(s.step == "github-issue" for s in report.steps)
