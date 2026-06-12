"""Codex agent (the `codex` CLI).

Codex stores each session as a JSONL rollout under
`~/.codex/sessions/<YYYY>/<MM>/<DD>/rollout-<ts>-<uuid>.jsonl`. The first line
is a `session_meta` envelope that carries the session id and the cwd codex was
launched in. We use that to associate transcripts with goblin tasks (matched
by cwd).

User-typed input is emitted as `event_msg` with `type=user_message`; the
assistant's natural-language replies are `event_msg` with `type=agent_message`.
We rely on those rather than the `response_item` user/assistant messages
because codex auto-injects an AGENTS.md preamble and `<environment_context>`
wrapper as the first `response_item` user message — using `event_msg` keeps
the description LLM focused on what the user actually typed.
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path

from goblin_watcher.agents.base import RawSession, TranscriptSummary

_SHORT_SNIPPET_LEN = 120
_LONG_SNIPPET_LEN = 400
_RECENT_KEEP = 3
# Cap any single agent_message at this length when rendering the full
# transcript. Codex agents sometimes paste large code blocks; without a cap one
# runaway message could blow the description prompt budget. The description
# module then applies a whole-transcript cap on top of this.
_FULL_MESSAGE_CAP = 8000


class CodexAgent:
    name = "codex"
    binary = "codex"
    unsafe_flags: tuple[str, ...] = ("--dangerously-bypass-approvals-and-sandbox",)

    def _prefix(self, unsafe: bool) -> list[str]:
        return [self.binary, *self.unsafe_flags] if unsafe else [self.binary]

    def spawn_command(
        self, *, prompt: str, cwd: Path, unsafe: bool = False, session_id: str | None = None
    ) -> list[str]:
        del cwd, session_id
        return [*self._prefix(unsafe), prompt]

    def new_session_id(self) -> str | None:
        # The codex CLI has no way to preassign a session id at spawn.
        return None

    def resume_command(
        self, *, session_id: str | None, cwd: Path, unsafe: bool = False
    ) -> list[str]:
        # We can't reliably round-trip synthesized ids back to `codex resume
        # <id>`; the launcher rewrites them to the real codex UUID only on
        # inline mode. Always fall back to codex's own picker.
        del cwd, session_id
        return [*self._prefix(unsafe), "resume"]

    def env(self) -> dict[str, str]:
        return {}

    @staticmethod
    def sessions_root() -> Path:
        return Path.home() / ".codex" / "sessions"

    def capture_session_id(self, cwd: Path) -> str | None:
        match = _newest_for_cwd(self.sessions_root(), cwd)
        if match is None:
            return None
        return str(match[1].get("id") or "") or None

    def list_sessions(self, cwd: Path) -> list[RawSession]:
        out: list[RawSession] = []
        for path, meta in _metas_for_cwd(self.sessions_root(), cwd):
            sid = str(meta.get("id") or "") or path.stem
            ts = _parse_iso(meta.get("timestamp")) or _file_mtime(path)
            out.append(
                RawSession(
                    session_id=sid,
                    created_at=ts,
                    transcript_path=path,
                    first_message_snippet=_first_user_snippet(path),
                )
            )
        out.sort(key=lambda r: r.created_at, reverse=True)
        return out

    def read_transcript(self, session_id: str, cwd: Path) -> TranscriptSummary:
        path = _find_transcript(self.sessions_root(), cwd, session_id)
        if path is None:
            return TranscriptSummary()
        return _parse_transcript(path)

    def render_transcript(self, session_id: str, cwd: Path) -> str | None:
        path = _find_transcript(self.sessions_root(), cwd, session_id)
        if path is None:
            return None
        return _render_transcript(path)


# ---------------------------------------------------------------------------
# Transcript discovery


def _iter_lines(path: Path) -> Iterator[dict]:
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


def _read_meta(path: Path) -> dict | None:
    """Return the `session_meta` payload from `path`, or None if absent.

    The meta envelope is the first line of a codex rollout. We only check the
    first non-blank record so this is cheap to call on every jsonl found.
    """
    for obj in _iter_lines(path):
        if obj.get("type") == "session_meta":
            payload = obj.get("payload")
            return payload if isinstance(payload, dict) else None
        return None
    return None


def _parse_iso(value: object) -> datetime | None:
    if not isinstance(value, str):
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


def _file_mtime(path: Path) -> datetime:
    try:
        return datetime.fromtimestamp(path.stat().st_mtime, tz=UTC)
    except OSError:
        return datetime.fromtimestamp(0, tz=UTC)


def _norm(p: object) -> str:
    if not isinstance(p, str) or not p:
        return ""
    try:
        return str(Path(p).resolve())
    except OSError:
        return p


def _iter_metas(root: Path) -> Iterator[tuple[Path, dict]]:
    if not root.exists():
        return
    for p in root.rglob("*.jsonl"):
        meta = _read_meta(p)
        if meta is not None:
            yield p, meta


def _metas_for_cwd(root: Path, cwd: Path) -> list[tuple[Path, dict]]:
    target = _norm(str(cwd))
    return [(p, m) for p, m in _iter_metas(root) if _norm(m.get("cwd")) == target]


def _newest_for_cwd(root: Path, cwd: Path) -> tuple[Path, dict] | None:
    matches = _metas_for_cwd(root, cwd)
    if not matches:
        return None

    def _key(item: tuple[Path, dict]) -> datetime:
        return _parse_iso(item[1].get("timestamp")) or _file_mtime(item[0])

    return max(matches, key=_key)


def _find_transcript(root: Path, cwd: Path, session_id: str) -> Path | None:
    """Locate the codex rollout for `session_id` under `cwd`.

    Synthesized session ids never match a real `session_meta.id`; for those
    we fall back to the newest rollout for the cwd. That's accurate for the
    common case of one active codex session per task; multiple synthesized
    sessions on the same cwd collapse to the same transcript (best-effort).
    """
    target_cwd = _norm(str(cwd))
    for p, meta in _iter_metas(root):
        if _norm(meta.get("cwd")) != target_cwd:
            continue
        if str(meta.get("id") or "") == session_id:
            return p
    newest = _newest_for_cwd(root, cwd)
    return newest[0] if newest else None


# ---------------------------------------------------------------------------
# Transcript parsing


def _truncate(text: str, max_len: int) -> str:
    text = " ".join(text.split())
    return text[:max_len] + ("…" if len(text) > max_len else "")


def _cap(text: str, max_len: int) -> str:
    if len(text) > max_len:
        return text[: max_len - 1] + "…"
    return text


def _iter_messages(path: Path) -> Iterator[tuple[str, str]]:
    """Yield (role, text) for each user/assistant message in `path`.

    Driven by `event_msg.user_message` / `event_msg.agent_message` so the
    auto-injected AGENTS.md preamble and `<environment_context>` wrapper
    don't pollute the output.
    """
    for obj in _iter_lines(path):
        if obj.get("type") != "event_msg":
            continue
        payload = obj.get("payload") or {}
        kind = payload.get("type")
        if kind == "user_message":
            text = payload.get("message")
            if isinstance(text, str) and text:
                yield "user", text
        elif kind == "agent_message":
            text = payload.get("message")
            if isinstance(text, str) and text:
                yield "assistant", text


def _parse_transcript(path: Path) -> TranscriptSummary:
    summary = TranscriptSummary(transcript_path=path)
    last_user: str | None = None
    last_assistant: str | None = None
    first_user_long: str | None = None
    recent_users: list[str] = []
    recent_assistants: list[str] = []
    for role, text in _iter_messages(path):
        if role == "user":
            summary.turn_count += 1
            last_user = _truncate(text, _SHORT_SNIPPET_LEN)
            long_text = _truncate(text, _LONG_SNIPPET_LEN)
            if first_user_long is None:
                first_user_long = long_text
            recent_users.append(long_text)
            if len(recent_users) > _RECENT_KEEP:
                recent_users.pop(0)
        else:
            last_assistant = _truncate(text, _SHORT_SNIPPET_LEN)
            recent_assistants.append(_truncate(text, _LONG_SNIPPET_LEN))
            if len(recent_assistants) > _RECENT_KEEP:
                recent_assistants.pop(0)
    summary.last_user_snippet = last_user
    summary.last_assistant_snippet = last_assistant
    summary.first_user_snippet = first_user_long
    summary.recent_user_snippets = recent_users
    summary.recent_assistant_snippets = recent_assistants
    return summary


def _render_transcript(path: Path) -> str | None:
    parts: list[str] = []
    for role, text in _iter_messages(path):
        parts.append(f"[{role}]\n{_cap(text, _FULL_MESSAGE_CAP)}")
    if not parts:
        return None
    return "\n\n".join(parts)


def _first_user_snippet(path: Path) -> str | None:
    for role, text in _iter_messages(path):
        if role == "user":
            return _truncate(text, _SHORT_SNIPPET_LEN)
    return None
