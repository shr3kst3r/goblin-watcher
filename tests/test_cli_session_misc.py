"""Tests for `gw session transcript` and the all-sessions refresh path."""

import subprocess
from datetime import UTC, datetime
from pathlib import Path
from unittest.mock import patch

from typer.testing import CliRunner

from goblin_watcher import state
from goblin_watcher.cli import app
from goblin_watcher.models import SessionRecord

runner = CliRunner()


def _init_repo(path: Path) -> None:
    path.mkdir(parents=True, exist_ok=True)
    subprocess.run(["git", "init", "-q", "-b", "main", str(path)], check=True)
    subprocess.run(["git", "-C", str(path), "config", "user.email", "t@t"], check=True)
    subprocess.run(["git", "-C", str(path), "config", "user.name", "tester"], check=True)
    (path / "README.md").write_text("hi")
    subprocess.run(["git", "-C", str(path), "add", "."], check=True)
    subprocess.run(["git", "-C", str(path), "commit", "-qm", "init"], check=True)


def _record(session_id: str, transcript: Path | None = None) -> SessionRecord:
    now = datetime.now(UTC)
    return SessionRecord(
        agent="claude",
        session_id=session_id,
        created_at=now,
        last_used_at=now,
        summary="something",
        summary_updated_at=now,
        transcript_path=transcript,
    )


def _bootstrap_task_with_sessions(
    tmp_path: Path, session_ids: list[str], transcript: Path | None = None
) -> None:
    repo = tmp_path / "alpha"
    _init_repo(repo)
    runner.invoke(app, ["project", "new", "alpha", "--dir", str(repo)])
    runner.invoke(app, ["new", "--branch-name", "spike/foo", "--no-launch"])
    proj = state.get_project("alpha")
    [task] = state.list_tasks(proj)
    records = [_record(sid, transcript) for sid in session_ids]
    state.save_task(proj, task.model_copy(update={"sessions": records}))


def test_session_transcript_renders_text(isolated_xdg: Path, tmp_path: Path) -> None:
    _bootstrap_task_with_sessions(tmp_path, ["s1"])

    class _FakeAgent:
        def render_transcript(self, _sid: str, _cwd: Path) -> str:
            return "[user]\nhello\n\n[assistant]\nhi there"

    with patch("goblin_watcher.commands.session.get_agent", return_value=_FakeAgent()):
        res = runner.invoke(app, ["session", "transcript", "s1"])
    assert res.exit_code == 0, res.output
    assert "[user]" in res.stdout
    assert "hi there" in res.stdout


def test_session_transcript_raw_prints_path(isolated_xdg: Path, tmp_path: Path) -> None:
    transcript = tmp_path / "t.jsonl"
    transcript.write_text("{}\n")
    _bootstrap_task_with_sessions(tmp_path, ["s1"], transcript=transcript)

    res = runner.invoke(app, ["session", "transcript", "s1", "--raw"])
    assert res.exit_code == 0, res.output
    assert res.stdout.strip() == str(transcript)


def test_session_transcript_missing_errors(isolated_xdg: Path, tmp_path: Path) -> None:
    _bootstrap_task_with_sessions(tmp_path, ["s1"])

    class _FakeAgent:
        def render_transcript(self, _sid: str, _cwd: Path) -> None:
            return None

    with patch("goblin_watcher.commands.session.get_agent", return_value=_FakeAgent()):
        res = runner.invoke(app, ["session", "transcript", "s1"])
    assert res.exit_code != 0
    assert res.exception is not None
    assert "No transcript available" in str(res.exception)


def test_session_refresh_all_counts_each_session_once(isolated_xdg: Path, tmp_path: Path) -> None:
    """A task with N sessions used to be refreshed N times and counted N²."""
    _bootstrap_task_with_sessions(tmp_path, ["s1", "s2", "s3"])

    refreshed_tasks: list[str] = []

    def _fake_refresh(task):
        refreshed_tasks.append(task.id)
        return task

    with patch(
        "goblin_watcher.commands.session.sessions.refresh_task_summaries",
        side_effect=_fake_refresh,
    ):
        res = runner.invoke(app, ["session", "refresh"])
    assert res.exit_code == 0, res.output
    assert "Refreshed 3 session(s)" in res.output
    assert len(refreshed_tasks) == 1


def test_session_show_reports_tokens_and_cost(isolated_xdg: Path, tmp_path: Path) -> None:
    from goblin_watcher.models import UsageBucket

    _bootstrap_task_with_sessions(tmp_path, ["s1"])
    proj = state.get_project("alpha")
    [task] = state.list_tasks(proj)
    [record] = task.sessions
    record = record.model_copy(
        update={
            "usage": [
                UsageBucket(
                    model="claude-opus-5",
                    input_tokens=2_000_000,
                    output_tokens=1_000_000,
                    cache_read_tokens=10_000_000,
                )
            ]
        }
    )
    state.save_task(proj, task.model_copy(update={"sessions": [record]}))

    res = runner.invoke(app, ["session", "show", "s1"], env={"COLUMNS": "400"})
    assert res.exit_code == 0, res.output
    assert "2.0M in · 1.0M out · 10.0M cache read" in res.output
    # $10 input + $25 output + $5 cache read.
    assert "~$40.00" in res.output
    assert "claude-opus-5" in res.output


def test_session_show_says_none_recorded_without_usage(isolated_xdg: Path, tmp_path: Path) -> None:
    _bootstrap_task_with_sessions(tmp_path, ["s1"])
    res = runner.invoke(app, ["session", "show", "s1"])
    assert res.exit_code == 0, res.output
    assert "none recorded" in res.output
