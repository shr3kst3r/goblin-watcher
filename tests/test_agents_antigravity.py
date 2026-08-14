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


def test_capability_is_parseable() -> None:
    assert AntigravityAgent.transcripts.parseable
    assert AntigravityAgent.transcripts.reason == ""


def _write_transcript(path: Path, records: list[dict]) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    lines = [json.dumps(r) for r in records]
    path.write_text("\n".join(lines) + "\n")
    return path


def test_list_sessions_finds_recorded_session(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    workspace = tmp_path / "worktree"
    workspace.mkdir()
    a = _agent_with_cache(monkeypatch, tmp_path, {str(workspace.resolve()): _UUID})
    monkeypatch.setattr(AntigravityAgent, "brain_root", classmethod(lambda cls: tmp_path / "brain"))

    # Before transcript exists -> returns empty list
    assert a.list_sessions(workspace) == []

    # Write transcript
    transcript_path = tmp_path / "brain" / _UUID / ".system_generated" / "logs" / "transcript.jsonl"
    _write_transcript(
        transcript_path,
        [
            {"type": "USER_INPUT", "source": "USER_EXPLICIT", "content": "hello world"},
            {"type": "PLANNER_RESPONSE", "source": "MODEL", "content": "hi there"},
        ],
    )

    sessions = a.list_sessions(workspace)
    assert len(sessions) == 1
    assert sessions[0].session_id == _UUID
    assert sessions[0].transcript_path == transcript_path
    assert sessions[0].first_message_snippet == "hello world"


def test_read_transcript_parses_turn_count_and_snippets(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    a = AntigravityAgent()
    monkeypatch.setattr(AntigravityAgent, "brain_root", classmethod(lambda cls: tmp_path / "brain"))

    transcript_path = tmp_path / "brain" / _UUID / ".system_generated" / "logs" / "transcript.jsonl"
    _write_transcript(
        transcript_path,
        [
            {"type": "USER_INPUT", "source": "USER_EXPLICIT", "content": "first prompt"},
            {"type": "PLANNER_RESPONSE", "source": "MODEL", "content": "first answer"},
            {"type": "USER_INPUT", "source": "USER_EXPLICIT", "content": "second prompt"},
            {"type": "PLANNER_RESPONSE", "source": "MODEL", "content": "second answer"},
        ],
    )

    summary = a.read_transcript(_UUID, tmp_path)
    assert summary.turn_count == 2
    assert summary.first_user_snippet == "first prompt"
    assert summary.last_user_snippet == "second prompt"
    assert summary.last_assistant_snippet == "second answer"
    assert summary.transcript_path == transcript_path


def test_read_transcript_fallback_by_cwd(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    workspace = tmp_path / "worktree"
    workspace.mkdir()
    a = _agent_with_cache(monkeypatch, tmp_path, {str(workspace.resolve()): _UUID})
    monkeypatch.setattr(AntigravityAgent, "brain_root", classmethod(lambda cls: tmp_path / "brain"))

    transcript_path = tmp_path / "brain" / _UUID / ".system_generated" / "logs" / "transcript.jsonl"
    _write_transcript(
        transcript_path,
        [
            {"type": "USER_INPUT", "source": "USER_EXPLICIT", "content": "fallback prompt"},
            {"type": "PLANNER_RESPONSE", "source": "MODEL", "content": "fallback answer"},
        ],
    )

    # Calling with unknown/placeholder session_id falls back to cwd in cache
    summary = a.read_transcript("placeholder-123", workspace)
    assert summary.turn_count == 1
    assert summary.last_user_snippet == "fallback prompt"
    assert summary.transcript_path == transcript_path


def test_read_transcript_accumulates_usage(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    a = AntigravityAgent()
    monkeypatch.setattr(AntigravityAgent, "brain_root", classmethod(lambda cls: tmp_path / "brain"))

    transcript_path = tmp_path / "brain" / _UUID / ".system_generated" / "logs" / "transcript.jsonl"
    _write_transcript(
        transcript_path,
        [
            {
                "step_index": 0,
                "type": "USER_INPUT",
                "source": "USER_EXPLICIT",
                "content": "do work",
            },
            {
                "step_index": 1,
                "type": "PLANNER_RESPONSE",
                "source": "MODEL",
                "content": "working",
                "model": "gemini-3.7-flash",
                "usage": {
                    "input_tokens": 100,
                    "output_tokens": 50,
                    "cache_read_input_tokens": 20,
                    "cache_write_tokens": 10,
                },
            },
        ],
    )

    summary = a.read_transcript(_UUID, tmp_path)
    assert len(summary.usage) == 1
    assert summary.usage[0].model == "gemini-3.7-flash"
    assert summary.usage[0].input_tokens == 100
    assert summary.usage[0].output_tokens == 50
    assert summary.usage[0].cache_read_tokens == 20
    assert summary.usage[0].cache_write_tokens == 10


def test_render_transcript(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    a = AntigravityAgent()
    monkeypatch.setattr(AntigravityAgent, "brain_root", classmethod(lambda cls: tmp_path / "brain"))

    transcript_path = tmp_path / "brain" / _UUID / ".system_generated" / "logs" / "transcript.jsonl"
    _write_transcript(
        transcript_path,
        [
            {"type": "USER_INPUT", "source": "USER_EXPLICIT", "content": "user question"},
            {"type": "PLANNER_RESPONSE", "source": "MODEL", "content": "assistant reply"},
        ],
    )

    rendered = a.render_transcript(_UUID, tmp_path)
    assert rendered == "[user]\nuser question\n\n[assistant]\nassistant reply"


def test_read_tail_pending_tool_call(tmp_path: Path) -> None:
    a = AntigravityAgent()
    path = tmp_path / "transcript.jsonl"
    _write_transcript(
        path,
        [
            {"type": "USER_INPUT", "source": "USER_EXPLICIT", "content": "run grep"},
            {
                "type": "PLANNER_RESPONSE",
                "source": "MODEL",
                "content": "running grep...",
                "tool_calls": [{"id": "call_1", "name": "grep_search", "args": {}}],
            },
        ],
    )
    tail = a.read_tail(path)
    assert tail is not None
    assert tail.pending_tool is True
    assert tail.last_role == "assistant"
    assert tail.last_assistant == "running grep..."


def test_read_tail_completed_tool_call(tmp_path: Path) -> None:
    a = AntigravityAgent()
    path = tmp_path / "transcript.jsonl"
    _write_transcript(
        path,
        [
            {"type": "USER_INPUT", "source": "USER_EXPLICIT", "content": "run grep"},
            {
                "type": "PLANNER_RESPONSE",
                "source": "MODEL",
                "content": "running grep...",
                "tool_calls": [{"id": "call_1", "name": "grep_search", "args": {}}],
            },
            {
                "type": "TOOL_RESULT",
                "source": "SYSTEM",
                "tool_use_id": "call_1",
                "content": "found matches",
            },
            {
                "type": "PLANNER_RESPONSE",
                "source": "MODEL",
                "content": "I finished the search.",
            },
        ],
    )
    tail = a.read_tail(path)
    assert tail is not None
    assert tail.pending_tool is False
    assert tail.last_role == "assistant"
    assert tail.last_assistant == "I finished the search."


def test_read_tail_running_status(tmp_path: Path) -> None:
    a = AntigravityAgent()
    path = tmp_path / "transcript.jsonl"
    _write_transcript(
        path,
        [
            {"type": "USER_INPUT", "source": "USER_EXPLICIT", "content": "think"},
            {
                "type": "PLANNER_RESPONSE",
                "source": "MODEL",
                "status": "RUNNING",
                "content": "thinking...",
            },
        ],
    )
    tail = a.read_tail(path)
    assert tail is not None
    assert tail.pending_tool is True
    assert tail.last_role == "assistant"


def test_read_tail_user_last_role(tmp_path: Path) -> None:
    a = AntigravityAgent()
    path = tmp_path / "transcript.jsonl"
    _write_transcript(
        path,
        [
            {"type": "PLANNER_RESPONSE", "source": "MODEL", "content": "ready"},
            {"type": "USER_INPUT", "source": "USER_EXPLICIT", "content": "next task"},
        ],
    )
    tail = a.read_tail(path)
    assert tail is not None
    assert tail.last_role == "user"
    assert tail.last_assistant is None


def test_read_tail_missing_or_empty(tmp_path: Path) -> None:
    a = AntigravityAgent()
    assert a.read_tail(tmp_path / "nonexistent.jsonl") is None
    empty = tmp_path / "empty.jsonl"
    empty.write_text("")
    assert a.read_tail(empty) is None
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
