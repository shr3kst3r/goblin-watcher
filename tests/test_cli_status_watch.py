"""`gw status --active` / `--watch` — the live dashboard (gh-21).

Two things are worth pinning down here: what the `--active` filter counts as
"in flight", and that `--watch` renders off local state only. The watch loop is
driven by patching `time.sleep` to raise `KeyboardInterrupt`, which is exactly
how a user leaves it — so the test exercises the real exit path.
"""

from __future__ import annotations

import os
import subprocess
from datetime import UTC, datetime
from pathlib import Path
from unittest.mock import patch

from typer.testing import CliRunner

from goblin_watcher import state
from goblin_watcher.cli import app
from goblin_watcher.errors import GoblinError
from goblin_watcher.models import SessionRecord, Task


def _init_repo(path: Path) -> None:
    path.mkdir(parents=True, exist_ok=True)
    subprocess.run(["git", "init", "-q", "-b", "main", str(path)], check=True)
    subprocess.run(["git", "-C", str(path), "config", "user.email", "t@t"], check=True)
    subprocess.run(["git", "-C", str(path), "config", "user.name", "tester"], check=True)
    (path / "README.md").write_text("hi")
    subprocess.run(["git", "-C", str(path), "add", "."], check=True)
    subprocess.run(["git", "-C", str(path), "commit", "-qm", "init"], check=True)


def _project(tmp_path: Path, name: str = "alpha") -> None:
    _init_repo(tmp_path / name)
    CliRunner().invoke(app, ["project", "new", name, "--dir", str(tmp_path / name)])


def _task(branch: str, project: str = "alpha") -> Task:
    CliRunner().invoke(app, ["new", "--branch-name", branch, "--project", project, "--no-launch"])
    proj = state.get_project(project)
    [task] = [t for t in state.list_tasks(proj) if t.branch == branch]
    return task


def _attach_session(
    task: Task,
    *,
    transcript: Path,
    age_seconds: float,
    summary: str,
    project: str = "alpha",
) -> None:
    """Give `task` one claude session whose transcript is `age_seconds` old."""
    transcript.write_text("{}\n")
    when = datetime.now(UTC).timestamp() - age_seconds
    os.utime(transcript, (when, when))
    now = datetime.now(UTC)
    record = SessionRecord(
        agent="claude",
        session_id=f"s-{task.id}",
        created_at=now,
        last_used_at=now,
        summary=summary,
        summary_updated_at=now,
        transcript_path=transcript,
    )
    proj = state.get_project(project)
    state.save_task(proj, task.model_copy(update={"sessions": [record]}))


# --- the --active filter ----------------------------------------------------


def test_active_keeps_a_session_that_just_went_quiet(isolated_xdg: Path, tmp_path: Path) -> None:
    """Four minutes idle is past the mtime active window but inside the grace.

    This is the case the filter exists for: an agent that stopped to ask a
    question is the thing you most need on screen, and it stops writing its
    transcript within two minutes of doing so.
    """
    _project(tmp_path)
    task = _task("spike/quiet")
    _attach_session(task, transcript=tmp_path / "q.jsonl", age_seconds=240, summary="asked you")

    res = CliRunner().invoke(app, ["status", "--active", "--no-linear"])
    assert res.exit_code == 0, res.output
    assert task.id in res.output
    assert "asked you" in res.output


def test_active_hides_a_long_idle_task(isolated_xdg: Path, tmp_path: Path) -> None:
    _project(tmp_path)
    task = _task("spike/stale")
    _attach_session(task, transcript=tmp_path / "s.jsonl", age_seconds=7200, summary="ancient")

    res = CliRunner().invoke(app, ["status", "--active", "--no-linear"])
    assert res.exit_code == 0, res.output
    assert task.id not in res.output
    assert "Nothing in flight" in res.output


def test_active_hides_a_task_with_no_sessions(isolated_xdg: Path, tmp_path: Path) -> None:
    _project(tmp_path)
    task = _task("spike/never-run")

    res = CliRunner().invoke(app, ["status", "--active", "--no-linear"])
    assert res.exit_code == 0, res.output
    assert task.id not in res.output


