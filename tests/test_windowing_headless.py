"""Tests for the headless windower (gh-15, ADR 0007).

Nothing here spawns a real process: `subprocess.Popen` is always patched, in
the same spirit as the tmux tests never calling the real binary.
"""

from __future__ import annotations

import os
from datetime import UTC, datetime
from pathlib import Path
from unittest.mock import patch

import pytest

from goblin_watcher import state
from goblin_watcher.agents import AGENT_NAMES, get_agent
from goblin_watcher.errors import GoblinError
from goblin_watcher.models import Project, Task
from goblin_watcher.windowing import WINDOWING_MODES, get_windower
from goblin_watcher.windowing.headless import HeadlessWindower, log_file


def _register(tmp_path: Path) -> tuple[Project, Task]:
    root = tmp_path / "alpha"
    (root / ".goblin").mkdir(parents=True)
    proj = Project(name="alpha", root=root, created_at=datetime.now(UTC))
    state.save_project(proj)
    state.register_project(proj)
    task = Task(
        id="gh-15",
        project="alpha",
        branch="gh-15-headless",
        worktree_path=root / ".worktrees" / "gh-15",
        base_branch="main",
        created_at=datetime.now(UTC),
    )
    return proj, task


class _FakeProc:
    def __init__(self, pid: int = 4242) -> None:
        self.pid = pid


def test_get_windower_resolves_headless() -> None:
    assert isinstance(get_windower("headless"), HeadlessWindower)
    assert "headless" in WINDOWING_MODES


def test_unknown_mode_lists_every_choice() -> None:
    with pytest.raises(GoblinError) as exc:
        get_windower("screen")
    assert exc.value.hint is not None
    assert "headless" in exc.value.hint


def test_every_local_agent_has_a_headless_mode() -> None:
    """This windower only exists because each CLI already has a print mode.

    A newly registered agent that forgets `headless_command` should fail here
    rather than at 3am on a cron-driven run.
    """
    for name in AGENT_NAMES:
        agent = get_agent(name)
        if name == "managed":
            # No local process to detach; the sandbox already runs remotely.
            with pytest.raises(GoblinError):
                agent.headless_command(prompt="hi", cwd=Path("/tmp"))
            continue
        argv = agent.headless_command(prompt="hi", cwd=Path("/tmp"))
        assert argv[0] == agent.binary
        assert argv[-1] == "hi"


def test_run_detaches_and_redirects_output(isolated_xdg: Path, tmp_path: Path) -> None:
    """The whole point: no terminal, no blocking, output on disk."""
    _, task = _register(tmp_path)
    captured: dict[str, object] = {}

    def _fake_popen(cmd, **kwargs):
        captured["cmd"] = cmd
        captured.update(kwargs)
        return _FakeProc()

    with patch(
        "goblin_watcher.windowing.headless.subprocess.Popen", side_effect=_fake_popen
    ) as popen:
        rc = HeadlessWindower().run(
            task=task,
            cmd=["claude", "-p", "do the thing"],
            cwd=task.worktree_path,
            env={"FOO": "BAR"},
            session_id="sess-1",
        )

    assert rc == 0
    assert popen.call_count == 1
    assert captured["cmd"] == ["claude", "-p", "do the thing"]
    assert captured["cwd"] == str(task.worktree_path)
    # Own session/process group, so the launching shell's Ctrl-C or exit
    # doesn't take the unattended agent with it.
    assert captured["start_new_session"] is True
    # Nothing to read from: EOF beats hanging on an absent terminal.
    assert captured["stdin"] == -3  # subprocess.DEVNULL
    # Agent extras merge over the real environment, exactly as inline does.
    env = captured["env"]
    assert isinstance(env, dict)
    assert env["FOO"] == "BAR"
    assert env["PATH"] == os.environ["PATH"]
    # stdout is the log file handle; stderr is folded into it.
    assert getattr(captured["stdout"], "name", None) == str(log_file(task, "sess-1"))
    assert captured["stderr"] == -2  # subprocess.STDOUT


def test_log_lands_under_the_project_goblin_dir(isolated_xdg: Path, tmp_path: Path) -> None:
    proj, task = _register(tmp_path)
    path = log_file(task, "sess-1")
    assert path == proj.root / ".goblin" / "logs" / "gh-15-sess-1.log"


