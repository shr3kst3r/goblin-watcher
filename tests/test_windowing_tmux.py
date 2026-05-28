from datetime import UTC, datetime
from pathlib import Path
from unittest.mock import patch

import pytest

from goblin_watcher.models import Task
from goblin_watcher.windowing.tmux import TmuxWindower


def _task(tmp_path: Path, task_id: str = "eng-123") -> Task:
    return Task(
        id=task_id,
        project="p",
        branch="b",
        worktree_path=tmp_path,
        base_branch="main",
        created_at=datetime.now(UTC),
    )


@pytest.fixture
def fake_tmux(monkeypatch: pytest.MonkeyPatch):
    """Pretend tmux is on PATH but record every invocation instead of executing."""
    monkeypatch.setattr("goblin_watcher.windowing.tmux.shutil.which", lambda _: "/usr/bin/tmux")
    monkeypatch.setenv("TMUX", "")  # ensure not "inside" tmux

    calls = []

    class _FakeRes:
        returncode = 1
        stdout = ""
        stderr = ""

    def _fake_run(cmd, capture_output, text, check):
        calls.append(cmd)
        return _FakeRes()

    monkeypatch.setattr("goblin_watcher.windowing.tmux.subprocess.run", _fake_run)
    return calls


def test_tmux_run_creates_session_and_window(isolated_xdg: Path, tmp_path: Path, fake_tmux) -> None:
    """When `attach_on_spawn` is on and we're outside tmux, the windower would
    `os.execvp` into `tmux attach`. We mock execvp so the test process survives."""

    def _fake_execvp(*_a, **_kw):
        raise SystemExit(0)

    with (
        patch("goblin_watcher.windowing.tmux.os.execvp", side_effect=_fake_execvp),
        patch("goblin_watcher.windowing.tmux.os.environ", new={"TMUX": ""}),
        pytest.raises(SystemExit),
    ):
        TmuxWindower().run(
            task=_task(tmp_path),
            cmd=["claude", "hi"],
            cwd=tmp_path,
            env={},
        )
    flat = [" ".join(c) for c in fake_tmux]
    assert any("new-session" in c for c in flat)
    assert any("new-window -t goblin -n eng-123" in c for c in flat)
    assert any("send-keys -t goblin:eng-123 claude hi Enter" in c for c in flat)


def _patch_inside_tmux_with_existing_window(monkeypatch: pytest.MonkeyPatch) -> list[list[str]]:
    """Pretend we're already inside tmux and the task window already exists."""
    monkeypatch.setattr("goblin_watcher.windowing.tmux.shutil.which", lambda _: "/usr/bin/tmux")
    monkeypatch.setenv("TMUX", "tmux-1234,1,0")

    calls: list[list[str]] = []

    class _FakeRes:
        def __init__(self, code: int = 0, stdout: str = "") -> None:
            self.returncode = code
            self.stdout = stdout
            self.stderr = ""

    def _fake_run(cmd, capture_output, text, check):
        calls.append(cmd)
        if "has-session" in cmd:
            return _FakeRes(0)
        if "list-windows" in cmd:
            return _FakeRes(0, stdout="eng-123\n")
        return _FakeRes(0)

    monkeypatch.setattr("goblin_watcher.windowing.tmux.subprocess.run", _fake_run)
    return calls


def test_tmux_run_splits_pane_when_window_exists(
    isolated_xdg: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """If the task window already exists, second invocation splits the existing pane.

    Default split orientation is `vertical` → tmux `-v` (panes stacked top/bottom).
    """
    calls = _patch_inside_tmux_with_existing_window(monkeypatch)

    rc = TmuxWindower().run(
        task=_task(tmp_path),
        cmd=["claude", "hi"],
        cwd=tmp_path,
        env={},
    )
    assert rc == 0
    flat = [" ".join(c) for c in calls]
    assert any("split-window -v -t goblin:eng-123" in c for c in flat)
    assert not any("new-window" in c for c in flat)


def test_tmux_split_horizontal_passes_h_flag(
    isolated_xdg: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Setting `tmux.split = "horizontal"` swaps in tmux's `-h` flag (panes side-by-side)."""
    from goblin_watcher import config

    cfg = config.Config()
    cfg.tmux.split = "horizontal"
    config.save(cfg)

    calls = _patch_inside_tmux_with_existing_window(monkeypatch)
    rc = TmuxWindower().run(
        task=_task(tmp_path),
        cmd=["claude", "hi"],
        cwd=tmp_path,
        env={},
    )
    assert rc == 0
    flat = [" ".join(c) for c in calls]
    assert any("split-window -h -t goblin:eng-123" in c for c in flat)


def test_tmux_mark_idle_sets_monitor_silence(
    isolated_xdg: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`mark_idle = true` wires monitor-silence per window (no hook)."""
    from goblin_watcher import config

    cfg = config.Config()
    cfg.tmux.mark_idle = True
    cfg.tmux.mark_idle_seconds = 7
    config.save(cfg)

    calls = _patch_inside_tmux_with_existing_window(monkeypatch)
    rc = TmuxWindower().run(
        task=_task(tmp_path),
        cmd=["claude", "hi"],
        cwd=tmp_path,
        env={},
    )
    assert rc == 0
    flat = [" ".join(c) for c in calls]
    assert any("set-window-option -t goblin:eng-123 monitor-silence 7" in c for c in flat)
    # We must never *install* the run-shell hook; only the defensive `-u` unset is allowed.
    assert not any("set-hook" in c and "alert-silence" in c and "-u" not in c for c in flat)


def test_tmux_ensure_session_clears_stale_alert_silence_hook(
    isolated_xdg: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Every run pass-clears any leftover `alert-silence` hook on the session.

    Older `bell_on_idle` builds installed a session-scoped hook that ran
    `printf \\a > /dev/tty`. The unset is idempotent against modern sessions
    that never had the hook in the first place.
    """
    calls = _patch_inside_tmux_with_existing_window(monkeypatch)
    rc = TmuxWindower().run(
        task=_task(tmp_path),
        cmd=["claude", "hi"],
        cwd=tmp_path,
        env={},
    )
    assert rc == 0
    flat = [" ".join(c) for c in calls]
    assert any("set-hook -u -t goblin alert-silence" in c for c in flat)


def test_tmux_mark_idle_off_by_default(
    isolated_xdg: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """No monitor-silence calls when the feature is left at its default."""
    calls = _patch_inside_tmux_with_existing_window(monkeypatch)
    rc = TmuxWindower().run(
        task=_task(tmp_path),
        cmd=["claude", "hi"],
        cwd=tmp_path,
        env={},
    )
    assert rc == 0
    flat = [" ".join(c) for c in calls]
    assert not any("monitor-silence" in c for c in flat)