def test_plain_status_still_shows_everything(isolated_xdg: Path, tmp_path: Path) -> None:
    """`--active` is opt-in; the default tree is unchanged."""
    _project(tmp_path)
    task = _task("spike/stale")
    _attach_session(task, transcript=tmp_path / "s.jsonl", age_seconds=7200, summary="ancient")

    res = CliRunner().invoke(app, ["status", "--no-linear"])
    assert res.exit_code == 0, res.output
    assert task.id in res.output


def test_active_keeps_a_live_headless_run_with_a_cold_transcript(
    isolated_xdg: Path, tmp_path: Path
) -> None:
    """A headless agent can go a long time between transcript writes.

    Its pid file is the second, independent signal — without it, an unattended
    run would drop off the dashboard while still working.
    """
    _project(tmp_path)
    task = _task("spike/headless")
    _attach_session(task, transcript=tmp_path / "h.jsonl", age_seconds=7200, summary="churning")

    with patch("goblin_watcher.windowing.headless.has_live_run", return_value=True):
        res = CliRunner().invoke(app, ["status", "--active", "--no-linear"])
    assert res.exit_code == 0, res.output
    assert task.id in res.output
    assert "headless" in res.output


def test_headless_badge_absent_when_no_run_is_alive(isolated_xdg: Path, tmp_path: Path) -> None:
    _project(tmp_path)
    task = _task("spike/plain")
    _attach_session(task, transcript=tmp_path / "p.jsonl", age_seconds=10, summary="typing")

    res = CliRunner().invoke(app, ["status", "--no-linear"])
    assert res.exit_code == 0, res.output
    assert task.id in res.output
    assert "⚡ headless" not in res.output


def test_active_drops_a_project_with_nothing_in_flight(isolated_xdg: Path, tmp_path: Path) -> None:
    """A project heading with no rows under it is noise on a dashboard."""
    _project(tmp_path, "alpha")
    _project(tmp_path, "beta")
    busy = _task("spike/busy", project="alpha")
    _attach_session(busy, transcript=tmp_path / "b.jsonl", age_seconds=5, summary="busy")
    quiet = _task("spike/quiet", project="beta")
    _attach_session(quiet, transcript=tmp_path / "q.jsonl", age_seconds=9000, summary="quiet")

    res = CliRunner().invoke(app, ["status", "--active", "--no-linear"])
    assert res.exit_code == 0, res.output
    assert "alpha" in res.output
    assert "beta" not in res.output


def test_active_respects_a_configured_grace_window(isolated_xdg: Path, tmp_path: Path) -> None:
    _project(tmp_path)
    task = _task("spike/tight")
    _attach_session(task, transcript=tmp_path / "t.jsonl", age_seconds=300, summary="five minutes")
    runner = CliRunner()

    assert task.id in runner.invoke(app, ["status", "--active", "--no-linear"]).output

    runner.invoke(app, ["config", "set", "defaults.activity_grace_seconds", "60"])
    res = runner.invoke(app, ["status", "--active", "--no-linear"])
    assert res.exit_code == 0, res.output
    assert task.id not in res.output


def test_active_skips_the_ticket_refresh_for_hidden_tasks(
    isolated_xdg: Path, tmp_path: Path
) -> None:
    """The filter runs before the network, which is most of what makes it fast."""
    _project(tmp_path)
    task = _task("spike/stale")
    _attach_session(task, transcript=tmp_path / "s.jsonl", age_seconds=7200, summary="ancient")

    with patch("goblin_watcher.linear_state.LinearStateFetcher.refresh") as refresh:
        res = CliRunner().invoke(app, ["status", "--active"])
    assert res.exit_code == 0, res.output
    refresh.assert_not_called()


# --- --watch ----------------------------------------------------------------


