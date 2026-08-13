"""`[sync.on]`: the actions a pass takes on the edges it detects (ADR 0012).

Same shape as tests/test_sync_engine.py — real git repos, `gh` and the notifier
patched at their module boundaries. No agent binary is ever spawned: the
launcher is patched, and the one test that needs a "busy" agent hand-writes a
claude transcript.
"""

from __future__ import annotations

import json
import subprocess
from datetime import UTC, datetime, timedelta
from pathlib import Path
from unittest.mock import patch

import pytest
from pydantic import ValidationError

from goblin_watcher import config, state
from goblin_watcher.agents.claude import ClaudeAgent
from goblin_watcher.errors import GoblinError
from goblin_watcher.models import Project, SessionRecord, Task
from goblin_watcher.sync import actions, journal, store
from goblin_watcher.windowing import HeadlessWindower


def _git(repo: Path, *args: str) -> None:
    subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True)


class _RecordingNotifier:
    name = "recording"

    def __init__(self) -> None:
        self.sent: list[tuple[str, str]] = []

    def send(self, title: str, body: str) -> bool:
        self.sent.append((title, body))
        return True


@pytest.fixture
def notifier() -> _RecordingNotifier:
    return _RecordingNotifier()


@pytest.fixture
def demo(isolated_xdg: Path, tmp_path: Path) -> Project:
    """A registered project on a real repo, with no tasks yet."""
    root = tmp_path / "repo"
    root.mkdir(parents=True, exist_ok=True)
    _git(root, "init", "-q", "-b", "main")
    _git(root, "config", "user.email", "t@example.com")
    _git(root, "config", "user.name", "T")
    (root / "README.md").write_text("hi\n")
    _git(root, "add", "-A")
    _git(root, "commit", "-qm", "init")
    project = Project(
        name="demo",
        root=root,
        repo_url=None,
        default_branch="main",
        created_at=datetime.now(UTC),
    )
    state.register_project(project)
    return project


def _task(project: Project, task_id: str, *, pr: str | None = "https://gh/pr/1") -> Task:
    worktree = project.root / ".worktrees" / task_id
    _git(project.root, "worktree", "add", "-q", "-b", task_id, str(worktree))
    task = Task(
        id=task_id,
        project=project.name,
        branch=task_id,
        worktree_path=worktree,
        base_branch="main",
        pr_url=pr,
        created_at=datetime.now(UTC),
    )
    state.save_task(project, task)
    return task


def _working_transcript(task: Task, session_id: str) -> Path:
    """A claude transcript ending on an unanswered tool call, in claude's store.

    It has to live where claude would actually put it: a sync pass re-derives
    `transcript_path` from the agent's own layout, so a fixture parked anywhere
    else is silently repointed and classifies as `unknown`.
    """
    directory = ClaudeAgent()._project_dir(task.agent_cwd)
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / f"{session_id}.jsonl"
    path.write_text(
        json.dumps(
            {
                "type": "assistant",
                "message": {
                    "role": "assistant",
                    "content": [{"type": "tool_use", "id": "t1", "name": "Bash", "input": {}}],
                },
            }
        )
        + "\n"
    )
    return path


def _cfg(on: dict[str, list[str]] | None = None, **sync: object) -> config.Config:
    """A `Config` with `[sync.on]` wired, validated exactly as TOML would be."""
    cfg = config.Config.model_validate({"sync": {"on": on or {}, **sync}})
    # Nothing here should reach a real agent binary or a real LLM.
    cfg.defaults.description_agent = "off"
    return cfg


def _pass(notifier: _RecordingNotifier, cfg: config.Config, **gh_returns: object):  # type: ignore[no-untyped-def]
    """Run one pass with `gh`, the notifier, and config all stubbed.

    Task PR URLs here aren't github.com URLs, so `_collect_pr_snapshots` takes
    its per-PR fallback and these two stubs are what it calls.
    """
    from goblin_watcher.sync import engine

    with (
        patch.multiple(
            "goblin_watcher.sync.engine.gh",
            pr_state=lambda url: gh_returns.get("pr_state"),
            pr_checks=lambda url: gh_returns.get("pr_checks"),
        ),
        patch("goblin_watcher.sync.engine.resolve", return_value=notifier),
        patch("goblin_watcher.sync.engine.config.load", return_value=cfg),
    ):
        return engine.run_pass()


