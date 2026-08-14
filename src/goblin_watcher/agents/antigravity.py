"""Google Antigravity agent (the `agy` CLI).

Antigravity CLI installs as `agy` (`~/.local/bin/agy` on macOS/Linux) and
seeds a fresh interactive session with `agy --prompt-interactive "<prompt>"`
— `-p` / `--print` is its *headless* mode, which would print and exit rather
than hand the user a TUI.

Transcripts are stored locally as JSONL files under:
`~/.gemini/antigravity-cli/brain/<conversation-id>/.system_generated/logs/transcript.jsonl`.
The CLI records the most recent conversation id for each workspace in
`~/.gemini/antigravity-cli/cache/last_conversations.json`, a JSON map of
absolute workspace path → conversation id.
"""

from __future__ import annotations

import json
import re
from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path

from goblin_watcher.agents._tail import tail_records, tail_text
from goblin_watcher.agents._usage import BucketAccumulator, as_int, local_day
from goblin_watcher.agents.base import (
    PARSEABLE_TRANSCRIPTS,
    LastRole,
    RawSession,
    TranscriptCapability,
    TranscriptSummary,
    TranscriptTail,
)

# Conversation ids are UUIDs. The launcher synthesizes a 24-char hex placeholder
# for agents that can't preassign an id (see `agents/launcher.py`), and in tmux
# mode that placeholder is never reconciled — passing it to `--conversation`
# would just earn a "conversation not found" warning. Shape-check before use.
_CONVERSATION_ID = re.compile(
    r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$"
)

_SHORT_SNIPPET_LEN = 120
_LONG_SNIPPET_LEN = 400
_RECENT_KEEP = 3
_FULL_MESSAGE_CAP = 8000


class AntigravityAgent:
    name = "antigravity"
    binary = "agy"
    unsafe_flags: tuple[str, ...] = ("--dangerously-skip-permissions",)
    transcripts: TranscriptCapability = PARSEABLE_TRANSCRIPTS

    def _prefix(self, unsafe: bool) -> list[str]:
        return [self.binary, *self.unsafe_flags] if unsafe else [self.binary]

    def spawn_command(
        self, *, prompt: str, cwd: Path, unsafe: bool = False, session_id: str | None = None
    ) -> list[str]:
        del cwd, session_id
        return [*self._prefix(unsafe), "--prompt-interactive", prompt]

    def headless_command(
        self, *, prompt: str, cwd: Path, unsafe: bool = False, session_id: str | None = None
    ) -> list[str]:
        # `-p` / `--print` is the headless counterpart to the
        # `--prompt-interactive` used for interactive spawns: it prints and
        # exits instead of handing over a TUI.
        del cwd, session_id
        return [*self._prefix(unsafe), "-p", prompt]

    def new_session_id(self) -> str | None:
        # `agy` has no flag to preassign a conversation id; the backend mints
        # one. The launcher synthesizes a placeholder and we reconcile it via
        # `capture_session_id`.
        return None

    def resume_command(
        self, *, session_id: str | None, cwd: Path, unsafe: bool = False
    ) -> list[str]:
        del cwd
        if session_id and _CONVERSATION_ID.match(session_id):
            return [*self._prefix(unsafe), "--conversation", session_id]
        # No usable id: `--continue` resumes the most recent conversation for
        # this workspace, which is the right one in the common case.
        return [*self._prefix(unsafe), "--continue"]

    def env(self) -> dict[str, str]:
        return {}

    # ----- session discovery / transcript parsing ------------------------------

    @staticmethod
    def app_data_dir() -> Path:
        return Path.home() / ".gemini" / "antigravity-cli"

    @classmethod
    def cache_path(cls) -> Path:
        return cls.app_data_dir() / "cache" / "last_conversations.json"

    @classmethod
    def brain_root(cls) -> Path:
        return cls.app_data_dir() / "brain"

    @classmethod
    def conversation_transcript_path(cls, session_id: str) -> Path:
        log_dir = cls.brain_root() / session_id / ".system_generated" / "logs"
        full = log_dir / "transcript_full.jsonl"
        if not (log_dir / "transcript.jsonl").exists() and full.exists():
            return full
        return log_dir / "transcript.jsonl"

    def capture_session_id(self, cwd: Path) -> str | None:
        """The conversation id `agy` last used in `cwd`, per its workspace cache."""
        try:
            raw = self.cache_path().read_text()
        except OSError:
            return None
        try:
            cache = json.loads(raw)
        except json.JSONDecodeError:
            return None
        if not isinstance(cache, dict):
            return None
        value = cache.get(str(cwd.resolve()))
        if not isinstance(value, str) or not value:
            return None
        return value

    def list_sessions(self, cwd: Path) -> list[RawSession]:
        captured = self.capture_session_id(cwd)
        if not captured or not _CONVERSATION_ID.match(captured):
            return []
        path = _find_transcript(captured, cwd, agent=self)
        if path is None or not path.exists():
            return []
        stat = path.stat()
        created = datetime.fromtimestamp(stat.st_mtime, tz=UTC)
        return [
            RawSession(
                session_id=captured,
                created_at=created,
                transcript_path=path,
                first_message_snippet=_first_user_message_snippet(path),
            )
        ]

    def read_transcript(self, session_id: str, cwd: Path) -> TranscriptSummary:
        path = _find_transcript(session_id, cwd, agent=self)
        if path is None or not path.exists():
            return TranscriptSummary()
        return _parse_transcript(path)

    def render_transcript(self, session_id: str, cwd: Path) -> str | None:
        path = _find_transcript(session_id, cwd, agent=self)
        if path is None or not path.exists():
            return None
        return _render_transcript(path)

    def read_tail(self, transcript_path: Path) -> TranscriptTail | None:
        return _read_tail(transcript_path)


