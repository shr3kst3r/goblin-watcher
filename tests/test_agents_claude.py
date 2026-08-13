import json
from pathlib import Path
from unittest.mock import patch

from goblin_watcher.agents.claude import ClaudeAgent


def _write_jsonl(path: Path, msgs: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w") as f:
        for m in msgs:
            f.write(json.dumps(m) + "\n")


def test_spawn_command_uses_prompt_positionally() -> None:
    a = ClaudeAgent()
    assert a.spawn_command(prompt="hi there", cwd=Path("/tmp")) == ["claude", "hi there"]


def test_spawn_command_with_preassigned_session_id() -> None:
    a = ClaudeAgent()
    assert a.spawn_command(prompt="hi", cwd=Path("/tmp"), session_id="some-uuid") == [
        "claude",
        "--session-id",
        "some-uuid",
        "hi",
    ]
    assert a.spawn_command(prompt="hi", cwd=Path("/tmp"), unsafe=True, session_id="some-uuid") == [
        "claude",
        "--dangerously-skip-permissions",
        "--session-id",
        "some-uuid",
        "hi",
    ]


def test_new_session_id_is_a_uuid() -> None:
    import uuid

    a = ClaudeAgent()
    sid = a.new_session_id()
    assert sid is not None
    # `claude --session-id` rejects anything that isn't a UUID.
    assert str(uuid.UUID(sid)) == sid
    assert a.new_session_id() != sid


def test_resume_command_with_id() -> None:
    a = ClaudeAgent()
    assert a.resume_command(session_id="abc", cwd=Path("/tmp")) == ["claude", "--resume", "abc"]


def test_resume_command_without_id_uses_continue() -> None:
    a = ClaudeAgent()
    assert a.resume_command(session_id=None, cwd=Path("/tmp")) == ["claude", "--continue"]


def test_spawn_command_unsafe_prepends_bypass_flag() -> None:
    a = ClaudeAgent()
    assert a.spawn_command(prompt="hi", cwd=Path("/tmp"), unsafe=True) == [
        "claude",
        "--dangerously-skip-permissions",
        "hi",
    ]


def test_resume_command_unsafe_prepends_bypass_flag() -> None:
    a = ClaudeAgent()
    assert a.resume_command(session_id="abc", cwd=Path("/tmp"), unsafe=True) == [
        "claude",
        "--dangerously-skip-permissions",
        "--resume",
        "abc",
    ]
    assert a.resume_command(session_id=None, cwd=Path("/tmp"), unsafe=True) == [
        "claude",
        "--dangerously-skip-permissions",
        "--continue",
    ]


def test_encode_cwd_matches_claude_layout(tmp_path: Path) -> None:
    import re

    encoded = ClaudeAgent._encode_cwd(tmp_path)
    # Claude replaces any non-[A-Za-z0-9-] with `-`. Mirror that.
    expected = "-" + re.sub(r"[^A-Za-z0-9-]", "-", str(tmp_path.resolve()).strip("/"))
    assert encoded == expected
    assert ClaudeAgent._encode_cwd(Path("/")) == "-"


def test_encode_cwd_replaces_dots_and_underscores() -> None:
    # The reason it must — `.worktrees/foo_bar` is a common shape and we'd
    # otherwise look in a directory that claude never wrote to.
    assert (
        ClaudeAgent._encode_cwd(Path("/Users/me/repo/.worktrees/foo_bar"))
        == "-Users-me-repo--worktrees-foo-bar"
    )


def test_capture_session_id_returns_newest_jsonl(tmp_path: Path) -> None:
    a = ClaudeAgent()
    cwd = tmp_path / "worktree"
    cwd.mkdir()
    fake_projects = tmp_path / "claude" / "projects"
    encoded = a._encode_cwd(cwd)
    sess_dir = fake_projects / encoded
    sess_dir.mkdir(parents=True)
    old = sess_dir / "old-session.jsonl"
    new = sess_dir / "newer-session.jsonl"
    old.write_text("{}\n")
    new.write_text("{}\n")
    # Force newer mtime on `new`.
    import os

    os.utime(old, (1, 1))
    os.utime(new, (2, 2))
    with patch.object(ClaudeAgent, "projects_root", return_value=fake_projects):
        assert a.capture_session_id(cwd) == "newer-session"


def test_capture_session_id_returns_none_for_unknown_cwd(tmp_path: Path) -> None:
    a = ClaudeAgent()
    with patch.object(ClaudeAgent, "projects_root", return_value=tmp_path / "noop"):
        assert a.capture_session_id(tmp_path) is None


def test_read_transcript_counts_user_messages(tmp_path: Path) -> None:
    a = ClaudeAgent()
    cwd = tmp_path / "worktree"
    cwd.mkdir()
    encoded = a._encode_cwd(cwd)
    fake_projects = tmp_path / "claude" / "projects"
    sess = fake_projects / encoded / "sid.jsonl"
    _write_jsonl(
        sess,
        [
            {"type": "user", "message": {"content": "First user message"}},
            {"type": "assistant", "message": {"content": "First reply"}},
            {"type": "user", "message": {"content": "Second user message"}},
        ],
    )
    with patch.object(ClaudeAgent, "projects_root", return_value=fake_projects):
        summary = a.read_transcript("sid", cwd)
    assert summary.turn_count == 2
    assert summary.last_user_snippet == "Second user message"
    assert summary.last_assistant_snippet == "First reply"


def test_list_sessions_returns_newest_first(tmp_path: Path) -> None:
    a = ClaudeAgent()
    cwd = tmp_path / "worktree"
    cwd.mkdir()
    fake_projects = tmp_path / "claude" / "projects"
    encoded = a._encode_cwd(cwd)
    sess_dir = fake_projects / encoded
    sess_dir.mkdir(parents=True)
    old = sess_dir / "a.jsonl"
    new = sess_dir / "b.jsonl"
    _write_jsonl(old, [{"type": "user", "message": {"content": "old session"}}])
    _write_jsonl(new, [{"type": "user", "message": {"content": "new session"}}])
    import os

    os.utime(old, (1, 1))
    os.utime(new, (2, 2))
    with patch.object(ClaudeAgent, "projects_root", return_value=fake_projects):
        sessions = a.list_sessions(cwd)
    assert [s.session_id for s in sessions] == ["b", "a"]
    assert sessions[0].first_message_snippet == "new session"


def test_turn_count_ignores_tool_results_and_meta(tmp_path: Path) -> None:
    """claude-code stores tool results and meta injections as `type: "user"`
    records; counting them made tool-heavy sessions look like marathon
    conversations."""
    a = ClaudeAgent()
    cwd = tmp_path / "wt"
    cwd.mkdir()
    sess_dir = tmp_path / "claude" / "projects" / a._encode_cwd(cwd)
    _write_jsonl(
        sess_dir / "s1.jsonl",
        [
            {"type": "user", "message": {"role": "user", "content": "do the thing"}},
            {
                "type": "user",
                "message": {
                    "role": "user",
                    "content": [{"type": "tool_result", "tool_use_id": "x", "content": "ok"}],
                },
            },
            {
                "type": "user",
                "isMeta": True,
                "message": {"role": "user", "content": "injected context"},
            },
            {
                "type": "assistant",
                "message": {"role": "assistant", "content": [{"type": "text", "text": "done"}]},
            },
            {"type": "user", "message": {"role": "user", "content": "thanks, next step"}},
        ],
    )
    fake_root = staticmethod(lambda: tmp_path / "claude" / "projects")
    with patch.object(ClaudeAgent, "projects_root", fake_root):
        summary = a.read_transcript("s1", cwd)
    assert summary.turn_count == 2
    assert summary.last_user_snippet == "thanks, next step"


def test_headless_command_uses_print_mode() -> None:
    a = ClaudeAgent()
    assert a.headless_command(prompt="do it", cwd=Path("/tmp")) == ["claude", "-p", "do it"]
    assert a.headless_command(
        prompt="do it", cwd=Path("/tmp"), unsafe=True, session_id="some-uuid"
    ) == [
        "claude",
        "--dangerously-skip-permissions",
        "--session-id",
        "some-uuid",
        "-p",
        "do it",
    ]
