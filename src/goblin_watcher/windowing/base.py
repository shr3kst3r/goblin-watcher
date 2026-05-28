from __future__ import annotations

from pathlib import Path
from typing import Protocol, runtime_checkable

from goblin_watcher.models import Task


@runtime_checkable
class Windower(Protocol):
    name: str

    def run(
        self,
        *,
        task: Task,
        cmd: list[str],
        cwd: Path,
        env: dict[str, str],
    ) -> int:
        """Run `cmd` for `task` in `cwd`. Returns process exit code."""
        ...

    def is_live(self, task: Task) -> bool:
        """True if a runtime window/pane currently hosts this task."""
        ...
