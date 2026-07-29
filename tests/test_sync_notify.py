"""Notification transports. Never invokes osascript or a real command."""

from __future__ import annotations

import subprocess
from unittest.mock import patch

from goblin_watcher.config import SyncConfig
from goblin_watcher.sync import notify


def test_resolve_auto_is_macos_on_darwin() -> None:
    assert notify.resolve(SyncConfig(notify="auto"), platform="darwin").name == "macos"


def test_resolve_auto_is_off_elsewhere() -> None:
    assert notify.resolve(SyncConfig(notify="auto"), platform="linux").name == "off"


def test_resolve_explicit_off_and_command() -> None:
    assert notify.resolve(SyncConfig(notify="off"), platform="darwin").name == "off"
    assert (
        notify.resolve(SyncConfig(notify="command", notify_command=["say"]), platform="linux").name
        == "command"
    )


def test_null_notifier_never_sends() -> None:
    assert notify.NullNotifier().send("t", "b") is False


def test_command_notifier_appends_title_and_body() -> None:
    with (
        patch("goblin_watcher.sync.notify.subprocess.run") as run,
        patch("goblin_watcher.sync.notify.shutil.which", return_value="/usr/bin/slack-me"),
    ):
        run.return_value = subprocess.CompletedProcess(args=[], returncode=0)
        ok = notify.CommandNotifier(["slack-me", "--channel", "dev"]).send("Title", "Body")

    assert ok is True
    argv = run.call_args[0][0]
    assert argv == ["slack-me", "--channel", "dev", "Title", "Body"]


def test_command_notifier_with_empty_argv_is_a_noop() -> None:
    with patch("goblin_watcher.sync.notify.subprocess.run") as run:
        assert notify.CommandNotifier([]).send("t", "b") is False
    run.assert_not_called()


def test_command_notifier_reports_failure_on_nonzero_exit() -> None:
    with patch("goblin_watcher.sync.notify.subprocess.run") as run:
        run.return_value = subprocess.CompletedProcess(args=[], returncode=3)
        assert notify.CommandNotifier(["x"]).send("t", "b") is False


def test_command_notifier_swallows_oserror() -> None:
    with patch("goblin_watcher.sync.notify.subprocess.run", side_effect=OSError("boom")):
        assert notify.CommandNotifier(["x"]).send("t", "b") is False


def test_macos_notifier_builds_escaped_applescript() -> None:
    with (
        patch("goblin_watcher.sync.notify.shutil.which", return_value="/usr/bin/osascript"),
        patch("goblin_watcher.sync.notify.subprocess.run") as run,
    ):
        run.return_value = subprocess.CompletedProcess(args=[], returncode=0)
        ok = notify.MacosNotifier().send('He said "hi"', "back\\slash")

    assert ok is True
    argv = run.call_args[0][0]
    assert argv[0] == "osascript"
    assert argv[1] == "-e"
    # Quotes and backslashes in user data must not break out of the string.
    assert '\\"hi\\"' in argv[2]
    assert "back\\\\slash" in argv[2]


def test_macos_notifier_absent_binary_is_a_noop() -> None:
    with patch("goblin_watcher.sync.notify.shutil.which", return_value=None):
        assert notify.MacosNotifier().send("t", "b") is False
