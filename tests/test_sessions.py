from datetime import UTC, datetime, timedelta
from pathlib import Path
from unittest.mock import patch

import pytest

from goblin_watcher import sessions as session_module
from goblin_watcher.agents.base import RawSession, TranscriptSummary
from goblin_watcher.agents.claude import ClaudeAgent
from goblin_watcher.models import SessionRecord, Task


def _make_task(tmp_path: Path) -> Task:
    return Task(
        id="t1",
        project="p",
        branch="b",
        worktree_path=tmp_path,
        base_branch="main",
        created_at=datetime.now(UTC),
    )


def _make_session(agent: str = "claude") -> SessionRecord:
    now = datetime.now(UTC)
    return SessionRecord(
        agent=agent,
        session_id="sid",
        created_at=now,
        last_used_at=now,
    )


def test_upsert_adds_new_session(tmp_path: Path) -> None:
    task = _make_task(tmp_path)
    s = _make_session()
    updated = session_module.upsert(task, s)
    assert len(updated.sessions) == 1
    assert updated.sessions[0].session_id == "sid"


def test_upsert_merges_matching_session(tmp_path: Path) -> None:
    task = _make_task(tmp_path)
    s1 = _make_session().model_copy(update={"summary": "first"})
    task = session_module.upsert(task, s1)
    s2 = _make_session().model_copy(update={"summary": "second", "turn_count": 5})
    task = session_module.upsert(task, s2)
    assert len(task.sessions) == 1
    assert task.sessions[0].summary == "second"
    assert task.sessions[0].turn_count == 5


def test_upsert_keeps_other_agents_distinct(tmp_path: Path) -> None:
    task = _make_task(tmp_path)
    task = session_module.upsert(task, _make_session("claude"))
    task = session_module.upsert(task, _make_session("codex"))
    assert {s.agent for s in task.sessions} == {"claude", "codex"}


def test_refresh_summary_pulls_from_agent_transcript(tmp_path: Path) -> None:
    task = _make_task(tmp_path)
    s = _make_session()
    fake_summary = TranscriptSummary(
        turn_count=4,
        last_user_snippet="latest user thing",
        last_assistant_snippet=None,
        transcript_path=tmp_path / "x.jsonl",
    )

    class _FakeAgent:
        def read_transcript(self, *_a, **_kw) -> TranscriptSummary:
            return fake_summary

    with patch("goblin_watcher.sessions.get_agent", return_value=_FakeAgent()):
        refreshed = session_module.refresh_summary(task, s)
    assert refreshed.summary == "latest user thing"
    assert refreshed.turn_count == 4
    assert refreshed.summary_updated_at is not None


def test_refresh_if_stale_skips_fresh_summary(tmp_path: Path) -> None:
    task = _make_task(tmp_path)
    fresh = _make_session().model_copy(
        update={"summary": "kept", "summary_updated_at": datetime.now(UTC)}
    )
    with patch("goblin_watcher.sessions.get_agent") as mocked:
        out = session_module.refresh_if_stale(task, fresh)
        mocked.assert_not_called()
    assert out.summary == "kept"


def test_adopt_orphan_sessions_noop_when_sessions_exist(tmp_path: Path) -> None:
    task = _make_task(tmp_path)
    task = session_module.upsert(task, _make_session())
    out = session_module.adopt_orphan_sessions(task)
    assert out is task


def test_adopt_orphan_sessions_noop_when_no_transcripts(tmp_path: Path) -> None:
    task = _make_task(tmp_path)
    out = session_module.adopt_orphan_sessions(task)
    assert out is task
    assert out.sessions == []


def test_adopt_orphan_sessions_picks_up_claude_transcripts(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))

    worktree = tmp_path / "worktree"
    worktree.mkdir()
    encoded = ClaudeAgent._encode_cwd(worktree)
    proj_dir = home / ".claude" / "projects" / encoded
    proj_dir.mkdir(parents=True)
    (proj_dir / "abc-123.jsonl").write_text(
        '{"type":"user","message":{"content":"hello there"}}\n'
        '{"type":"assistant","message":{"content":"hi back"}}\n'
    )

    task = _make_task(worktree)
    updated = session_module.adopt_orphan_sessions(task)
    assert len(updated.sessions) == 1
    s = updated.sessions[0]
    assert s.agent == "claude"
    assert s.session_id == "abc-123"
    assert s.label == "hello there"


def test_adopt_orphan_sessions_skips_agents_already_tracked(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    raw_sessions_called: list[Path] = []

    def fake_list_sessions(self: object, cwd: Path) -> list[RawSession]:
        raw_sessions_called.append(cwd)
        return []

    monkeypatch.setattr(ClaudeAgent, "list_sessions", fake_list_sessions)
    task = _make_task(tmp_path)
    task = session_module.upsert(task, _make_session())
    out = session_module.adopt_orphan_sessions(task)
    assert out is task
    assert raw_sessions_called == []


def test_refresh_if_stale_refreshes_old_summary(tmp_path: Path) -> None:
    task = _make_task(tmp_path)
    stale = _make_session().model_copy(
        update={
            "summary": "old",
            "summary_updated_at": datetime.now(UTC) - timedelta(minutes=5),
        }
    )

    class _FakeAgent:
        def read_transcript(self, *_a, **_kw) -> TranscriptSummary:
            return TranscriptSummary(turn_count=1, last_user_snippet="new!")

    with patch("goblin_watcher.sessions.get_agent", return_value=_FakeAgent()):
        out = session_module.refresh_if_stale(task, stale)
    assert out.summary == "new!"
