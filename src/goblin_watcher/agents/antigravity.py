"""Google Antigravity agent (the `agy` CLI).

Antigravity CLI installs as `agy` (`~/.local/bin/agy` on macOS/Linux) and
seeds a fresh interactive session with `agy --prompt-interactive "<prompt>"`
— `-p` / `--print` is its *headless* mode, which would print and exit rather
than hand the user a TUI.

Conversations live server-side; locally the CLI keeps them in SQLite under
`~/.gemini/antigravity-cli/` (the Antigravity CLI shares Gemini's config
root). We deliberately don't parse that database — the schema is internal and
undocumented — so transcript summaries stay empty, exactly like the gemini
agent. What we *can* read is the documented workspace cache at
`~/.gemini/antigravity-cli/cache/last_conversations.json`, a JSON map of
absolute workspace path → most recent conversation id. That's enough to
capture the real conversation id after an inline run and resume it later by id
(`agy --conversation <id>`), falling back to the workspace-scoped
`agy --continue`.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

from goblin_watcher.agents.base import RawSession, TranscriptSummary

# Conversation ids are UUIDs. The launcher synthesizes a 24-char hex placeholder
# for agents that can't preassign an id (see `agents/launcher.py`), and in tmux
# mode that placeholder is never reconciled — passing it to `--conversation`
# would just earn a "conversation not found" warning. Shape-check before use.
_CONVERSATION_ID = re.compile(
    r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$"
)


class AntigravityAgent:
    name = "antigravity"
    binary = "agy"
    unsafe_flags: tuple[str, ...] = ("--dangerously-skip-permissions",)

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

    # ----- session discovery ---------------------------------------------------

    @staticmethod
    def cache_path() -> Path:
        return Path.home() / ".gemini" / "antigravity-cli" / "cache" / "last_conversations.json"

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
        # Conversations are stored in an internal SQLite database, not as
        # per-session transcript files we can enumerate. `gw` still tracks the
        # sessions it launched itself, via SessionRecord.
        del cwd
        return []

    def read_transcript(self, session_id: str, cwd: Path) -> TranscriptSummary:
        del session_id, cwd
        return TranscriptSummary()

    def render_transcript(self, session_id: str, cwd: Path) -> str | None:
        del session_id, cwd
        return None