def _stop_after(ticks: int):
    """A `time.sleep` stand-in that lets the loop run `ticks` times, then Ctrl-Cs."""
    calls = {"n": 0}

    def fake_sleep(_seconds: float) -> None:
        calls["n"] += 1
        if calls["n"] >= ticks:
            raise KeyboardInterrupt
        return None

    return fake_sleep, calls


def test_watch_renders_and_exits_cleanly_on_ctrl_c(isolated_xdg: Path, tmp_path: Path) -> None:
    _project(tmp_path)
    task = _task("spike/watched")
    _attach_session(task, transcript=tmp_path / "w.jsonl", age_seconds=5, summary="in flight")
    sleep, calls = _stop_after(2)

    with patch("goblin_watcher.commands.status.time.sleep", side_effect=sleep):
        res = CliRunner().invoke(app, ["status", "--watch", "--active"])

    assert res.exit_code == 0, res.output
    assert calls["n"] == 2
    assert task.id in res.output
    assert "Stopped." in res.output


def test_watch_never_touches_the_network(isolated_xdg: Path, tmp_path: Path) -> None:
    """The whole premise of the dashboard: it renders off local state and the
    sync cache, so leaving it open costs no API round-trips."""
    _project(tmp_path)
    task = _task("spike/watched")
    _attach_session(task, transcript=tmp_path / "w.jsonl", age_seconds=5, summary="in flight")
    sleep, _calls = _stop_after(2)

    with (
        patch("goblin_watcher.commands.status.time.sleep", side_effect=sleep),
        patch("goblin_watcher.linear_state.LinearStateFetcher.refresh") as linear_refresh,
        patch("goblin_watcher.github_state.refresh") as gh_refresh,
        patch("goblin_watcher.sessions.schedule_descriptions") as describe,
    ):
        res = CliRunner().invoke(app, ["status", "--watch"])

    assert res.exit_code == 0, res.output
    linear_refresh.assert_not_called()
    gh_refresh.assert_not_called()
    describe.assert_not_called()


def test_watch_does_not_rewrite_state_when_nothing_changed(
    isolated_xdg: Path, tmp_path: Path
) -> None:
    """A tick that finds no new transcript bytes must not take the task lock and
    rewrite identical JSON — at a 2s poll that would be a write every 2s forever."""
    _project(tmp_path)
    task = _task("spike/watched")
    _attach_session(task, transcript=tmp_path / "w.jsonl", age_seconds=5, summary="in flight")
    sleep, _calls = _stop_after(3)

    with (
        patch("goblin_watcher.commands.status.time.sleep", side_effect=sleep),
        patch(
            "goblin_watcher.sessions.persist_refresh", side_effect=lambda _p, t, *_a, **_k: t
        ) as persist,
    ):
        res = CliRunner().invoke(app, ["status", "--watch"])

    assert res.exit_code == 0, res.output
    persist.assert_not_called()


def test_watch_says_so_when_nothing_is_in_flight(isolated_xdg: Path, tmp_path: Path) -> None:
    """An empty filtered dashboard has to explain itself, or it reads as broken."""
    _project(tmp_path)
    task = _task("spike/stale")
    _attach_session(task, transcript=tmp_path / "s.jsonl", age_seconds=7200, summary="ancient")
    sleep, _calls = _stop_after(1)

    with patch("goblin_watcher.commands.status.time.sleep", side_effect=sleep):
        res = CliRunner().invoke(app, ["status", "--watch", "--active"])

    assert res.exit_code == 0, res.output
    assert "Nothing in flight" in res.output


def test_watch_rejects_a_too_small_interval(isolated_xdg: Path, tmp_path: Path) -> None:
    """`main()` is what maps GoblinError to an exit code, so CliRunner sees the
    exception itself — assert on that rather than on a status the runner never
    produces."""
    _project(tmp_path)
    res = CliRunner().invoke(app, ["status", "--watch", "--interval", "0.01"])
    assert isinstance(res.exception, GoblinError)
    assert res.exception.exit_code == 2
    assert "--interval" in res.exception.message
