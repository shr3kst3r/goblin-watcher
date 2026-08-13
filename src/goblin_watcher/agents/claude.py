"""Claude Code agent (the `claude` CLI).

Session layout (as of 2026-05): JSONL files at
`~/.claude/projects/<encoded-cwd>/<session-id>.jsonl`. The path encoding
replaces `/` with `-` and prefixes with `-` (e.g. `/tmp/foo` becomes
`-tmp-foo`).
"""

from __future__ import annotations

import json
import re
import uuid
from datetime import UTC, datetime
from pathlib import Path

from goblin_watcher.agents._usage import BucketAccumulator, as_int, local_day
from goblin_watcher.agents.base import RawSession, TranscriptSummary

# Claude Code's cwd encoding replaces any character that isn't an ASCII letter,
# digit, or dash with a literal `-`. That's stricter than just slashes — dots
# and underscores are converted too. Mirror that exactly or we'll miss the
# transcript directory on disk.
_CWD_NONSAFE = re.compile(r"[^A-Za-z0-9-]")


class ClaudeAgent:
    name = "claude"
    binary = "claude"
    unsafe_flags: tuple[str, ...] = ("--dangerously-skip-permissions",)

    def _prefix(self, unsafe: bool) -> list[str]:
        return [self.binary, *self.unsafe_flags] if unsafe else [self.binary]

    def spawn_command(
        self, *, prompt: str, cwd: Path, unsafe: bool = False, session_id: str | None = None
    ) -> list[str]:
        del cwd
        cmd = self._prefix(unsafe)
        if session_id:
            cmd += ["--session-id", session_id]
        return [*cmd, prompt]

    def new_session_id(self) -> str | None:
        # `claude --session-id` requires a UUID and names the transcript file
        # after it, so the id we record up-front is the id on disk.
        return str(uuid.uuid4())

    def resume_command(
        self, *, session_id: str | None, cwd: Path, unsafe: bool = False
    ) -> list[str]:
        del cwd
        if session_id:
            return [*self._prefix(unsafe), "--resume", session_id]
        return [*self._prefix(unsafe), "--continue"]

    def env(self) -> dict[str, str]:
        return {}

    # ----- session discovery / transcript parsing ------------------------------

    @staticmethod
    def _encode_cwd(cwd: Path) -> str:
        return "-" + _CWD_NONSAFE.sub("-", str(cwd.resolve()).strip("/"))

    @staticmethod
    def projects_root() -> Path:
        return Path.home() / ".claude" / "projects"

    def _project_dir(self, cwd: Path) -> Path:
        return self.projects_root() / self._encode_cwd(cwd)

    def capture_session_id(self, cwd: Path) -> str | None:
        d = self._project_dir(cwd)
        if not d.exists():
            return None
        # Newest jsonl file wins.
        candidates = sorted(d.glob("*.jsonl"), key=lambda p: p.stat().st_mtime, reverse=True)
        return candidates[0].stem if candidates else None

    def list_sessions(self, cwd: Path) -> list[RawSession]:
        d = self._project_dir(cwd)
        if not d.exists():
            return []
        sessions: list[RawSession] = []
        for p in sorted(d.glob("*.jsonl"), key=lambda f: f.stat().st_mtime, reverse=True):
            stat = p.stat()
            created = datetime.fromtimestamp(stat.st_mtime, tz=UTC)
            sessions.append(
                RawSession(
                    session_id=p.stem,
                    created_at=created,
                    transcript_path=p,
                    first_message_snippet=_first_user_message_snippet(p),
                )
            )
        return sessions

    def read_transcript(self, session_id: str, cwd: Path) -> TranscriptSummary:
        path = self._project_dir(cwd) / f"{session_id}.jsonl"
        return _parse_transcript(path)

    def render_transcript(self, session_id: str, cwd: Path) -> str | None:
        path = self._project_dir(cwd) / f"{session_id}.jsonl"
        return _render_transcript(path)


def _iter_messages(path: Path):
    if not path.exists():
        return
    try:
        with path.open("r") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    yield json.loads(line)
                except json.JSONDecodeError:
                    continue
    except OSError:
        return


