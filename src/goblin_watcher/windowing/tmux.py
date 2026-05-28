"""Tmux windower: places agent processes inside a long-running tmux session.

Layout:
  - One tmux session named `goblin` (configurable).
  - One window per task, named after `task.id` (e.g. `eng-123`).
  - One pane per agent session. Second+ sessions on the same task `split-window`
    in the orientation set by `tmux.split` ("vertical" → top/bottom, the default;
    "horizontal" → side-by-side).

We send the command via `send-keys` rather than passing it to `new-window` so
the shell environment is the same as a manual `tmux new-window`.
"""

from __future__ import annotations

import os
import shlex
import shutil
import subprocess
from pathlib import Path

from goblin_watcher import config
from goblin_watcher.console import console
from goblin_watcher.errors import MissingDependencyError
from goblin_watcher.models import Task


def _ensure_tmux() -> str:
    found = shutil.which("tmux")
    if not found:
        raise MissingDependencyError(
            "`tmux` is not on PATH.",
            hint="Install tmux, or switch to windowing = 'inline'.",
        )
    return found


def _run_tmux(*args: str) -> subprocess.CompletedProcess[str]:
    _ensure_tmux()
    return subprocess.run(
        ["tmux", *args],
        capture_output=True,
        text=True,
        check=False,
    )


class TmuxWindower:
    name = "tmux"

    def _session(self) -> str:
        return config.load().tmux.session_name

    def _attach_on_spawn(self) -> bool:
        return config.load().tmux.attach_on_spawn

    def _ensure_session(self) -> None:
        s = self._session()
        res = _run_tmux("has-session", "-t", s)
        if res.returncode != 0:
            _run_tmux("new-session", "-d", "-s", s, "-n", "intro")
        # Clear any stale `alert-silence` hook left over from the old
        # `bell_on_idle` code path. That hook ran `printf \a > /dev/tty`,
        # which fails inside tmux's run-shell context and surfaces an ugly
        # banner across the session. Idempotent — unsetting an absent hook
        # is a no-op.
        _run_tmux("set-hook", "-u", "-t", s, "alert-silence")

    def _window_exists(self, task_id: str) -> bool:
        s = self._session()
        res = _run_tmux("list-windows", "-t", s, "-F", "#W")
        if res.returncode != 0:
            return False
        return any(line.strip() == task_id for line in res.stdout.splitlines())

    def run(
        self,
        *,
        task: Task,
        cmd: list[str],
        cwd: Path,
        env: dict[str, str],
    ) -> int:
        _ensure_tmux()
        self._ensure_session()
        s = self._session()
        target = f"{s}:{task.id}"
        if self._window_exists(task.id):
            # Add a pane to the existing window for this additional session.
            # `-v` stacks top/bottom, `-h` places side-by-side. `vertical` ==
            # panes-stacked-vertically matches tmux's `-v` flag letter.
            split_flag = "-h" if config.load().tmux.split == "horizontal" else "-v"
            _run_tmux("split-window", split_flag, "-t", target, "-c", str(cwd))
        else:
            _run_tmux("new-window", "-t", s, "-n", task.id, "-c", str(cwd))

        shell_cmd = " ".join(shlex.quote(arg) for arg in cmd)
        # Send the command into the pane (last pane of the target window).
        _run_tmux("send-keys", "-t", target, shell_cmd, "Enter")

        cfg = config.load()
        if cfg.tmux.mark_idle:
            # `monitor-silence N` flags the window with a `~` in the status
            # bar when its pane sees no output for N seconds. No hook — the
            # visual marker is enough and never steals focus.
            _run_tmux(
                "set-window-option",
                "-t",
                target,
                "monitor-silence",
                str(cfg.tmux.mark_idle_seconds),
            )

        # Attach behavior depends on where gw was invoked from.
        if self._attach_on_spawn():
            if os.environ.get("TMUX"):
                # Already inside tmux — switch to the right window if same server.
                _run_tmux("select-window", "-t", target)
            else:
                console.print(
                    f"[muted]Agent launched in tmux window {target}. "
                    f"Attaching: `tmux attach -t {s}`...[/]"
                )
                # Replace this process with the attach (best UX).
                tmux = _ensure_tmux()
                os.execvp(tmux, [tmux, "attach", "-t", s])

        del env  # env applies to the new tmux pane's shell, not via tmux itself
        return 0

    def is_live(self, task: Task) -> bool:
        s = self._session()
        res = _run_tmux("list-windows", "-t", s, "-F", "#W")
        if res.returncode != 0:
            return False
        return any(line.strip() == task.id for line in res.stdout.splitlines())