def test_run_creates_the_logs_dir_and_appends(isolated_xdg: Path, tmp_path: Path) -> None:
    """A second run on the same session must not erase the first one's output."""
    _, task = _register(tmp_path)
    path = log_file(task, "sess-1")
    assert not path.parent.exists()

    with patch(
        "goblin_watcher.windowing.headless.subprocess.Popen", return_value=_FakeProc()
    ) as popen:
        HeadlessWindower().run(
            task=task, cmd=["claude", "-p", "one"], cwd=tmp_path, env={}, session_id="sess-1"
        )
        # Simulate the first run having written something before the second starts.
        path.write_bytes(b"first run\n")
        HeadlessWindower().run(
            task=task, cmd=["claude", "-p", "two"], cwd=tmp_path, env={}, session_id="sess-1"
        )

    assert popen.call_count == 2
    assert path.read_bytes() == b"first run\n"


def test_run_records_the_pid(isolated_xdg: Path, tmp_path: Path) -> None:
    """The pid sidecar is the only handle a user has on a detached run."""
    _, task = _register(tmp_path)
    with patch(
        "goblin_watcher.windowing.headless.subprocess.Popen", return_value=_FakeProc(pid=9999)
    ):
        HeadlessWindower().run(
            task=task, cmd=["claude", "-p", "go"], cwd=tmp_path, env={}, session_id="sess-1"
        )
    pid_file = log_file(task, "sess-1").with_name("gh-15-sess-1.pid")
    assert pid_file.read_text().strip() == "9999"


def test_missing_binary_is_a_goblin_error(isolated_xdg: Path, tmp_path: Path) -> None:
    _, task = _register(tmp_path)
    with (
        patch(
            "goblin_watcher.windowing.headless.subprocess.Popen",
            side_effect=FileNotFoundError("no such file"),
        ),
        pytest.raises(GoblinError) as exc,
    ):
        HeadlessWindower().run(
            task=task, cmd=["nope", "-p", "go"], cwd=tmp_path, env={}, session_id="sess-1"
        )
    assert "nope" in exc.value.message
    assert exc.value.hint is not None
    assert "gw doctor" in exc.value.hint


def test_is_live_follows_the_recorded_pid(isolated_xdg: Path, tmp_path: Path) -> None:
    _, task = _register(tmp_path)
    windower = HeadlessWindower()
    # No logs dir at all yet.
    assert windower.is_live(task) is False

    logs = log_file(task, "sess-1").parent
    logs.mkdir(parents=True)
    # Our own pid is definitionally alive.
    (logs / "gh-15-sess-1.pid").write_text(f"{os.getpid()}\n")
    assert windower.is_live(task) is True

    # A dead (or unparseable) sidecar reads as not-live rather than raising.
    (logs / "gh-15-sess-1.pid").write_text("2147483647\n")
    assert windower.is_live(task) is False
    (logs / "gh-15-sess-1.pid").write_text("not-a-pid\n")
    assert windower.is_live(task) is False


def test_is_live_does_not_match_a_task_whose_id_is_a_prefix(
    isolated_xdg: Path, tmp_path: Path
) -> None:
    """`gh-1` must not see `gh-15`'s pid file as its own."""
    _, task = _register(tmp_path)  # gh-15
    sibling = task.model_copy(update={"id": "gh-1"})
    logs = log_file(task, "sess-1").parent
    logs.mkdir(parents=True)
    (logs / "gh-15-sess-1.pid").write_text(f"{os.getpid()}\n")
    assert HeadlessWindower().is_live(task) is True
    assert HeadlessWindower().is_live(sibling) is False


def test_send_explains_there_is_no_input(isolated_xdg: Path, tmp_path: Path) -> None:
    _, task = _register(tmp_path)
    with pytest.raises(GoblinError) as exc:
        HeadlessWindower().send(task=task, text="also fix the tests")
    assert "take no input" in exc.value.message
    assert exc.value.hint is not None
    assert "tmux" in exc.value.hint


def test_remove_run_files_clears_a_destroyed_tasks_logs(isolated_xdg: Path, tmp_path: Path) -> None:
    """A removed task's log has no record left to reference it, so it goes too."""
    from goblin_watcher.windowing.headless import remove_run_files

    _, task = _register(tmp_path)
    sibling = task.model_copy(update={"id": "gh-1"})
    logs = log_file(task, "sess-1").parent
    logs.mkdir(parents=True)
    mine = [logs / "gh-15-sess-1.log", logs / "gh-15-sess-1.pid"]
    theirs = [logs / "gh-1-sess-9.log", logs / "unrelated.log"]
    for p in (*mine, *theirs):
        p.write_text("x")

    remove_run_files(task)
    assert not any(p.exists() for p in mine)
    # Another task's files — including one whose id is a prefix — are untouched.
    assert all(p.exists() for p in theirs)
    # Idempotent, and fine when there is no logs dir at all.
    remove_run_files(task)
    remove_run_files(sibling.model_copy(update={"project": "nope"}))