def _first_user_message_snippet(path: Path) -> str | None:
    for msg in _iter_messages(path):
        role = (msg.get("type") or msg.get("role") or "").lower()
        if role in {"user", "human"}:
            return _coerce_text(msg)
    return None


def _is_real_user_turn(msg: dict) -> bool:
    """True for a human-typed user message; False for the bookkeeping records
    claude-code also stores with `type: "user"` (tool results, meta/system
    injections). Counting those inflates `turn_count` badly on tool-heavy
    sessions.
    """
    if msg.get("isMeta"):
        return False
    inner = msg.get("message") or msg
    content = inner.get("content")
    if isinstance(content, list):
        blocks = [b for b in content if isinstance(b, dict)]
        if blocks and all(b.get("type") == "tool_result" for b in blocks):
            return False
    return True


def _coerce_text(msg: dict) -> str | None:
    # claude-code's JSONL stores `message: {role, content: [...]}` with content blocks.
    inner = msg.get("message") or msg
    content = inner.get("content")
    if isinstance(content, str):
        return _truncate(content)
    if isinstance(content, list):
        for block in content:
            if isinstance(block, dict) and block.get("type") == "text":
                text = block.get("text") or ""
                if text:
                    return _truncate(text)
    if isinstance(inner.get("text"), str):
        return _truncate(inner["text"])
    return None


def _truncate(text: str, max_len: int = 120) -> str:
    text = " ".join(text.split())
    return text[:max_len] + ("…" if len(text) > max_len else "")


_LONG_SNIPPET_LEN = 400
_RECENT_KEEP = 3


def _parse_transcript(path: Path) -> TranscriptSummary:
    """Parse a claude jsonl into a TranscriptSummary.

    Walks every message once and keeps:
      * `last_user_snippet` / `last_assistant_snippet`: short (~120 char)
        snippets used by the cheap summary path and UI display.
      * `first_user_snippet` + the trailing `_RECENT_KEEP` user/assistant
        messages at ~400 char each: longer excerpts the LLM description
        path uses to characterize the session.
      * `usage`: per-(model, day) token counts, deduplicated by message id.
    """
    summary = TranscriptSummary(transcript_path=path)
    last_user: str | None = None
    last_assistant: str | None = None
    first_user_long: str | None = None
    recent_users: list[str] = []
    recent_assistants: list[str] = []
    usage = BucketAccumulator()
    counted_messages: set[str] = set()
    for msg in _iter_messages(path):
        _accumulate_usage(msg, usage, counted_messages)
        role = (msg.get("type") or msg.get("role") or "").lower()
        if role in {"user", "human"}:
            if not _is_real_user_turn(msg):
                continue
            summary.turn_count += 1
            text = _coerce_text(msg)
            long_text = _coerce_text_long(msg)
            if text:
                last_user = text
            if long_text:
                if first_user_long is None:
                    first_user_long = long_text
                recent_users.append(long_text)
                if len(recent_users) > _RECENT_KEEP:
                    recent_users.pop(0)
        elif role in {"assistant", "ai"}:
            text = _coerce_text(msg)
            long_text = _coerce_text_long(msg)
            if text:
                last_assistant = text
            if long_text:
                recent_assistants.append(long_text)
                if len(recent_assistants) > _RECENT_KEEP:
                    recent_assistants.pop(0)
    summary.last_user_snippet = last_user
    summary.last_assistant_snippet = last_assistant
    summary.first_user_snippet = first_user_long
    summary.recent_user_snippets = recent_users
    summary.recent_assistant_snippets = recent_assistants
    summary.usage = usage.buckets()
    return summary


