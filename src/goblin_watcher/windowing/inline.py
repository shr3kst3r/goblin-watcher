from __future__ import annotations

import os
import subprocess
from pathlib import Path

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
    ) -> int:
        del task
        # Stdin/stdout/stderr pass through — this is an interactive agent.
        # `env` is the agent's extras only; merge over the full environment.
        proc = subprocess.run(cmd, cwd=str(cwd), env={**os.environ, **env}, check=False)
        return proc.returncode

    def is_live(self, task: Task) -> bool:
        del task
        return False