def _events(name: str) -> list[dict]:
    return [e for e in journal.read_entries(limit=500) if e.get("event") == name]


# ---------------------------------------------------------------------------
# Opt-in, and off by default.


def test_no_actions_run_when_sync_on_is_empty(demo, notifier) -> None:  # type: ignore[no-untyped-def]
    """The default config must leave sync a reporter."""
    _task(demo, "demo-1")

    report = _pass(notifier, _cfg(), pr_state="OPEN", pr_checks="failing")

    assert report.actions == []
    assert any("checks failing" in t for t, _b in notifier.sent), "the edge still notifies"


def test_unknown_event_or_action_is_a_config_error() -> None:
    """`sync.on` is a closed vocabulary on both sides of the arrow.

    A typo has to fail loudly: a silently dead rule looks identical to a rule
    that is working, and you would only find out when the thing it was meant to
    catch happened.
    """
    with pytest.raises(ValidationError):
        config.Config.model_validate({"sync": {"on": {"checks-flaily": ["prune"]}}})
    with pytest.raises(ValidationError):
        config.Config.model_validate({"sync": {"on": {"checks-failed": ["rm -rf /"]}}})


def test_actions_for_does_not_alias_agent_idle(demo) -> None:  # type: ignore[no-untyped-def]
    """`notify_events` reads a lone `agent-idle` as all three states; `sync.on`
    is new, has no legacy config to honour, and must not widen a rule."""
    cfg = _cfg({"agent-idle": ["archive"]})
    assert actions.actions_for(cfg, "agent-idle") == ["archive"]
    assert actions.actions_for(cfg, "agent-done") == []
    assert actions.actions_for(cfg, "agent-needs-you") == []


# ---------------------------------------------------------------------------
# spawn-fix-session.


def test_checks_failed_spawns_a_headless_session(demo, notifier) -> None:  # type: ignore[no-untyped-def]
    task = _task(demo, "demo-1")
    cfg = _cfg({"checks-failed": ["spawn-fix-session"]})

    with patch("goblin_watcher.sync.actions.launch_agent", return_value=(0, task)) as launch:
        report = _pass(notifier, cfg, pr_state="OPEN", pr_checks="failing")

    assert report.actions == ["spawn-fix-session: demo-1 (checks-failed)"]
    kwargs = launch.call_args.kwargs
    assert isinstance(kwargs["windower"], HeadlessWindower), (
        "an action that needs a terminal is useless in a launchd pass"
    )
    prompt = kwargs["choice"].prompt
    assert "checks-failed" in prompt
    assert "CI is failing on demo-1" in prompt, "the notification body is the brief's context"
    assert "gw pr checks" in prompt, "the per-event instruction"
    assert "demo-1" in prompt and str(task.worktree_path) in prompt, "the ordinary task brief"


def test_spawn_uses_the_configured_default_agent(demo, notifier) -> None:  # type: ignore[no-untyped-def]
    task = _task(demo, "demo-1")
    cfg = _cfg({"checks-failed": ["spawn-fix-session"]})
    cfg.defaults.agent = "codex"

    with patch("goblin_watcher.sync.actions.launch_agent", return_value=(0, task)) as launch:
        _pass(notifier, cfg, pr_state="OPEN", pr_checks="failing")

    assert launch.call_args.kwargs["agent"].name == "codex"


def test_spawn_fires_once_per_transition(demo, notifier) -> None:  # type: ignore[no-untyped-def]
    """A branch that stays red must not re-spawn a fixer every pass."""
    task = _task(demo, "demo-1")
    cfg = _cfg({"checks-failed": ["spawn-fix-session"]})

    with patch("goblin_watcher.sync.actions.launch_agent", return_value=(0, task)) as launch:
        _pass(notifier, cfg, pr_state="OPEN", pr_checks="failing")
        second = _pass(notifier, cfg, pr_state="OPEN", pr_checks="failing")

    assert launch.call_count == 1
    assert second.actions == []