# ---------------------------------------------------------------------------
# Transcript parsing / tail reading


def _find_transcript(
    session_id: str | None, cwd: Path, *, agent: AntigravityAgent | None = None
) -> Path | None:
    agent = agent or AntigravityAgent()
    if session_id and _CONVERSATION_ID.match(session_id):
        path = agent.conversation_transcript_path(session_id)
        if path.exists():
            return path
        full = (
            agent.brain_root() / session_id / ".system_generated" / "logs" / "transcript_full.jsonl"
        )
        if full.exists():
            return full
    captured = agent.capture_session_id(cwd)
    if captured and _CONVERSATION_ID.match(captured):
        path = agent.conversation_transcript_path(captured)
        if path.exists():
            return path
        full = (
            agent.brain_root() / captured / ".system_generated" / "logs" / "transcript_full.jsonl"
        )
        if full.exists():
            return full
    return None


def _iter_messages(path: Path) -> Iterator[dict]:
    if not path.exists():
        return
    try:
        with path.open("r", encoding="utf-8", errors="replace") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    obj = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if isinstance(obj, dict):
                    yield obj
    except OSError:
        return


def _extract_text(msg: dict) -> str | None:
    content = msg.get("content")
    if isinstance(content, str) and content.strip():
        return content
    text = msg.get("text")
    if isinstance(text, str) and text.strip():
        return text
    message = msg.get("message")
    if isinstance(message, str) and message.strip():
        return message
    if isinstance(message, dict):
        inner_content = message.get("content") or message.get("text")
        if isinstance(inner_content, str) and inner_content.strip():
            return inner_content
        if isinstance(inner_content, list):
            chunks = [
                b.get("text") or b.get("content") or ""
                for b in inner_content
                if isinstance(b, dict)
            ]
            joined = "\n".join(c for c in chunks if isinstance(c, str) and c.strip())
            if joined:
                return joined
    if isinstance(content, list):
        chunks = []
        for b in content:
            if isinstance(b, str) and b.strip():
                chunks.append(b)
            elif isinstance(b, dict):
                c = b.get("text") or b.get("content") or ""
                if isinstance(c, str) and c.strip():
                    chunks.append(c)
        joined = "\n".join(chunks)
        if joined:
            return joined
    return None


def _is_user_turn(msg: dict) -> bool:
    msg_type = str(msg.get("type") or "").upper()
    if msg_type in {"TOOL_RESULT", "TOOL_RESPONSE", "TOOL_CALL"}:
        return False
    if msg.get("isMeta"):
        return False
    source = str(msg.get("source") or "").upper()
    role = str(msg.get("role") or "").lower()
    if (
        msg_type in {"USER_INPUT", "USER_MESSAGE", "USER"}
        or source in {"USER_EXPLICIT", "USER"}
        or role in {"user", "human"}
    ):
        content = msg.get("content")
        if isinstance(content, list):
            blocks = [b for b in content if isinstance(b, dict)]
            if blocks and all(b.get("type") == "tool_result" for b in blocks):
                return False
        return True
    return False


def _is_assistant_turn(msg: dict) -> bool:
    msg_type = str(msg.get("type") or "").upper()
    source = str(msg.get("source") or "").upper()
    role = str(msg.get("role") or "").lower()
    return (
        msg_type in {"PLANNER_RESPONSE", "AGENT_MESSAGE", "ASSISTANT"}
        or source in {"MODEL", "ASSISTANT"}
        or role in {"assistant", "ai"}
    )


