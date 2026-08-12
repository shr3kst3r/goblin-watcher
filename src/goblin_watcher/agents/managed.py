"""Managed agent (Anthropic-hosted execution).

Design recorded in `docs/adrs/0002-managed-agent-patch-return.md`. The agent
runs in a remote sandbox; `gw` attaches bidirectionally and applies returned
patches to the local worktree. Sessions persist across detach.

This module is **scaffolding only**. The `ManagedClient` Protocol defines the
remote-API surface that the eventual backend will implement; today only
`NotConfiguredClient` is wired, and its methods raise a `GoblinError` that
points the user at the missing piece. `ManagedAgent`'s interactive methods
(`spawn_command`, `resume_command`) raise the same error — the existing
`launcher.launch` pathway is subprocess-shaped and can't drive a managed
attach loop without further work. That dispatch is deliberately left out of
this first slice; see the ADR for the intended shape.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Protocol, runtime_checkable

from goblin_watcher.agents.base import RawSession, TranscriptSummary
from goblin_watcher.errors import GoblinError


@dataclass(frozen=True)
class RemoteSession:
    """Handle to a session running in the managed sandbox."""

    session_id: str
    base_sha: str  # commit the sandbox cloned from; used to validate patch apply


@dataclass(frozen=True)
class PatchArtifact:
    """A patch produced by the remote sandbox.

    `base_sha` is the local commit the sandbox started from; the patch is
    expected to apply cleanly on top of that commit, and `apply_patch_safely`
    refuses if HEAD has moved.
    """

    session_id: str
    base_sha: str
    diff: str
    checkpoint: str | None = None  # None = session-end patch; otherwise user-named


@runtime_checkable
class ManagedClient(Protocol):
    """Wire protocol for the Anthropic-hosted execution backend.

    Intentionally narrow. Each method maps to one remote operation. Backends
    that stream (e.g. `attach`) are expected to expose an iterator; the
    eventual launcher will drive it.
    """

    def create_session(self, *, repo_url: str, base_branch: str, prompt: str) -> RemoteSession:
        """Boot a remote sandbox and seed it with `prompt`."""
        ...

    def submit_turn(self, session_id: str, message: str) -> None:
        """Send a user turn to the running session."""
        ...

    def stream_events(self, session_id: str, since_offset: int = 0):
        """Yield transcript events from `since_offset` forward.

        Concrete shape (event payload) is deliberately unspecified at the
        scaffolding stage — the real wire format gets pinned when the backend
        is wired.
        """
        ...

    def fetch_patch(self, session_id: str, *, checkpoint: str | None = None) -> PatchArtifact:
        """Snapshot the sandbox's diff against `base_sha`.

        `checkpoint=None` is the session-end patch. A non-None value is the
        user-named mid-session checkpoint.
        """
        ...

    def terminate(self, session_id: str) -> None:
        """Tear down the remote sandbox."""
        ...


class NotConfiguredClient:
    """Default `ManagedClient` impl until a real backend is wired.

    Every method raises `GoblinError`. We pick a single message so the user
    sees the same instruction regardless of where the call originates.
    """

    _MSG = "Managed-agent backend is not configured."
    _HINT = (
        "ADR 0002 describes the intended integration. No remote-execution "
        "backend ships with gw yet — pick a local agent "
        "(claude/codex/gemini/antigravity) "
        "or wire a ManagedClient implementation."
    )

    def _refuse(self) -> GoblinError:
        return GoblinError(self._MSG, hint=self._HINT)

    def create_session(self, *, repo_url: str, base_branch: str, prompt: str) -> RemoteSession:
        del repo_url, base_branch, prompt
        raise self._refuse()

    def submit_turn(self, session_id: str, message: str) -> None:
        del session_id, message
        raise self._refuse()

    def stream_events(self, session_id: str, since_offset: int = 0):
        del session_id, since_offset
        raise self._refuse()

    def fetch_patch(self, session_id: str, *, checkpoint: str | None = None) -> PatchArtifact:
        del session_id, checkpoint
        raise self._refuse()

    def terminate(self, session_id: str) -> None:
        del session_id
        raise self._refuse()


_LAUNCHER_NOT_WIRED = GoblinError(
    "Managed agent is registered but its launcher is not wired yet.",
    hint=(
        "The existing launcher invokes agents as subprocesses; managed runs "
        "need an attach loop driving a ManagedClient. See "
        "docs/adrs/0002-managed-agent-patch-return.md for the intended shape."
    ),
)


class ManagedAgent:
    """Anthropic-hosted agent — scaffold only."""

    name = "managed"
    binary = ""  # No local binary; doctor's binary-check row skips this agent.

    def __init__(self, client: ManagedClient | None = None) -> None:
        self.client: ManagedClient = client or NotConfiguredClient()

    def spawn_command(
        self, *, prompt: str, cwd: Path, unsafe: bool = False, session_id: str | None = None
    ) -> list[str]:
        del prompt, cwd, unsafe, session_id
        raise _LAUNCHER_NOT_WIRED

    def new_session_id(self) -> str | None:
        return None

    def resume_command(
        self, *, session_id: str | None, cwd: Path, unsafe: bool = False
    ) -> list[str]:
        del session_id, cwd, unsafe
        raise _LAUNCHER_NOT_WIRED

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

    def env(self) -> dict[str, str]:
        return {}
