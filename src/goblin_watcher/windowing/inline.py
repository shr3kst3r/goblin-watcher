from __future__ import annotations

import os
import subprocess
from pathlib import Path

from goblin_watcher.errors import GoblinError
from goblin_watcher.models import Task


class InlineWindower:
    name = "inline"

    def run(
        self,
        *,
        task: Task,
        cmd: list[str],
        cwd: Path,
        env: dict[str, str],
        session_id: str | None = None,
    ) -> int:
        del task, session_id
        # Stdin/stdout/stderr pass through — this is an interactive agent.
        # `env` is the agent's extras only; merge over the full environment.
        proc = subprocess.run(cmd, cwd=str(cwd), env={**os.environ, **env}, check=False)
        return proc.returncode

    def is_live(self, task: Task) -> bool:
        del task
        return False

    def send(
        self,
        *,
        task: Task,
        text: str,
        session_id: str | None = None,
        enter: bool = True,
    ) -> str:
        """Always raises: an inline agent owns the terminal `gw` was run from.

        There is no pane to address — the agent's stdin *is* the user's
        keyboard, and the `gw` process that launched it exited long ago.
        """
        del text, session_id, enter
        raise GoblinError(
            f"Inline windowing has no pane to send to (task {task.id!r}).",
            hint="Type into the agent's own terminal, or spawn agents in tmux "
            "(`gw config set defaults.windowing tmux`) so sessions become addressable.",
        )
