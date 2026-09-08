import os
import shlex
import time
from datetime import UTC, datetime
from pathlib import Path
from unittest.mock import patch

import pytest

from goblin_watcher import paths
from goblin_watcher.errors import GoblinError
from goblin_watcher.models import Task
from goblin_watcher.windowing.tmux import (
    _SCRIPT_TTL_SECONDS,
    TmuxWindower,
    _argv_size,
    _prune_pane_scripts,
)

# tmux's client refuses a command whose argv exceeds MAX_IMSGSIZE. Asserting
# against the real ceiling (rather than gw's budget) is what makes these tests
# about the failure they prevent.
_TMUX_MESSAGE_CAP = 16 * 1024

# Shaped like the seed prompt that first hit this: long, and dense with
# apostrophes, which shell-quoting inflates ~1.7x on the way into tmux.
_LONG_PROMPT = "the worker pod's ephemeral-storage limit is 4Gi today. " * 400


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
        def __init__(self, code: int) -> None:
            self.returncode = code
            self.stdout = ""
            self.stderr = ""

    def _fake_run(cmd, capture_output, text, check):
        calls.append(cmd)
        # `has-session` / `list-windows` return nonzero so the windower creates
        # a fresh session and treats the task window as absent. Window/pane
        # creation (`new-window`, `split-window`) must succeed (0), else the
        # windower now raises.
        sub = cmd[1] if len(cmd) > 1 else ""
        code = 1 if sub in ("has-session", "list-windows") else 0
        return _FakeRes(code)

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
    assert any("new-window -a -t goblin -n eng-123" in c for c in flat)
    # The agent command is the pane's process (no `send-keys`), wrapped in a
    # login-interactive shell with omz auto-update suppressed.
    assert not any("send-keys" in c for c in flat)
    new_window = next(c for c in fake_tmux if "new-window" in c)
    pane_cmd = new_window[-1]
    assert "DISABLE_AUTO_UPDATE=true" in pane_cmd
    assert "-lic" in pane_cmd
    assert "claude hi" in pane_cmd