def test_action_runs_even_when_its_notification_is_off(demo, notifier) -> None:  # type: ignore[no-untyped-def]
    """Acting on an edge and hearing about it are independent switches."""
    task = _task(demo, "demo-1")
    cfg = _cfg({"checks-failed": ["spawn-fix-session"]}, notify_events=["pr-merged"])

    with patch("goblin_watcher.sync.actions.launch_agent", return_value=(0, task)) as launch:
        report = _pass(notifier, cfg, pr_state="OPEN", pr_checks="failing")

    assert notifier.sent == []
    assert report.notifications == []
    assert launch.call_count == 1


def test_spawn_declines_while_an_agent_is_still_working(demo, notifier) -> None:  # type: ignore[no-untyped-def]
    """CI routinely goes red while an agent is mid-push. Don't pile on."""
    task = _task(demo, "demo-1")
    transcript = _working_transcript(task, "s1")
    now = datetime.now(UTC)
    state.update_task(
        demo,
        task.id,
        lambda t: t.model_copy(
            update={
                "sessions": [
                    SessionRecord(
                        agent="claude",
                        session_id="s1",
                        created_at=now,
                        last_used_at=now,
                        transcript_path=transcript,
                    )
                ]
            }
        ),
    )
    cfg = _cfg({"checks-failed": ["spawn-fix-session"]})

    with patch("goblin_watcher.sync.actions.launch_agent") as launch:
        report = _pass(notifier, cfg, pr_state="OPEN", pr_checks="failing")

    assert launch.call_count == 0
    assert report.actions == []
    assert any("s1 is still working" in str(e.get("detail")) for e in _events("action-skipped"))


def test_spawn_declines_on_an_archived_task(demo, notifier) -> None:  # type: ignore[no-untyped-def]
    """Rematerializing a checkout the user deliberately gave up is not an
    unattended decision (gh-23)."""
    task = _task(demo, "demo-1")
    state.update_task(demo, task.id, lambda t: t.model_copy(update={"archived": True}))
    cfg = _cfg({"checks-failed": ["spawn-fix-session"]})

    with patch("goblin_watcher.sync.actions.launch_agent") as launch:
        report = _pass(notifier, cfg, pr_state="OPEN", pr_checks="failing")

    assert launch.call_count == 0
    assert report.actions == []


def test_a_declined_action_does_not_start_the_cooldown(demo, notifier) -> None:  # type: ignore[no-untyped-def]
    """A skip must cost nothing, or a conservative guard becomes a wedge."""
    task = _task(demo, "demo-1")
    state.update_task(demo, task.id, lambda t: t.model_copy(update={"archived": True}))
    cfg = _cfg({"checks-failed": ["spawn-fix-session"]})

    _pass(notifier, cfg, pr_state="OPEN", pr_checks="failing")

    assert store.load_state().action_runs == {}


# ---------------------------------------------------------------------------
# Rate limiting and the per-pass cap.


def test_cooldown_suppresses_a_flapping_signal(demo, notifier) -> None:  # type: ignore[no-untyped-def]
    """The edge trigger bounds a *steady* signal; the cooldown bounds a flapping
    one — red, green, red inside the window is one action, not two."""
    task = _task(demo, "demo-1")
    cfg = _cfg({"checks-failed": ["spawn-fix-session"]}, action_rate_limit_seconds=3600)

    with patch("goblin_watcher.sync.actions.launch_agent", return_value=(0, task)) as launch:
        _pass(notifier, cfg, pr_state="OPEN", pr_checks="failing")
        _pass(notifier, cfg, pr_state="OPEN", pr_checks="passing")
        report = _pass(notifier, cfg, pr_state="OPEN", pr_checks="failing")

    assert launch.call_count == 1
    assert report.actions == []
    assert _events("action-rate-limited")


