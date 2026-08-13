import json
from pathlib import Path
from unittest.mock import patch

from goblin_watcher.agents.codex import CodexAgent


def _meta_line(session_id: str, cwd: Path, timestamp: str = "2026-05-20T00:37:39Z") -> str:
    payload = {"id": session_id, "cwd": str(cwd), "timestamp": timestamp}
    return json.dumps({"timestamp": timestamp, "type": "session_meta", "payload": payload})


def _user_msg_line(message: str, timestamp: str = "2026-05-20T00:38:00Z") -> str:
    return json.dumps(
        {
            "timestamp": timestamp,
            "type": "event_msg",
            "payload": {"type": "user_message", "message": message},
        }
    )


def _agent_msg_line(message: str, timestamp: str = "2026-05-20T00:38:05Z") -> str:
    return json.dumps(
        {
            "timestamp": timestamp,
            "type": "event_msg",
            "payload": {"type": "agent_message", "message": message},
        }
    )


def _write_rollout(path: Path, lines: list[str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w") as f:
        for line in lines:
            f.write(line + "\n")


def test_spawn_command_uses_prompt_positionally() -> None:
    a = CodexAgent()
    assert a.spawn_command(prompt="hi there", cwd=Path("/tmp")) == ["codex", "hi there"]


def test_resume_ignores_synthetic_session_id() -> None:
    a = CodexAgent()
    assert a.resume_command(session_id="some-fake-id", cwd=Path("/tmp")) == ["codex", "resume"]
    assert a.resume_command(session_id=None, cwd=Path("/tmp")) == ["codex", "resume"]


def test_unsafe_prepends_bypass_flag() -> None:
    a = CodexAgent()
    assert a.spawn_command(prompt="hi", cwd=Path("/tmp"), unsafe=True) == [
        "codex",
        "--dangerously-bypass-approvals-and-sandbox",
        "hi",
    ]
    assert a.resume_command(session_id="anything", cwd=Path("/tmp"), unsafe=True) == [
        "codex",
        "--dangerously-bypass-approvals-and-sandbox",
        "resume",
    ]


def test_capture_session_id_picks_newest_for_cwd(tmp_path: Path) -> None:
    a = CodexAgent()
    cwd = tmp_path / "wt"
    cwd.mkdir()
    fake_sessions = tmp_path / "codex" / "sessions"
    _write_rollout(
        fake_sessions / "2026" / "05" / "19" / "rollout-old.jsonl",
        [_meta_line("uuid-old", cwd, timestamp="2026-05-19T10:00:00Z")],
    )
    _write_rollout(
        fake_sessions / "2026" / "05" / "20" / "rollout-new.jsonl",
        [_meta_line("uuid-new", cwd, timestamp="2026-05-20T10:00:00Z")],
    )
    # A transcript in a different cwd must be ignored.
    other_cwd = tmp_path / "other-wt"
    _write_rollout(
        fake_sessions / "2026" / "05" / "20" / "rollout-other.jsonl",
        [_meta_line("uuid-other", other_cwd, timestamp="2026-05-20T11:00:00Z")],
    )
    with patch.object(CodexAgent, "sessions_root", return_value=fake_sessions):
        assert a.capture_session_id(cwd) == "uuid-new"


def test_capture_session_id_returns_none_when_empty(tmp_path: Path) -> None:
    a = CodexAgent()
    with patch.object(CodexAgent, "sessions_root", return_value=tmp_path / "noop"):
        assert a.capture_session_id(tmp_path) is None


def test_list_sessions_returns_newest_first(tmp_path: Path) -> None:
    a = CodexAgent()
    cwd = tmp_path / "wt"
    cwd.mkdir()
    fake_sessions = tmp_path / "codex" / "sessions"
    _write_rollout(
        fake_sessions / "2026" / "05" / "19" / "old.jsonl",
        [
            _meta_line("uuid-old", cwd, timestamp="2026-05-19T10:00:00Z"),
            _user_msg_line("old session"),
        ],
    )
    _write_rollout(
        fake_sessions / "2026" / "05" / "20" / "new.jsonl",
        [
            _meta_line("uuid-new", cwd, timestamp="2026-05-20T10:00:00Z"),
            _user_msg_line("new session"),
        ],
    )
    with patch.object(CodexAgent, "sessions_root", return_value=fake_sessions):
        sessions = a.list_sessions(cwd)
    assert [s.session_id for s in sessions] == ["uuid-new", "uuid-old"]
    assert sessions[0].first_message_snippet == "new session"


def test_read_transcript_counts_user_turns_via_event_msg(tmp_path: Path) -> None:
    a = CodexAgent()
    cwd = tmp_path / "wt"
    cwd.mkdir()
    fake_sessions = tmp_path / "codex" / "sessions"
    rollout = fake_sessions / "2026" / "05" / "20" / "r.jsonl"
    _write_rollout(
        rollout,
        [
            _meta_line("uuid-1", cwd),
            # Auto-injected response_item user noise should NOT count.
            json.dumps(
                {
                    "type": "response_item",
                    "payload": {
                        "type": "message",
                        "role": "user",
                        "content": [{"type": "input_text", "text": "# AGENTS.md instructions ..."}],
                    },
                }
            ),
            _user_msg_line("Do a code review"),
            _agent_msg_line("Reviewing now."),
            _user_msg_line("Anything blocking?"),
            _agent_msg_line("One scheduling issue."),
        ],
    )
    with patch.object(CodexAgent, "sessions_root", return_value=fake_sessions):
        summary = a.read_transcript("uuid-1", cwd)
    assert summary.turn_count == 2
    assert summary.last_user_snippet == "Anything blocking?"
    assert summary.last_assistant_snippet == "One scheduling issue."
    assert summary.first_user_snippet == "Do a code review"
    assert summary.recent_user_snippets == ["Do a code review", "Anything blocking?"]
    assert summary.recent_assistant_snippets == ["Reviewing now.", "One scheduling issue."]


def _turn_context_line(model: str, timestamp: str = "2026-05-20T00:38:00Z") -> str:
    return json.dumps(
        {"timestamp": timestamp, "type": "turn_context", "payload": {"model": model, "cwd": "/x"}}
    )


def _token_count_line(
    *,
    input_tokens: int,
    cached: int,
    output_tokens: int,
    timestamp: str = "2026-05-20T12:00:00Z",
) -> str:
    """A codex `token_count` event, whose totals are cumulative for the session."""
    totals = {
        "input_tokens": input_tokens,
        "cached_input_tokens": cached,
        "output_tokens": output_tokens,
        "total_tokens": input_tokens + output_tokens,
    }
    return json.dumps(
        {
            "timestamp": timestamp,
            "type": "event_msg",
            "payload": {
                "type": "token_count",
                "info": {"total_token_usage": totals, "last_token_usage": totals},
            },
        }
    )


def _usage_of(a: CodexAgent, tmp_path: Path, lines: list[str]) -> list:
    cwd = tmp_path / "wt"
    cwd.mkdir(exist_ok=True)
    fake_sessions = tmp_path / "codex" / "sessions"
    _write_rollout(
        fake_sessions / "2026" / "05" / "20" / "r.jsonl", [_meta_line("uuid-1", cwd), *lines]
    )
    with patch.object(CodexAgent, "sessions_root", return_value=fake_sessions):
        return a.read_transcript("uuid-1", cwd).usage


def test_usage_differences_cumulative_totals(tmp_path: Path) -> None:
    """Codex reports session totals on every event, so buckets must sum back to
    the *last* event's totals — not to the sum of every event."""
    a = CodexAgent()
    buckets = _usage_of(
        a,
        tmp_path,
        [
            _turn_context_line("gpt-5-codex"),
            _token_count_line(input_tokens=1_000, cached=400, output_tokens=50),
            _token_count_line(input_tokens=3_000, cached=1_400, output_tokens=120),
        ],
    )
    [bucket] = buckets
    assert bucket.model == "gpt-5-codex"
    # Cached input is a subset of input_tokens; only the remainder is full price.
    assert bucket.cache_read_tokens == 1_400
    assert bucket.input_tokens == 3_000 - 1_400
    assert bucket.output_tokens == 120


def test_usage_attributes_each_turn_to_its_own_model(tmp_path: Path) -> None:
    a = CodexAgent()
    buckets = _usage_of(
        a,
        tmp_path,
        [
            _turn_context_line("gpt-5-codex"),
            _token_count_line(input_tokens=1_000, cached=0, output_tokens=100),
            _turn_context_line("gpt-5.5"),
            _token_count_line(input_tokens=1_500, cached=0, output_tokens=250),
        ],
    )
    by_model = {b.model: b for b in buckets}
    assert by_model["gpt-5-codex"].output_tokens == 100
    assert by_model["gpt-5.5"].output_tokens == 150
    assert by_model["gpt-5.5"].input_tokens == 500


def test_usage_treats_a_backwards_counter_as_a_fresh_baseline(tmp_path: Path) -> None:
    """A resumed rollout can restart its counters; differencing across the reset
    would otherwise produce a negative turn."""
    a = CodexAgent()
    buckets = _usage_of(
        a,
        tmp_path,
        [
            _turn_context_line("gpt-5-codex"),
            _token_count_line(input_tokens=9_000, cached=0, output_tokens=900),
            _token_count_line(input_tokens=100, cached=0, output_tokens=10),
        ],
    )
    [bucket] = buckets
    assert bucket.input_tokens == 9_100
    assert bucket.output_tokens == 910


def test_usage_ignores_events_without_totals(tmp_path: Path) -> None:
    a = CodexAgent()
    buckets = _usage_of(
        a,
        tmp_path,
        [
            _turn_context_line("gpt-5-codex"),
            json.dumps(
                {
                    "timestamp": "2026-05-20T12:00:00Z",
                    "type": "event_msg",
                    "payload": {"type": "token_count", "info": {"total_token_usage": None}},
                }
            ),
            _user_msg_line("hello"),
        ],
    )
    assert buckets == []


def test_read_transcript_falls_back_to_newest_for_synthetic_id(tmp_path: Path) -> None:
    a = CodexAgent()
    cwd = tmp_path / "wt"
    cwd.mkdir()
    fake_sessions = tmp_path / "codex" / "sessions"
    _write_rollout(
        fake_sessions / "2026" / "05" / "20" / "r.jsonl",
        [
            _meta_line("uuid-real", cwd, timestamp="2026-05-20T10:00:00Z"),
            _user_msg_line("the actual conversation"),
        ],
    )
    with patch.object(CodexAgent, "sessions_root", return_value=fake_sessions):
        summary = a.read_transcript("synthetic-id-that-doesnt-exist", cwd)
    assert summary.turn_count == 1
    assert summary.last_user_snippet == "the actual conversation"


def test_read_transcript_returns_empty_when_no_match(tmp_path: Path) -> None:
    a = CodexAgent()
    cwd = tmp_path / "wt"
    cwd.mkdir()
    with patch.object(CodexAgent, "sessions_root", return_value=tmp_path / "noop"):
        summary = a.read_transcript("anything", cwd)
    assert summary.turn_count == 0
    assert summary.last_user_snippet is None


def test_render_transcript_labels_event_messages(tmp_path: Path) -> None:
    a = CodexAgent()
    cwd = tmp_path / "wt"
    cwd.mkdir()
    fake_sessions = tmp_path / "codex" / "sessions"
    _write_rollout(
        fake_sessions / "2026" / "05" / "20" / "r.jsonl",
        [
            _meta_line("uuid-1", cwd),
            _user_msg_line("kick off the work"),
            _agent_msg_line("on it"),
        ],
    )
    with patch.object(CodexAgent, "sessions_root", return_value=fake_sessions):
        rendered = a.render_transcript("uuid-1", cwd)
    assert rendered is not None
    assert "[user]\nkick off the work" in rendered
    assert "[assistant]\non it" in rendered


def test_render_transcript_returns_none_when_no_messages(tmp_path: Path) -> None:
    a = CodexAgent()
    cwd = tmp_path / "wt"
    cwd.mkdir()
    fake_sessions = tmp_path / "codex" / "sessions"
    # Meta only — no user_message / agent_message events.
    _write_rollout(
        fake_sessions / "2026" / "05" / "20" / "r.jsonl",
        [_meta_line("uuid-1", cwd)],
    )
    with patch.object(CodexAgent, "sessions_root", return_value=fake_sessions):
        assert a.render_transcript("uuid-1", cwd) is None


def test_headless_command_uses_exec_subcommand() -> None:
    a = CodexAgent()
    assert a.headless_command(prompt="do it", cwd=Path("/tmp")) == ["codex", "exec", "do it"]


def test_headless_bypass_flag_follows_the_subcommand() -> None:
    """`exec` declares its own copy of the flag; putting it before the
    subcommand would rely on the root parser accepting it."""
    a = CodexAgent()
    assert a.headless_command(prompt="do it", cwd=Path("/tmp"), unsafe=True) == [
        "codex",
        "exec",
        "--dangerously-bypass-approvals-and-sandbox",
        "do it",
    ]
