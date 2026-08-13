from __future__ import annotations

from pathlib import Path
from typing import Protocol, runtime_checkable

from goblin_watcher.models import Task


@runtime_checkable
class Windower(Protocol):
    name: str
    # True when `run` returns while the agent is still running, rather than
    # blocking until it exits. The launcher skips post-run reconciliation for
    # these: the transcript the agent is about to write doesn't exist yet, so
    # `capture_session_id` would race it (or pick up an older session).
    detaches: bool
    # True when the windower hosts no terminal at all, so the agent has to be
    # launched in its non-interactive print/exec mode
    # (`Agent.headless_command`) rather than its interactive one.
    headless: bool

    def run(
        self,
        *,
        task: Task,
        cmd: list[str],
        cwd: Path,
        env: dict[str, str],
        session_id: str | None = None,
    ) -> int:
        """Run `cmd` for `task` in `cwd`. Returns process exit code.

        `env` carries only the agent's *extra* variables (`Agent.env()`), not
        a full environment. Each windower decides how to deliver them: inline
        merges them over `os.environ`; tmux injects them into the pane command
        (a tmux pane can't inherit this process's environment).

        `session_id` is the `SessionRecord.session_id` this launch belongs to.
        Windowers that host many sessions side by side (tmux) label the
        window/pane with it so `send` can find its way back; the ones that
        don't ignore it.
        """
        ...

    def is_live(self, task: Task) -> bool:
        """True if a runtime window/pane currently hosts this task."""
        ...

    def send(
        self,
        *,
        task: Task,
        text: str,
        session_id: str | None = None,
        enter: bool = True,
    ) -> str:
        """Type `text` into the live agent hosting `task`, as if at its keyboard.

        `session_id` selects among several live sessions on the same task;
        `None` means "the only one there is". `enter` submits the text.

        Returns a human-readable description of where the text landed. Raises
        `GoblinError` when there is nothing to send to — no live window, or an
        ambiguous choice of them — and for windowers with no addressable input
        at all (inline).
        """
        ...