def test_an_expired_cooldown_lets_the_action_run_again(demo, notifier) -> None:  # type: ignore[no-untyped-def]
    task = _task(demo, "demo-1")
    cfg = _cfg({"checks-failed": ["spawn-fix-session"]}, action_rate_limit_seconds=60)

    with patch("goblin_watcher.sync.actions.launch_agent", return_value=(0, task)) as launch:
        _pass(notifier, cfg, pr_state="OPEN", pr_checks="failing")
        # Age the recorded run past the window instead of sleeping through it.
        st = store.load_state()
        key = "demo/demo-1:checks-failed:spawn-fix-session"
        assert key in st.action_runs
        st.action_runs[key] = datetime.now(UTC) - timedelta(seconds=600)
        store.save_state(st)

        _pass(notifier, cfg, pr_state="OPEN", pr_checks="passing")
        _pass(notifier, cfg, pr_state="OPEN", pr_checks="failing")

    assert launch.call_count == 2


def test_max_actions_per_pass_caps_the_fan_out(demo, notifier) -> None:  # type: ignore[no-untyped-def]
    """One CI outage turning twenty branches red is not twenty agents of work."""
    tasks = [_task(demo, f"demo-{n}", pr=f"https://gh/pr/{n}") for n in (1, 2, 3)]
    cfg = _cfg({"checks-failed": ["spawn-fix-session"]}, max_actions_per_pass=1)

    with patch("goblin_watcher.sync.actions.launch_agent", return_value=(0, tasks[0])) as launch:
        report = _pass(notifier, cfg, pr_state="OPEN", pr_checks="failing")

    assert launch.call_count == 1
    assert len(report.actions) == 1
    [capped] = _events("actions-capped")
    assert "2 deferred" in str(capped["detail"]), "a capped pass must not read as a complete one"


# ---------------------------------------------------------------------------
# prune.


def test_pr_merged_prunes_a_merged_clean_task(demo, notifier) -> None:  # type: ignore[no-untyped-def]
    task = _task(demo, "demo-1")
    # Periodic prune off, so the action is unambiguously what removed it.
    cfg = _cfg({"pr-merged": ["prune"]}, prune=False)

    report = _pass(notifier, cfg, pr_state="MERGED")

    assert report.actions == ["prune: demo-1 (pr-merged)"]
    assert report.pruned == ["demo/demo-1"]
    assert state.list_tasks(demo) == []
    assert not task.worktree_path.exists()


def test_pruning_forgets_the_tasks_derived_state(demo, notifier) -> None:  # type: ignore[no-untyped-def]
    """Including the cooldown key just written — a task reusing the id later
    must not inherit it and sit out its first action."""
    _task(demo, "demo-1")
    cfg = _cfg({"pr-merged": ["prune"]}, prune=False)

    _pass(notifier, cfg, pr_state="MERGED")

    st = store.load_state()
    assert st.action_runs == {}
    assert st.last_seen == {}
    assert store.load_cache().entries == {}


def test_prune_action_refuses_an_unmerged_branch(demo, notifier) -> None:  # type: ignore[no-untyped-def]
    """Wired to the wrong event, `prune` declines — it can never be weaker than
    the periodic prune, because it shares its safety checks."""
    task = _task(demo, "demo-1")
    cfg = _cfg({"checks-passed": ["prune"]}, prune=False)

    report = _pass(notifier, cfg, pr_state="OPEN", pr_checks="passing")

    assert report.actions == []
    assert state.load_task(demo, task.id).id == "demo-1"
    assert task.worktree_path.exists()


def test_prune_action_refuses_a_dirty_worktree(demo, notifier) -> None:  # type: ignore[no-untyped-def]
    task = _task(demo, "demo-1")
    (task.worktree_path / "wip.txt").write_text("unfinished\n")
    cfg = _cfg({"pr-merged": ["prune"]}, prune=False)

    report = _pass(notifier, cfg, pr_state="MERGED")

    assert report.actions == []
    assert task.worktree_path.exists()
    assert any("uncommitted" in str(e.get("detail")) for e in _events("action-skipped"))


# ---------------------------------------------------------------------------
# archive.


