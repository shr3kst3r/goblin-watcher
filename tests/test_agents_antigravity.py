"""Tests for the Google Antigravity agent (the `agy` CLI)."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from goblin_watcher.agents import AGENT_NAMES, get_agent, registry
from goblin_watcher.agents.antigravity import AntigravityAgent

_UUID = "055a398f-db14-4c5f-abbb-1bf03f8120a7"


def _agent_with_cache(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, payload: object | None
) -> AntigravityAgent:
    """An agent whose workspace cache lives under `tmp_path` (written iff payload)."""
    cache = tmp_path / "cache" / "last_conversations.json"
    if payload is not None:
        cache.parent.mkdir(parents=True, exist_ok=True)
        cache.write_text(payload if isinstance(payload, str) else json.dumps(payload))
    monkeypatch.setattr(AntigravityAgent, "cache_path", staticmethod(lambda: cache))
    return AntigravityAgent()


def test_registered_under_antigravity() -> None:
    assert "antigravity" in registry
    assert "antigravity" in AGENT_NAMES
    assert isinstance(get_agent("antigravity"), AntigravityAgent)


def test_binary_is_agy() -> None:
    assert AntigravityAgent().binary == "agy"


def test_spawn_command_uses_prompt_interactive() -> None:
    a = AntigravityAgent()
    # `-p` / `--print` is headless mode; `--prompt-interactive` seeds a TUI session.
    assert a.spawn_command(prompt="hi there", cwd=Path("/tmp")) == [
        "agy",
        "--prompt-interactive",
        "hi there",
    ]


def test_spawn_command_unsafe_prepends_bypass_flag() -> None:
    a = AntigravityAgent()
    assert a.spawn_command(prompt="hi", cwd=Path("/tmp"), unsafe=True) == [
        "agy",
        "--dangerously-skip-permissions",
        "--prompt-interactive",
        "hi",
    ]


def test_spawn_command_ignores_preassigned_session_id() -> None:
    a = AntigravityAgent()
    assert a.new_session_id() is None
    assert a.spawn_command(prompt="hi", cwd=Path("/tmp"), session_id="whatever") == [
        "agy",
        "--prompt-interactive",
        "hi",
    ]


def test_resume_command_with_conversation_id() -> None:
    a = AntigravityAgent()
    assert a.resume_command(session_id=_UUID, cwd=Path("/tmp")) == [
        "agy",
        "--conversation",
        _UUID,
    ]
    assert a.resume_command(session_id=_UUID, cwd=Path("/tmp"), unsafe=True) == [
        "agy",
        "--dangerously-skip-permissions",
        "--conversation",
        _UUID,
    ]


def test_resume_command_without_id_uses_continue() -> None:
    a = AntigravityAgent()
    assert a.resume_command(session_id=None, cwd=Path("/tmp")) == ["agy", "--continue"]


def test_resume_command_falls_back_for_synthesized_id() -> None:
    """The launcher's placeholder id isn't a conversation id — don't pass it on."""
    a = AntigravityAgent()
    placeholder = "8f14e45fceea167a5a36dedd"  # uuid4().hex[:24], no dashes
    assert a.resume_command(session_id=placeholder, cwd=Path("/tmp")) == ["agy", "--continue"]


def test_capture_session_id_reads_workspace_cache(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    workspace = tmp_path / "worktree"
    workspace.mkdir()
    a = _agent_with_cache(
        monkeypatch, tmp_path, {str(workspace.resolve()): _UUID, "/other/repo": "nope"}
    )
    assert a.capture_session_id(workspace) == _UUID


def test_capture_session_id_none_for_unknown_workspace(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    workspace = tmp_path / "worktree"
    workspace.mkdir()
    a = _agent_with_cache(monkeypatch, tmp_path, {"/some/other/place": _UUID})
    assert a.capture_session_id(workspace) is None


def test_capture_session_id_none_when_cache_missing(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    a = _agent_with_cache(monkeypatch, tmp_path, None)
    assert a.capture_session_id(tmp_path) is None


def test_capture_session_id_survives_corrupt_cache(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    a = _agent_with_cache(monkeypatch, tmp_path, "{not json")
    assert a.capture_session_id(tmp_path) is None


def test_capture_session_id_ignores_non_mapping_cache(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    a = _agent_with_cache(monkeypatch, tmp_path, ["not", "a", "map"])
    assert a.capture_session_id(tmp_path) is None


def test_read_only_protocol_methods_are_stubs() -> None:
    a = AntigravityAgent()
    cwd = Path("/tmp")
    # Conversations live in an internal SQLite store we don't parse.
    assert a.list_sessions(cwd) == []
    assert a.read_transcript("sid", cwd).turn_count == 0
    assert a.render_transcript("sid", cwd) is None
    assert a.env() == {}


def test_headless_command_uses_print_mode() -> None:
    """`-p` is agy's headless mode — the counterpart to --prompt-interactive."""
    a = AntigravityAgent()
    assert a.headless_command(prompt="do it", cwd=Path("/tmp")) == ["agy", "-p", "do it"]
    assert a.headless_command(prompt="do it", cwd=Path("/tmp"), unsafe=True) == [
        "agy",
        "--dangerously-skip-permissions",
        "-p",
        "do it",
    ]
