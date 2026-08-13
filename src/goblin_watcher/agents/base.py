from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Protocol, runtime_checkable


@dataclass
class RawSession:
    """A session discovered from an agent's on-disk session store."""

    session_id: str
    created_at: datetime
    transcript_path: Path
    first_message_snippet: str | None = None


@dataclass
class TranscriptSummary:
    """Derived from a transcript file: counts + the most-recent snippets.

    Short snippets (`last_user_snippet`, `last_assistant_snippet`) drive the
    cheap rolling summary and UI display — capped at ~120 chars. The
    `first_user_snippet` / `recent_*_snippets` lists carry longer (~400 char)
    excerpts intended for LLM-based description refresh: the goal at the
    start of the session plus a handful of the most recent exchanges,
    chronological (oldest first). Agents that can't easily parse their
    transcripts leave these empty.
    """

    turn_count: int = 0
    last_user_snippet: str | None = None
    last_assistant_snippet: str | None = None
    transcript_path: Path | None = None
    first_user_snippet: str | None = None
    recent_user_snippets: list[str] = field(default_factory=list)
    recent_assistant_snippets: list[str] = field(default_factory=list)
    extras: dict[str, str] = field(default_factory=dict)


@runtime_checkable
class Agent(Protocol):
    """One concrete agent (claude / codex / gemini / antigravity)."""

    name: str
    binary: str

    def spawn_command(
        self, *, prompt: str, cwd: Path, unsafe: bool = False, session_id: str | None = None
    ) -> list[str]:
        """Argv to start a fresh interactive session seeded with `prompt`.

        When `unsafe` is true, prepend the agent's bypass-permission flag.
        `session_id` (from `new_session_id`) preassigns the session's id;
        agents whose CLI can't accept one ignore it.
        """
        ...

    def headless_command(
        self, *, prompt: str, cwd: Path, unsafe: bool = False, session_id: str | None = None
    ) -> list[str]:
        """Argv to run `prompt` to completion non-interactively, then exit.

        Every registered CLI has such a mode — `claude -p`, `codex exec`,
        `agy -p`, `gemini -p`. It draws no TUI, reads no input, writes plain
        text to stdout and exits when the work is done, which is what
        `HeadlessWindower` needs to detach a run from any terminal.

        Same `unsafe` / `session_id` contract as `spawn_command`. Agents with
        no headless mode of their own raise `GoblinError`.
        """
        ...

    def new_session_id(self) -> str | None:
        """A fresh id to preassign to the next spawned session, or None.

        When non-None, the launcher passes it to `spawn_command` and records
        it as the session's id — no post-launch capture needed, which is the
        only reliable option for windowers (tmux) that detach before the
        agent writes its transcript. Agents whose CLI can't accept a
        caller-chosen id return None; the launcher then falls back to a
        synthetic placeholder reconciled via `capture_session_id`.
        """
        ...

    def resume_command(
        self, *, session_id: str | None, cwd: Path, unsafe: bool = False
    ) -> list[str]:
        """Argv to resume a session.

        If `session_id` is None, the implementation should use the agent's
        "continue most recent in this cwd" mode (e.g. `gemini --continue`).
        When `unsafe` is true, prepend the agent's bypass-permission flag.
        """
        ...

    def capture_session_id(self, cwd: Path) -> str | None:
        """Return the id of the session that was just created/resumed at `cwd`.

        Called right after the agent process exits. May return None if the agent
        doesn't surface a stable id; in that case the caller should synthesize
        one (e.g. ULID) so SessionRecord.session_id is always non-empty.
        """
        ...

    def list_sessions(self, cwd: Path) -> list[RawSession]:
        """Sessions for this `cwd` discovered from the agent's on-disk store.

        May return an empty list if discovery isn't possible (e.g. agents that
        don't persist sessions in a discoverable layout).
        """
        ...

    def read_transcript(self, session_id: str, cwd: Path) -> TranscriptSummary:
        """Parse the agent's transcript file for `session_id` into a summary."""
        ...

    def render_transcript(self, session_id: str, cwd: Path) -> str | None:
        """Render the full transcript as readable text for LLM consumption.

        One labeled block per message (`[user] ...`, `[assistant] ...`).
        Returns None when the agent can't parse its transcript on disk.
        Callers should expect arbitrary length and clamp at the
        consumption site.
        """
        ...

    def env(self) -> dict[str, str]:
        """Extra env vars to inject when spawning. Rarely needed."""
        ...
