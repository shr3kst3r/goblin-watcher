from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Literal, Protocol, runtime_checkable

from goblin_watcher.models import UsageBucket

# Who spoke last in a transcript. `None` when the tail held no message at all.
LastRole = Literal["user", "assistant"]


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

    `usage` carries per-(model, day) token counts read off the same pass, so
    cost accounting costs nothing beyond the walk the summary already does.
    """

    turn_count: int = 0
    last_user_snippet: str | None = None
    last_assistant_snippet: str | None = None
    transcript_path: Path | None = None
    first_user_snippet: str | None = None
    recent_user_snippets: list[str] = field(default_factory=list)
    recent_assistant_snippets: list[str] = field(default_factory=list)
    extras: dict[str, str] = field(default_factory=dict)
    usage: list[UsageBucket] = field(default_factory=list)


@dataclass(frozen=True)
class TranscriptCapability:
    """Whether `gw` can parse this agent's transcripts, and why not if it can't.

    Declared per agent so the degradation is visible instead of silent. When
    transcripts can't be parsed, `read_transcript` returns an empty
    `TranscriptSummary` and `render_transcript` returns None, which quietly
    empties out half the product: rolling summaries, LLM-refreshed
    descriptions, turn counts, and the transcript-derived activity states
    (ADR 0010) — such an agent can only ever report `working` or `idle` off
    file mtime, never `needs-you` or `done`.

    `reason` is a short lowercase fragment naming the obstacle (e.g. "the CLI
    keeps conversations in an internal SQLite store"); `gw doctor` composes it
    into the user-facing warning, so the consequence wording lives in one
    place. It is required when `parseable` is False.
    """

    parseable: bool
    reason: str = ""

    def __post_init__(self) -> None:
        if not self.parseable and not self.reason:
            raise ValueError("TranscriptCapability(parseable=False) needs a reason")


PARSEABLE_TRANSCRIPTS = TranscriptCapability(parseable=True)


@dataclass(frozen=True)
class TranscriptTail:
    """What the *end* of a transcript says about the agent's state.

    Deliberately shape, not meaning: whether a tool call is still outstanding,
    who spoke last, and the text of the final assistant turn. Naming a state
    from that is `activity.classify`'s job — the question-detection heuristic is
    agent-independent and has no business being written five times.

    * `pending_tool` — the agent asked for a tool and no result has come back,
      so it is mid-call right now.
    * `last_role` — `"user"` means the turn was handed to the agent and it
      hasn't answered yet; `"assistant"` means it finished speaking.
    * `last_assistant` — the tail of the final assistant message, trimmed from
      the front (see `_tail.tail_text`), because how a turn *ends* is what says
      whether it ended on a question.
    """

    pending_tool: bool = False
    last_role: LastRole | None = None
    last_assistant: str | None = None


@runtime_checkable
class Agent(Protocol):
    """One concrete agent (claude / codex / gemini / antigravity)."""

    name: str
    binary: str
    transcripts: TranscriptCapability

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

    def read_tail(self, transcript_path: Path) -> TranscriptTail | None:
        """Shape of the last few records of `transcript_path`, for classification.

        Takes a path rather than `(session_id, cwd)` on purpose: this is called
        on every `gw status` render and every `--watch` tick, and the path is
        already on the `SessionRecord`. Going through `read_transcript`'s
        lookup instead would re-glob the agent's whole session store each time
        (codex walks `~/.codex/sessions` recursively).

        Implementations read a bounded window from the *end* of the file
        (`agents._tail`) so cost doesn't scale with transcript size. Return None
        when this agent's transcripts can't be parsed, or the window held
        nothing usable — callers then fall back to the mtime heuristic.
        """
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