def _truncate(text: str, max_len: int = _SHORT_SNIPPET_LEN) -> str:
    text = " ".join(text.split())
    return text[:max_len] + ("…" if len(text) > max_len else "")


def _first_user_message_snippet(path: Path) -> str | None:
    for msg in _iter_messages(path):
        if _is_user_turn(msg):
            text = _extract_text(msg)
            if text:
                return _truncate(text)
    return None


def _accumulate_usage(msg: dict, usage: BucketAccumulator, counted_messages: set[str]) -> None:
    raw = msg.get("usage") or msg.get("metrics")
    if not isinstance(raw, dict):
        return
    step_id = msg.get("step_index") or msg.get("id") or msg.get("step_id")
    if step_id is not None:
        step_key = str(step_id)
        if step_key in counted_messages:
            return
        counted_messages.add(step_key)
    model = msg.get("model") or raw.get("model")
    usage.add(
        model=str(model) if model else None,
        day=local_day(msg.get("timestamp") or msg.get("created_at")),
        input_tokens=as_int(raw.get("input_tokens") or raw.get("prompt_tokens")),
        output_tokens=as_int(raw.get("output_tokens") or raw.get("completion_tokens")),
        cache_read_tokens=as_int(raw.get("cache_read_input_tokens") or raw.get("cached_tokens")),
        cache_write_tokens=as_int(
            raw.get("cache_write_tokens") or raw.get("cache_creation_input_tokens")
        ),
    )


def _parse_transcript(path: Path) -> TranscriptSummary:
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
        if _is_user_turn(msg):
            text = _extract_text(msg)
            if not text:
                continue
            summary.turn_count += 1
            short_text = _truncate(text, max_len=_SHORT_SNIPPET_LEN)
            long_text = _truncate(text, max_len=_LONG_SNIPPET_LEN)
            last_user = short_text
            if first_user_long is None:
                first_user_long = long_text
            recent_users.append(long_text)
            if len(recent_users) > _RECENT_KEEP:
                recent_users.pop(0)
        elif _is_assistant_turn(msg):
            text = _extract_text(msg)
            if not text:
                continue
            short_text = _truncate(text, max_len=_SHORT_SNIPPET_LEN)
            long_text = _truncate(text, max_len=_LONG_SNIPPET_LEN)
            last_assistant = short_text
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


def _tool_calls(msg: dict) -> list[dict]:
    raw = msg.get("tool_calls")
    if not isinstance(raw, list):
        return []
    return [tc for tc in raw if isinstance(tc, dict)]


def _read_tail(path: Path) -> TranscriptTail | None:
    records = tail_records(path)
    if not records:
        return None
    pending: set[str] = set()
    last_role: LastRole | None = None
    last_assistant: str | None = None
    has_running_status = False

    for msg in records:
        if _is_assistant_turn(msg):
            last_role = "assistant"
            status = str(msg.get("status") or "").upper()
            has_running_status = status == "RUNNING"

            for i, tc in enumerate(_tool_calls(msg)):
                call_id = (
                    tc.get("id") or tc.get("call_id") or f"tool_{msg.get('step_index', '')}_{i}"
                )
                pending.add(str(call_id))

            text = _extract_text(msg)
            if text:
                last_assistant = tail_text(text)

        elif _is_user_turn(msg):
            last_role = "user"
            last_assistant = None
            has_running_status = False
        elif (
            str(msg.get("type") or "").upper() in {"TOOL_RESULT", "TOOL_RESPONSE"}
            or str(msg.get("source") or "").upper() == "SYSTEM"
        ):
            call_id = msg.get("tool_use_id") or msg.get("call_id") or msg.get("id")
            if call_id and str(call_id) in pending:
                pending.discard(str(call_id))
            elif pending:
                pending.pop()
            has_running_status = False

    if last_role is None:
        return None

    return TranscriptTail(
        pending_tool=bool(pending) or has_running_status,
        last_role=last_role,
        last_assistant=last_assistant,
    )


def _render_transcript(path: Path) -> str | None:
    if not path.exists():
        return None
    parts: list[str] = []
    for msg in _iter_messages(path):
        if _is_user_turn(msg):
            label = "user"
        elif _is_assistant_turn(msg):
            label = "assistant"
        else:
            continue
        text = _extract_text(msg)
        if not text:
            continue
        if len(text) > _FULL_MESSAGE_CAP:
            text = text[: _FULL_MESSAGE_CAP - 1] + "…"
        parts.append(f"[{label}]\n{text}")
    if not parts:
        return None
    return "\n\n".join(parts)