def _accumulate_usage(msg: dict, usage: BucketAccumulator, counted_messages: set[str]) -> None:
    """Fold one record's `message.usage` into `usage`, skipping duplicates.

    A single assistant turn is written to the JSONL once per content block, and
    every one of those records repeats the *same* cumulative `usage` object.
    Summing them naively double- or triple-counts a session (measured 2.1x on
    real transcripts), so dedupe on `message.id` — the API's id for the one
    request those blocks came from.
    """
    inner = msg.get("message")
    if not isinstance(inner, dict):
        return
    raw = inner.get("usage")
    if not isinstance(raw, dict):
        return
    message_id = inner.get("id") or msg.get("requestId")
    if isinstance(message_id, str) and message_id:
        if message_id in counted_messages:
            return
        counted_messages.add(message_id)
    model = inner.get("model")
    usage.add(
        model=model if isinstance(model, str) and model else None,
        day=local_day(msg.get("timestamp")),
        input_tokens=as_int(raw.get("input_tokens")),
        output_tokens=as_int(raw.get("output_tokens")),
        cache_read_tokens=as_int(raw.get("cache_read_input_tokens")),
        **_cache_writes(raw),
    )


def _cache_writes(raw: dict) -> dict[str, int]:
    """Split cache-creation tokens by TTL — they're billed at different rates.

    Newer transcripts carry a `cache_creation` breakdown; older ones only have
    the `cache_creation_input_tokens` total, which we attribute to the 5-minute
    cache (the API default).
    """
    total = as_int(raw.get("cache_creation_input_tokens"))
    breakdown = raw.get("cache_creation")
    if isinstance(breakdown, dict):
        five_min = as_int(breakdown.get("ephemeral_5m_input_tokens"))
        one_hour = as_int(breakdown.get("ephemeral_1h_input_tokens"))
        if five_min or one_hour:
            return {"cache_write_tokens": five_min, "cache_write_1h_tokens": one_hour}
    return {"cache_write_tokens": total, "cache_write_1h_tokens": 0}


def _coerce_text_long(msg: dict) -> str | None:
    """Like `_coerce_text` but with a ~400-char window for LLM-prompt use."""
    inner = msg.get("message") or msg
    content = inner.get("content")
    if isinstance(content, str):
        return _truncate(content, max_len=_LONG_SNIPPET_LEN)
    if isinstance(content, list):
        for block in content:
            if isinstance(block, dict) and block.get("type") == "text":
                text = block.get("text") or ""
                if text:
                    return _truncate(text, max_len=_LONG_SNIPPET_LEN)
    if isinstance(inner.get("text"), str):
        return _truncate(inner["text"], max_len=_LONG_SNIPPET_LEN)
    return None


# Cap any single message at this length when rendering the full transcript.
# Code-paste messages can be enormous; without a cap one runaway block could
# blow the prompt budget. The description.py side then applies a whole-
# transcript cap on top of this.
_FULL_MESSAGE_CAP = 8000


def _extract_full_text(msg: dict) -> str | None:
    """Return the message text without aggressive whitespace flattening."""
    inner = msg.get("message") or msg
    content = inner.get("content")
    text: str | None = None
    if isinstance(content, str):
        text = content
    elif isinstance(content, list):
        # Concatenate all text blocks in order, preserving paragraph structure.
        chunks = [
            block.get("text") or ""
            for block in content
            if isinstance(block, dict) and block.get("type") == "text"
        ]
        joined = "\n".join(c for c in chunks if c)
        if joined:
            text = joined
    elif isinstance(inner.get("text"), str):
        text = inner["text"]
    if not text:
        return None
    if len(text) > _FULL_MESSAGE_CAP:
        text = text[: _FULL_MESSAGE_CAP - 1] + "…"
    return text


def _render_transcript(path: Path) -> str | None:
    """Render every message in the transcript as labeled blocks.

    Returns None when there's no transcript on disk. Skips tool-call / system
    messages — we want user + assistant prose for description purposes.
    """
    if not path.exists():
        return None
    parts: list[str] = []
    for msg in _iter_messages(path):
        role = (msg.get("type") or msg.get("role") or "").lower()
        if role in {"user", "human"}:
            label = "user"
        elif role in {"assistant", "ai"}:
            label = "assistant"
        else:
            continue
        text = _extract_full_text(msg)
        if not text:
            continue
        parts.append(f"[{label}]\n{text}")
    if not parts:
        return None
    return "\n\n".join(parts)
