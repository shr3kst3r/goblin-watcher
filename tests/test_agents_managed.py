"""Tests for the managed-agent scaffolding (ADR 0002).

The agent is registered but its remote backend isn't wired. These tests
exercise the parts that *are* real: registry presence, the validator's
remote-gate behavior, and the cleanly-erroring shape of the unwired calls.
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pytest

from goblin_watcher.agents import (
    AGENT_NAMES,
    get_agent,
    registry,
    validate_agent_for_project,
)
from goblin_watcher.agents.managed import (
    ManagedAgent,
    ManagedClient,
    NotConfiguredClient,
    PatchArtifact,
    RemoteSession,
)
from goblin_watcher.errors import GoblinError
from goblin_watcher.models import Project


def _project(name: str = "demo", *, repo_url: str | None) -> Project:
    return Project(
        name=name,
        root=Path("/tmp/fake-root"),
        repo_url=repo_url,
        created_at=datetime.now(UTC),
    )


def test_managed_is_registered() -> None:
    assert "managed" in registry
    assert "managed" in AGENT_NAMES
    assert isinstance(get_agent("managed"), ManagedAgent)


def test_validator_allows_local_agents_regardless_of_remote() -> None:
    # Local agents have no project-level prerequisites.
    proj = _project(repo_url=None)
    for name in ("claude", "codex", "gemini", "antigravity"):
        validate_agent_for_project(name, proj)  # does not raise


def test_validator_refuses_managed_without_remote() -> None:
    proj = _project(repo_url=None)
    with pytest.raises(GoblinError) as exc:
        validate_agent_for_project("managed", proj)
    assert "managed agent" in exc.value.message.lower()
    assert exc.value.hint is not None


def test_validator_allows_managed_with_remote() -> None:
    proj = _project(repo_url="git@github.com:acme/repo.git")
    validate_agent_for_project("managed", proj)  # does not raise


def test_managed_agent_spawn_raises_unwired_error() -> None:
    agent = ManagedAgent()
    with pytest.raises(GoblinError) as exc:
        agent.spawn_command(prompt="hi", cwd=Path("/tmp"))
    assert "launcher is not wired" in exc.value.message


def test_managed_agent_resume_raises_unwired_error() -> None:
    agent = ManagedAgent()
    with pytest.raises(GoblinError):
        agent.resume_command(session_id=None, cwd=Path("/tmp"))


def test_managed_agent_read_only_protocol_methods_return_empty() -> None:
    agent = ManagedAgent()
    cwd = Path("/tmp")
    assert agent.capture_session_id(cwd) is None
    assert agent.list_sessions(cwd) == []
    assert agent.read_transcript("sid", cwd).turn_count == 0
    assert agent.render_transcript("sid", cwd) is None
    assert agent.env() == {}


def test_managed_client_protocol_is_runtime_checkable() -> None:
    # NotConfiguredClient implements every method on ManagedClient — it should
    # therefore satisfy the runtime_checkable Protocol.
    client = NotConfiguredClient()
    assert isinstance(client, ManagedClient)


def test_not_configured_client_methods_all_raise() -> None:
    client = NotConfiguredClient()
    with pytest.raises(GoblinError):
        client.create_session(repo_url="x", base_branch="main", prompt="p")
    with pytest.raises(GoblinError):
        client.submit_turn("sid", "msg")
    with pytest.raises(GoblinError):
        list(client.stream_events("sid"))  # generator-or-call: force materialize
    with pytest.raises(GoblinError):
        client.fetch_patch("sid")
    with pytest.raises(GoblinError):
        client.terminate("sid")


def test_patch_artifact_defaults_session_end() -> None:
    p = PatchArtifact(session_id="sid", base_sha="abc123", diff="--- a\n+++ b\n")
    assert p.checkpoint is None


def test_remote_session_is_frozen() -> None:
    from dataclasses import FrozenInstanceError

    rs = RemoteSession(session_id="sid", base_sha="abc123")
    with pytest.raises(FrozenInstanceError):
        rs.session_id = "other"  # type: ignore[misc]