def test_tmux_run_raises_when_new_window_fails(
    isolated_xdg: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A failing `new-window` (e.g. tmux's "index N in use") surfaces a
    GoblinError instead of silently leaving the agent unspawned."""
    from goblin_watcher.errors import GoblinError

    monkeypatch.setattr("goblin_watcher.windowing.tmux.shutil.which", lambda _: "/usr/bin/tmux")
    monkeypatch.setenv("TMUX", "")

    class _FakeRes:
        def __init__(self, code: int, stderr: str = "") -> None:
            self.returncode = code
            self.stdout = ""
            self.stderr = stderr

    def _fake_run(cmd, capture_output, text, check):
        sub = cmd[1] if len(cmd) > 1 else ""
        if sub == "new-window":
            return _FakeRes(1, stderr="create window failed: index 5 in use")
        # has-session / list-windows nonzero → fresh session, window absent.
        return _FakeRes(1 if sub in ("has-session", "list-windows") else 0)

    monkeypatch.setattr("goblin_watcher.windowing.tmux.subprocess.run", _fake_run)

    with pytest.raises(GoblinError, match="index 5 in use"):
        TmuxWindower().run(task=_task(tmp_path), cmd=["claude", "hi"], cwd=tmp_path, env={})


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


def test_tmux_switch_client_when_inside_other_session(
    isolated_xdg: Path, tmp_path: Path, fake_tmux
) -> None:
    """Inside tmux, the windower must `switch-client` so a client attached to
    a different session actually lands on the goblin window — `select-window`
    alone only changes the goblin session's active window."""
    with patch("goblin_watcher.windowing.tmux.os.environ", new={"TMUX": "/tmp/tmux-1,2,3"}):
        code = TmuxWindower().run(
            task=_task(tmp_path),
            cmd=["claude", "hi"],
            cwd=tmp_path,
            env={},
        )
    assert code == 0
    flat = [" ".join(c) for c in fake_tmux]
    assert any("select-window -t goblin:eng-123" in c for c in flat)
    assert any("switch-client -t goblin:eng-123" in c for c in flat)


def test_tmux_pane_command_injects_agent_env(isolated_xdg: Path, tmp_path: Path, fake_tmux) -> None:
    """`Agent.env()` extras must reach the pane via the `env` prefix — a tmux
    pane's shell can't inherit gw's process environment."""
    with patch("goblin_watcher.windowing.tmux.os.environ", new={"TMUX": "/tmp/tmux-1,2,3"}):
        TmuxWindower().run(
            task=_task(tmp_path),
            cmd=["claude", "hi"],
            cwd=tmp_path,
            env={"MY_AGENT_VAR": "a value"},
        )
    new_window = next(c for c in fake_tmux if "new-window" in c)
    pane_cmd = new_window[-1]
    assert "MY_AGENT_VAR=a value" in pane_cmd
    assert "DISABLE_AUTO_UPDATE=true" in pane_cmd


@pytest.fixture
def fake_tmux_with_window(monkeypatch: pytest.MonkeyPatch):
    """Fake tmux where the goblin session has a live `eng-123` window."""
    monkeypatch.setattr("goblin_watcher.windowing.tmux.shutil.which", lambda _: "/usr/bin/tmux")

    calls = []

    class _FakeRes:
        def __init__(self, code: int, stdout: str = "") -> None:
            self.returncode = code
            self.stdout = stdout
            self.stderr = ""

    def _fake_run(cmd, capture_output, text, check):
        calls.append(cmd)
        sub = cmd[1] if len(cmd) > 1 else ""
        if sub == "list-windows":
            return _FakeRes(0, stdout="intro\neng-123\n")
        return _FakeRes(0)

    monkeypatch.setattr("goblin_watcher.windowing.tmux.subprocess.run", _fake_run)
    return calls


def _fake_tmux_panes(monkeypatch: pytest.MonkeyPatch, panes: list[tuple[str, str]]):
    """Fake tmux whose `eng-123` window holds `panes` as (pane_id, @gw_session).

    An empty session id models a pane gw never tagged (spawned by hand, or by
    an older gw).
    """
    monkeypatch.setattr("goblin_watcher.windowing.tmux.shutil.which", lambda _: "/usr/bin/tmux")

    calls: list[list[str]] = []

    class _FakeRes:
        def __init__(self, code: int = 0, stdout: str = "") -> None:
            self.returncode = code
            self.stdout = stdout
            self.stderr = ""

    def _fake_run(cmd, capture_output, text, check):
        calls.append(cmd)
        sub = cmd[1] if len(cmd) > 1 else ""
        if sub == "list-panes":
            if "goblin:eng-123" not in cmd:
                return _FakeRes(1)
            return _FakeRes(0, stdout="".join(f"{pid}\t{sid}\n" for pid, sid in panes))
        return _FakeRes(0)

    monkeypatch.setattr("goblin_watcher.windowing.tmux.subprocess.run", _fake_run)
    return calls


def test_run_tags_pane_with_session_id(
    isolated_xdg: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The pane gw opens is stamped with the session id, so `send` can find it.

    tmux itself holds the mapping for the pane's lifetime — nothing is written
    to gw's state, which matters because the tmux path may `execvp` away before
    any post-launch write could happen.
    """
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
        sub = cmd[1] if len(cmd) > 1 else ""
        if sub == "list-windows":
            return _FakeRes(0, stdout="eng-123\n")
        if sub in ("split-window", "new-window"):
            # `-P -F '#{pane_id}'` makes tmux print the new pane's id.
            return _FakeRes(0, stdout="%7\n")
        return _FakeRes(0)

    monkeypatch.setattr("goblin_watcher.windowing.tmux.subprocess.run", _fake_run)

    TmuxWindower().run(
        task=_task(tmp_path),
        cmd=["claude", "hi"],
        cwd=tmp_path,
        env={},
        session_id="sess-abc",
    )
    flat = [" ".join(c) for c in calls]
    assert any("split-window -v -t goblin:eng-123" in c and "-P -F #{pane_id}" in c for c in flat)
    assert ["tmux", "set-option", "-p", "-t", "%7", "@gw_session", "sess-abc"] in calls


def test_run_without_session_id_skips_tagging(
    isolated_xdg: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls = _patch_inside_tmux_with_existing_window(monkeypatch)
    TmuxWindower().run(task=_task(tmp_path), cmd=["claude", "hi"], cwd=tmp_path, env={})
    assert not any("set-option" in c for c in [" ".join(x) for x in calls])


def test_send_types_text_then_enter(
    isolated_xdg: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Text goes literally (`-l --`), Enter as a key name — two separate calls."""
    calls = _fake_tmux_panes(monkeypatch, [("%3", "sess-abc")])

    where = TmuxWindower().send(task=_task(tmp_path), text="-also fix the tests")

    assert where == "goblin:eng-123 pane %3"
    assert ["tmux", "send-keys", "-t", "%3", "-l", "--", "-also fix the tests"] in calls
    assert ["tmux", "send-keys", "-t", "%3", "Enter"] in calls


def test_send_no_enter_leaves_text_unsubmitted(
    isolated_xdg: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls = _fake_tmux_panes(monkeypatch, [("%3", "sess-abc")])

    TmuxWindower().send(task=_task(tmp_path), text="draft", enter=False)

    assert ["tmux", "send-keys", "-t", "%3", "-l", "--", "draft"] in calls
    assert not any(c[-1] == "Enter" for c in calls)


def test_send_picks_the_pane_matching_the_session(
    isolated_xdg: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls = _fake_tmux_panes(monkeypatch, [("%3", "sess-abc"), ("%4", "sess-def")])

    TmuxWindower().send(task=_task(tmp_path), text="hi", session_id="sess-def")

    assert ["tmux", "send-keys", "-t", "%4", "-l", "--", "hi"] in calls


def test_send_prefers_the_newest_pane_for_a_resumed_session(
    isolated_xdg: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Resuming a session opens a second pane with the same tag; the live one is
    the newer, so the older (whose agent has exited) must not swallow input."""
    calls = _fake_tmux_panes(monkeypatch, [("%3", "sess-abc"), ("%9", "sess-abc")])

    TmuxWindower().send(task=_task(tmp_path), text="hi", session_id="sess-abc")

    assert ["tmux", "send-keys", "-t", "%9", "-l", "--", "hi"] in calls


def test_send_is_ambiguous_across_several_panes(
    isolated_xdg: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _fake_tmux_panes(monkeypatch, [("%3", "sess-abc"), ("%4", "sess-def")])

    with pytest.raises(GoblinError) as exc:
        TmuxWindower().send(task=_task(tmp_path), text="hi")
    assert "2 live panes" in exc.value.message
    assert exc.value.hint is not None
    assert "sess-abc" in exc.value.hint and "sess-def" in exc.value.hint


def test_send_falls_back_to_the_only_pane_when_untagged(
    isolated_xdg: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Panes from before gw tagged them still take input — there's only one target."""
    calls = _fake_tmux_panes(monkeypatch, [("%3", "")])

    TmuxWindower().send(task=_task(tmp_path), text="hi", session_id="sess-abc")

    assert ["tmux", "send-keys", "-t", "%3", "-l", "--", "hi"] in calls


def test_send_rejects_an_unmatched_session_among_many_panes(
    isolated_xdg: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _fake_tmux_panes(monkeypatch, [("%3", "sess-abc"), ("%4", "")])

    with pytest.raises(GoblinError) as exc:
        TmuxWindower().send(task=_task(tmp_path), text="hi", session_id="sess-zzz")
    assert "sess-zzz" in exc.value.message
    assert exc.value.hint is not None
    assert "%4 (untagged)" in exc.value.hint


def test_send_without_a_live_window_errors(
    isolated_xdg: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _fake_tmux_panes(monkeypatch, [])

    with pytest.raises(GoblinError) as exc:
        TmuxWindower().send(task=_task(tmp_path, "eng-999"), text="hi")
    assert "No live tmux pane" in exc.value.message


def test_send_surfaces_tmux_failure(
    isolated_xdg: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("goblin_watcher.windowing.tmux.shutil.which", lambda _: "/usr/bin/tmux")

    class _FakeRes:
        def __init__(self, code: int = 0, stdout: str = "", stderr: str = "") -> None:
            self.returncode = code
            self.stdout = stdout
            self.stderr = stderr

    def _fake_run(cmd, capture_output, text, check):
        sub = cmd[1] if len(cmd) > 1 else ""
        if sub == "list-panes":
            return _FakeRes(0, stdout="%3\tsess-abc\n")
        if sub == "send-keys":
            return _FakeRes(1, stderr="can't find pane %3")
        return _FakeRes(0)

    monkeypatch.setattr("goblin_watcher.windowing.tmux.subprocess.run", _fake_run)

    with pytest.raises(GoblinError, match="can't find pane"):
        TmuxWindower().send(task=_task(tmp_path), text="hi")


def test_rename_window_renames_live_window(isolated_xdg: Path, fake_tmux_with_window) -> None:
    assert TmuxWindower().rename_window("eng-123", "eng-456") is True
    assert ["tmux", "rename-window", "-t", "goblin:eng-123", "eng-456"] in fake_tmux_with_window


def test_rename_window_no_window_is_a_noop(isolated_xdg: Path, fake_tmux_with_window) -> None:
    assert TmuxWindower().rename_window("eng-999", "eng-456") is False
    assert not any("rename-window" in c for c in fake_tmux_with_window)


def test_rename_window_without_tmux_binary(
    isolated_xdg: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("goblin_watcher.windowing.tmux.shutil.which", lambda _: None)
    assert TmuxWindower().rename_window("eng-123", "eng-456") is False


def test_tmux_run_spills_an_over_budget_pane_command_to_a_script(
    isolated_xdg: Path, tmp_path: Path, fake_tmux, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A prompt too long for tmux's message cap rides in a file, not the argv.

    Before this, `gw new` on a ticket with a long description died on
    `new-window` with "command too long" and the agent was never spawned.
    """
    monkeypatch.setenv("TMUX", "tmux-1234,1,0")
    rc = TmuxWindower().run(
        task=_task(tmp_path),
        cmd=["claude", "--dangerously-skip-permissions", _LONG_PROMPT],
        cwd=tmp_path,
        env={},
        session_id="sess-abc",
    )

    assert rc == 0
    new_window = next(c for c in fake_tmux if "new-window" in c)
    assert _argv_size(new_window) < _TMUX_MESSAGE_CAP

    script = paths.pane_script_file("eng-123", "sess-abc")
    assert new_window[-1] == f"exec /bin/sh {shlex.quote(str(script))}"
    body = script.read_text()
    assert body.startswith("#!/bin/sh\n")
    # The script holds the command tmux would otherwise have carried, whole.
    assert "DISABLE_AUTO_UPDATE=true" in body
    assert "--dangerously-skip-permissions" in body
    assert len(body) > _TMUX_MESSAGE_CAP


def test_tmux_run_keeps_a_short_pane_command_inline(
    isolated_xdg: Path, tmp_path: Path, fake_tmux, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The ordinary case is unchanged: the command stays in tmux's argv, and no
    file is written. `#{pane_start_command}` remains readable."""
    monkeypatch.setenv("TMUX", "tmux-1234,1,0")
    TmuxWindower().run(
        task=_task(tmp_path),
        cmd=["claude", "hi"],
        cwd=tmp_path,
        env={},
        session_id="sess-abc",
    )

    new_window = next(c for c in fake_tmux if "new-window" in c)
    assert "claude hi" in new_window[-1]
    assert not paths.pane_scripts_dir().exists()


def test_tmux_run_spills_on_the_split_path_too(
    isolated_xdg: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The budget covers a second session on the same task, which spawns through
    the longer `split-window` form."""
    calls = _patch_inside_tmux_with_existing_window(monkeypatch)

    TmuxWindower().run(
        task=_task(tmp_path),
        cmd=["claude", _LONG_PROMPT],
        cwd=tmp_path,
        env={},
        session_id="sess-def",
    )

    split = next(c for c in calls if "split-window" in c)
    assert _argv_size(split) < _TMUX_MESSAGE_CAP
    assert paths.pane_script_file("eng-123", "sess-def").exists()


def test_pane_script_is_keyed_by_session_so_a_respawn_overwrites(
    isolated_xdg: Path, tmp_path: Path, fake_tmux, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Two spawns of the same session reuse one file; a different session gets its own."""
    monkeypatch.setenv("TMUX", "tmux-1234,1,0")
    for session_id in ("sess-abc", "sess-abc", "sess-def"):
        TmuxWindower().run(
            task=_task(tmp_path),
            cmd=["claude", _LONG_PROMPT],
            cwd=tmp_path,
            env={},
            session_id=session_id,
        )

    assert sorted(p.name for p in paths.pane_scripts_dir().glob("*.sh")) == [
        "eng-123-sess-abc.sh",
        "eng-123-sess-def.sh",
    ]
    # No temp files left behind by the atomic write.
    assert not list(paths.pane_scripts_dir().glob("*.tmp"))


def test_pane_scripts_are_pruned_once_past_their_ttl(isolated_xdg: Path) -> None:
    """Nothing live reads a pane script after startup, so old ones just go."""
    directory = paths.pane_scripts_dir()
    directory.mkdir(parents=True)
    fresh = directory / "eng-1-sess.sh"
    stale = directory / "eng-2-sess.sh"
    fresh.write_text("#!/bin/sh\n")
    stale.write_text("#!/bin/sh\n")
    old = time.time() - _SCRIPT_TTL_SECONDS - 60
    os.utime(stale, (old, old))

    _prune_pane_scripts(directory)

    assert fresh.exists()
    assert not stale.exists()


def test_send_chunks_text_too_long_for_one_tmux_message(
    isolated_xdg: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A single `send-keys` over tmux's cap fails outright, so long text is typed
    in pieces — in order, and reassembling to exactly the original."""
    calls = _fake_tmux_panes(monkeypatch, [("%3", "sess-abc")])
    text = "".join(chr(0x61 + i % 26) for i in range(20_000))

    TmuxWindower().send(task=_task(tmp_path), text=text, session_id="sess-abc")

    typed = [c for c in calls if c[1] == "send-keys" and "-l" in c]
    assert len(typed) > 1
    assert all(_argv_size(c) < _TMUX_MESSAGE_CAP for c in typed)
    assert "".join(c[-1] for c in typed) == text
    # Still exactly one Enter, after the last chunk.
    assert [c for c in calls if c[-1] == "Enter"] == [["tmux", "send-keys", "-t", "%3", "Enter"]]
