"""Gemini agent (the `gemini` CLI).

Gemini uses cwd-scoped checkpoints, not stable session ids. We always resume
via `--continue`, and synthesize a ULID for SessionRecord storage.
"""

from __future__ import annotations

from pathlib import Path

from goblin_watcher.agents.base import RawSession, TranscriptSummary


class GeminiAgent:
    name = "gemini"
    binary = "gemini"
    unsafe_flags: tuple[str, ...] = ("--yolo",)

    def _prefix(self, unsafe: bool) -> list[str]:
        return [self.binary, *self.unsafe_flags] if unsafe else [self.binary]

    def spawn_command(
        self, *, prompt: str, cwd: Path, unsafe: bool = False, session_id: str | None = None
    ) -> list[str]:
        del cwd, session_id
        return [*self._prefix(unsafe), "-p", prompt]

    def headless_command(
        self, *, prompt: str, cwd: Path, unsafe: bool = False, session_id: str | None = None
    ) -> list[str]:
        # `gemini -p` *is* the non-interactive mode, and it's what the spawn
        # path already uses — so headless and interactive coincide here. The
        # difference is only in where the windower puts the process.
        return self.spawn_command(prompt=prompt, cwd=cwd, unsafe=unsafe, session_id=session_id)

    def new_session_id(self) -> str | None:
        # Gemini has no stable session ids at all; the launcher synthesizes.
        return None

    def resume_command(
        self, *, session_id: str | None, cwd: Path, unsafe: bool = False
    ) -> list[str]:
        del session_id, cwd
        return [*self._prefix(unsafe), "--continue"]

    def env(self) -> dict[str, str]:
        return {}

    def capture_session_id(self, cwd: Path) -> str | None:
        del cwd
        return None

    def list_sessions(self, cwd: Path) -> list[RawSession]:
        del cwd
        return []

    def read_transcript(self, session_id: str, cwd: Path) -> TranscriptSummary:
        del session_id, cwd
        return TranscriptSummary()

    def render_transcript(self, session_id: str, cwd: Path) -> str | None:
        del session_id, cwd
        return None
