from datetime import UTC, datetime
from pathlib import Path
from unittest.mock import patch

import pytest

from goblin_watcher.errors import GoblinError
from goblin_watcher.models import Task
from goblin_watcher.windowing.inline import InlineWindower


def _task(tmp_path: Path) -> Task:
    return Task(
        id="t1",
        project="p",
        branch="b",
        worktree_path=tmp_path,
        base_branch="main",
        created_at=datetime.now(UTC),
    )


def test_inline_windower_runs_subprocess(tmp_path: Path) -> None:
    captured = {}

    class _FakeProc:
        returncode = 0

    def _fake_run(cmd, cwd, env, check):
        captured["cmd"] = cmd
        captured["cwd"] = cwd
        return _FakeProc()

    with patch("goblin_watcher.windowing.inline.subprocess.run", side_effect=_fake_run):
        rc = InlineWindower().run(
            task=_task(tmp_path),
            cmd=["echo", "hi"],
            cwd=tmp_path,
            env={"FOO": "BAR"},
        )
    assert rc == 0
    assert captured["cmd"] == ["echo", "hi"]
    assert captured["cwd"] == str(tmp_path)


def test_inline_windower_propagates_exit_code(tmp_path: Path) -> None:
    class _FakeProc:
        returncode = 42

    with patch("goblin_watcher.windowing.inline.subprocess.run", return_value=_FakeProc()):
        rc = InlineWindower().run(
            task=_task(tmp_path),
            cmd=["false"],
            cwd=tmp_path,
            env={},
        )
    assert rc == 42


def test_inline_windower_send_explains_there_is_no_pane(tmp_path: Path) -> None:
    """`gw session send` against inline windowing must fail legibly, not obscurely."""
    with pytest.raises(GoblinError) as exc:
        InlineWindower().send(task=_task(tmp_path), text="also fix the tests")
    assert "no pane to send to" in exc.value.message
    assert exc.value.hint is not None
    assert "tmux" in exc.value.hint
