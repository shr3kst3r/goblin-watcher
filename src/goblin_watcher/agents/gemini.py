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

    def spawn_command(self, *, prompt: str, cwd: Path, unsafe: bool = False) -> list[str]:
        del cwd
        return [*self._prefix(unsafe), "-p", prompt]

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
