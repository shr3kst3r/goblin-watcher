"""Tests for `gw session send` — delivering input to an already-running agent."""

import subprocess
from datetime import UTC, datetime
from pathlib import Path
from unittest.mock import patch

from typer.testing import CliRunner

from goblin_watcher import state
from goblin_watcher.cli import app
from goblin_watcher.models import SessionRecord, Task

runner = CliRunner()


def _init_repo(path: Path) -> None:
    path.mkdir(parents=True, exist_ok=True)
    subprocess.run(["git", "init", "-q", "-b", "main", str(path)], check=True)
    subprocess.run(["git", "-C", str(path), "config", "user.email", "t@t"], check=True)
    subprocess.run(["git", "-C", str(path), "config", "user.name", "tester"], check=True)
    (path / "README.md").write_text("hi")
    subprocess.run(["git", "-C", str(path), "add", "."], check=True)
    subprocess.run(["git", "-C", str(path), "commit", "-qm", "init"], check=True)


def _bootstrap(tmp_path: Path, session_ids: list[str]) -> Task:
    repo = tmp_path / "alpha"
    _init_repo(repo)
    runner.invoke(app, ["project", "new", "alpha", "--dir", str(repo)])
    runner.invoke(app, ["new", "--branch-name", "spike/foo", "--no-launch"])
    proj = state.get_project("alpha")
    [task] = state.list_tasks(proj)
    now = datetime.now(UTC)
    records = [
        SessionRecord(agent="claude", session_id=sid, created_at=now, last_used_at=now)
        for sid in session_ids
    ]
    task = task.model_copy(update={"sessions": records})
    state.save_task(proj, task)
    return task


class _RecordingWindower:
    """Stands in for the tmux windower; records what `send` was asked to do."""

    name = "tmux"

    def __init__(self) -> None:
        self.calls: list[dict[str, object]] = []

    def send(self, *, task, text: str, session_id: str | None = None, enter: bool = True) -> str:
        self.calls.append({"task": task.id, "text": text, "session_id": session_id, "enter": enter})
        return "goblin:eng-1 pane %3"


def test_send_delivers_message_to_the_task(isolated_xdg: Path, tmp_path: Path) -> None:
    task = _bootstrap(tmp_path, ["s1"])
    windower = _RecordingWindower()

    with patch("goblin_watcher.commands.session.get_windower", return_value=windower):
        res = runner.invoke(app, ["session", "send", task.id, "also fix the tests"])

    assert res.exit_code == 0, res.output
    assert windower.calls == [
        {"task": task.id, "text": "also fix the tests", "session_id": None, "enter": True}
    ]
    assert "Sent to" in res.output


def test_send_passes_session_and_no_enter_through(isolated_xdg: Path, tmp_path: Path) -> None:
    task = _bootstrap(tmp_path, ["s1", "s2"])
    windower = _RecordingWindower()

    with patch("goblin_watcher.commands.session.get_windower", return_value=windower):
        res = runner.invoke(
            app, ["session", "send", task.id, "hi", "--session", "s2", "--no-enter"]
        )

    assert res.exit_code == 0, res.output
    assert windower.calls[0]["session_id"] == "s2"
    assert windower.calls[0]["enter"] is False


def test_send_rejects_a_session_that_is_not_on_the_task(isolated_xdg: Path, tmp_path: Path) -> None:
    task = _bootstrap(tmp_path, ["s1"])
    windower = _RecordingWindower()

    with patch("goblin_watcher.commands.session.get_windower", return_value=windower):
        res = runner.invoke(app, ["session", "send", task.id, "hi", "--session", "nope"])

    assert res.exit_code != 0
    assert "is not on task" in str(res.exception)
    assert windower.calls == []


def test_send_resolves_the_task_from_a_worktree_path(isolated_xdg: Path, tmp_path: Path) -> None:
    """`gw session send . "…"` works from inside the worktree, like `gw run`."""
    task = _bootstrap(tmp_path, ["s1"])
    windower = _RecordingWindower()

    with patch("goblin_watcher.commands.session.get_windower", return_value=windower):
        res = runner.invoke(app, ["session", "send", str(task.worktree_path), "hi"])

    assert res.exit_code == 0, res.output
    assert windower.calls[0]["task"] == task.id


def test_send_under_inline_windowing_says_why_it_cannot(isolated_xdg: Path, tmp_path: Path) -> None:
    """Default windowing is inline; the failure has to name the reason."""
    task = _bootstrap(tmp_path, ["s1"])

    res = runner.invoke(app, ["session", "send", task.id, "hi"])

    assert res.exit_code != 0
    assert "no pane to send to" in str(res.exception)


def test_send_rejects_an_empty_message_with_no_enter(isolated_xdg: Path, tmp_path: Path) -> None:
    task = _bootstrap(tmp_path, ["s1"])
    windower = _RecordingWindower()

    with patch("goblin_watcher.commands.session.get_windower", return_value=windower):
        res = runner.invoke(app, ["session", "send", task.id, "", "--no-enter"])

    assert res.exit_code != 0
    assert "Nothing to send" in str(res.exception)
    assert windower.calls == []


def test_send_bare_enter_is_allowed(isolated_xdg: Path, tmp_path: Path) -> None:
    """An empty message with Enter is how you answer an agent's y/n prompt."""
    task = _bootstrap(tmp_path, ["s1"])
    windower = _RecordingWindower()

    with patch("goblin_watcher.commands.session.get_windower", return_value=windower):
        res = runner.invoke(app, ["session", "send", task.id, ""])

    assert res.exit_code == 0, res.output
    assert windower.calls[0] == {"task": task.id, "text": "", "session_id": None, "enter": True}
