"""Notification transports for background sync.

Notifications are *edge-triggered* by the engine: it compares the current signal
against the last value it recorded and fires only on change, so a quiet day
produces zero notifications and a merged PR is announced exactly once.

Every notification is journaled regardless of transport, so `gw sync watch` and
`gw sync status` still show what happened when delivery is off or fails.
"""

from __future__ import annotations

import shutil
import subprocess
import sys
from typing import Protocol

from goblin_watcher.config import SyncConfig


class Notifier(Protocol):
    name: str

    def send(self, title: str, body: str) -> bool:
        """Deliver a notification. Returns True on success. Never raises."""
        ...


class NullNotifier:
    name = "off"

    def send(self, title: str, body: str) -> bool:
        return False


class MacosNotifier:
    """`osascript -e 'display notification ...'` — no extra dependency."""

    name = "macos"

    def send(self, title: str, body: str) -> bool:
        if shutil.which("osascript") is None:
            return False
        script = (
            f"display notification {_as_applescript_string(body)} "
            f"with title {_as_applescript_string(title)}"
        )
        try:
            proc = subprocess.run(
                ["osascript", "-e", script],
                capture_output=True,
                timeout=10,
                check=False,
            )
        except (OSError, subprocess.SubprocessError):
            return False
        return proc.returncode == 0


class CommandNotifier:
    """User-supplied argv with title and body appended as the last two args.

    Never shell-interpolated — the configured list is passed to `subprocess.run`
    verbatim, so a webhook CLI (e.g. `slack-me`) can be wired in without gw
    knowing anything about it.
    """

    name = "command"

    def __init__(self, argv: list[str]) -> None:
        self._argv = list(argv)

    def send(self, title: str, body: str) -> bool:
        if not self._argv:
            return False
        try:
            proc = subprocess.run(
                [*self._argv, title, body],
                capture_output=True,
                timeout=30,
                check=False,
            )
        except (OSError, subprocess.SubprocessError):
            return False
        return proc.returncode == 0


def _as_applescript_string(value: str) -> str:
    escaped = value.replace("\\", "\\\\").replace('"', '\\"')
    return f'"{escaped}"'


def resolve(cfg: SyncConfig, platform: str | None = None) -> Notifier:
    """Pick a transport from config. "auto" means macOS notifications on darwin."""
    transport = cfg.notify
    if transport == "auto":
        transport = "macos" if (platform or sys.platform) == "darwin" else "off"
    if transport == "macos":
        return MacosNotifier()
    if transport == "command":
        return CommandNotifier(cfg.notify_command)
    return NullNotifier()