def test_archive_action_drops_the_worktree_and_keeps_the_record(demo, notifier) -> None:  # type: ignore[no-untyped-def]
    task = _task(demo, "demo-1")
    cfg = _cfg({"pr-merged": ["archive"]}, prune=False)

    report = _pass(notifier, cfg, pr_state="MERGED")

    assert report.actions == ["archive: demo-1 (pr-merged)"]
    assert not task.worktree_path.exists()
    kept = state.load_task(demo, task.id)
    assert kept.archived is True
    assert kept.branch == "demo-1"


def test_archive_action_refuses_a_dirty_worktree(demo, notifier) -> None:  # type: ignore[no-untyped-def]
    task = _task(demo, "demo-1")
    (task.worktree_path / "wip.txt").write_text("unfinished\n")
    cfg = _cfg({"pr-merged": ["archive"]}, prune=False)

    report = _pass(notifier, cfg, pr_state="MERGED")

    assert report.actions == []
    assert task.worktree_path.exists()
    assert state.load_task(demo, task.id).archived is False


# ---------------------------------------------------------------------------
# Failure isolation.


def test_a_failing_action_does_not_stop_the_pass(demo, notifier) -> None:  # type: ignore[no-untyped-def]
    _task(demo, "demo-1", pr="https://gh/pr/1")
    _task(demo, "demo-2", pr="https://gh/pr/2")
    cfg = _cfg({"checks-failed": ["spawn-fix-session"]}, max_actions_per_pass=0)

    calls: list[str] = []

    def _boom(**kwargs: object) -> tuple[int, Task]:
        task = kwargs["task"]
        calls.append(task.id)  # type: ignore[union-attr]
        raise GoblinError("claude is not on PATH")

    with patch("goblin_watcher.sync.actions.launch_agent", side_effect=_boom):
        report = _pass(notifier, cfg, pr_state="OPEN", pr_checks="failing")

    assert calls == ["demo-1", "demo-2"], "one broken action must not skip the rest"
    assert report.status == "partial"
    assert len(report.errors) == 2
    assert report.actions == []
    assert len(_events("action-failed")) == 2


def test_an_action_crash_makes_the_pass_error(demo, notifier) -> None:  # type: ignore[no-untyped-def]
    """A non-`GoblinError` is a bug, and must exit non-zero into the launchd log."""
    _task(demo, "demo-1")
    cfg = _cfg({"checks-failed": ["spawn-fix-session"]})

    with patch("goblin_watcher.sync.actions.launch_agent", side_effect=RuntimeError("boom")):
        report = _pass(notifier, cfg, pr_state="OPEN", pr_checks="failing")

    assert report.status == "error"
    assert _events("action-crashed")


def test_an_action_on_a_vanished_task_is_skipped(demo, notifier) -> None:  # type: ignore[no-untyped-def]
    """Step 7 pruning the task before the queue drains is a clean skip, not a
    crash against a deleted checkout."""
    _task(demo, "demo-1")
    # `prune` (periodic, step 7) removes the task; the queued spawn then finds
    # nothing to run in.
    cfg = _cfg({"pr-merged": ["spawn-fix-session"]}, prune=True)

    with patch("goblin_watcher.sync.actions.launch_agent") as launch:
        report = _pass(notifier, cfg, pr_state="MERGED")

    assert state.list_tasks(demo) == []
    assert launch.call_count == 0
    assert report.status == "ok"
    assert any("no longer exists" in str(e.get("detail")) for e in _events("action-skipped"))


# ---------------------------------------------------------------------------
# The brief.


def test_every_event_gets_a_usable_brief() -> None:
    """An action wired to an event with no tailored instruction still gets a
    session that knows what woke it up."""
    for event in ("checks-failed", "pr-merged", "prunable", "parent-merged"):
        brief = actions._brief(
            actions.PendingAction(
                project="demo", task_id="demo-1", event=event, action="spawn-fix-session", body="x"
            )
        )
        assert event in brief
        assert "headless" in brief
        assert actions._DEFAULT_INSTRUCTION not in brief, f"{event} should have its own instruction"

    generic = actions._brief(
        actions.PendingAction(
            project="demo",
            task_id="demo-1",
            event="checks-passed",
            action="spawn-fix-session",
            body="",
        )
    )
    assert "(no detail recorded)" in generic
