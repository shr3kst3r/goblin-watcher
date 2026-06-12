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


def test_refresh_summary_uses_workspace_cwd_for_multi_repo(tmp_path: Path) -> None:
    """Multi-repo tasks launch the agent in the workspace, so transcript reads
    must be keyed on the workspace too — not the primary worktree subdir."""
    from goblin_watcher.models import TaskRepo

    ws = tmp_path / "ws"
    task = _make_task(tmp_path).model_copy(
        update={
            "worktree_path": ws / "p",
            "workspace_path": ws,
            "secondary_repos": [
                TaskRepo(project="q", branch="b", worktree_path=ws / "q", base_branch="main")
            ],
        }
    )
    seen_cwds: list[Path] = []

    class _FakeAgent:
        def read_transcript(self, _sid: str, cwd: Path) -> TranscriptSummary:
            seen_cwds.append(cwd)
            return TranscriptSummary()

    with patch("goblin_watcher.sessions.get_agent", return_value=_FakeAgent()):
        session_module.refresh_summary(task, _make_session())
    assert seen_cwds == [ws]


def _aged(record: SessionRecord, minutes: int = 10) -> SessionRecord:
    """Push `created_at` past the reconcile grace window."""
    past = datetime.now(UTC) - timedelta(minutes=minutes)
    return record.model_copy(update={"created_at": past})


def _raw(session_id: str, path: Path, snippet: str | None = None) -> RawSession:
    return RawSession(
        session_id=session_id,
        created_at=datetime.now(UTC),
        transcript_path=path,
        first_message_snippet=snippet,
    )


def _registry_with(list_result: list[RawSession]) -> dict:
    class _FakeAgent:
        def list_sessions(self, cwd: Path) -> list[RawSession]:
            del cwd
            return list_result

    return {"claude": _FakeAgent}


def test_reconcile_rebinds_dangling_record_to_untracked_transcript(tmp_path: Path) -> None:
    """The tmux-spawn bug: a placeholder id was recorded but never reconciled,
    while the agent's real transcript sits on disk untracked. The record must
    be re-bound to the real id (keeping gw-side metadata like the label)."""
    task = _make_task(tmp_path)
    bogus = _aged(_make_session().model_copy(update={"session_id": "a29e7bceec544c9aa4a2e405"}))
    bogus = bogus.model_copy(update={"label": "kick off", "summary_updated_at": datetime.now(UTC)})
    task = task.model_copy(update={"sessions": [bogus]})

    real = _raw("e910f381-34cd-4598-8321-d58d9ff84338", tmp_path / "real.jsonl", "hello")
    with patch.object(session_module, "agent_registry", _registry_with([real])):
        out = session_module.reconcile_sessions(task)

    [record] = out.sessions
    assert record.session_id == "e910f381-34cd-4598-8321-d58d9ff84338"
    assert record.transcript_path == tmp_path / "real.jsonl"
    assert record.label == "kick off"
    # Stale summary/description must be recomputed against the real transcript.
    assert record.summary_updated_at is None
    assert record.description_updated_at is None


def test_reconcile_drops_dangling_record_with_no_candidate(tmp_path: Path) -> None:
    """Transcript garbage-collected by the agent: the store is enumerable but
    neither the recorded id nor any untracked session exists — resume can
    never work, so the record goes."""
    task = _make_task(tmp_path)
    gone = _aged(_make_session().model_copy(update={"session_id": "cleaned-up"}))
    kept = _aged(_make_session().model_copy(update={"session_id": "alive"}))
    task = task.model_copy(update={"sessions": [gone, kept]})

    alive = _raw("alive", tmp_path / "alive.jsonl")
    with patch.object(session_module, "agent_registry", _registry_with([alive])):
        out = session_module.reconcile_sessions(task)
    assert [s.session_id for s in out.sessions] == ["alive"]


def test_reconcile_leaves_records_alone_when_discovery_is_empty(tmp_path: Path) -> None:
    """No on-disk evidence (gemini, or a missing store) must never drop data."""
    task = _make_task(tmp_path)
    task = task.model_copy(update={"sessions": [_aged(_make_session())]})
    with patch.object(session_module, "agent_registry", _registry_with([])):
        out = session_module.reconcile_sessions(task)
    assert out is task


def test_reconcile_spares_records_within_grace_window(tmp_path: Path) -> None:
    """A just-spawned agent may not have written its transcript yet; its
    fresh record must not be treated as dangling."""
    task = _make_task(tmp_path)
    fresh = _make_session().model_copy(update={"session_id": "just-spawned"})
    task = task.model_copy(update={"sessions": [fresh]})

    other = _raw("older-session", tmp_path / "older.jsonl")
    with patch.object(session_module, "agent_registry", _registry_with([other])):
        out = session_module.reconcile_sessions(task)
    assert [s.session_id for s in out.sessions] == ["just-spawned"]


def test_reconcile_adopts_when_no_records_exist(tmp_path: Path) -> None:
    task = _make_task(tmp_path)
    raw = _raw("found-on-disk", tmp_path / "found.jsonl", "hi")
    with patch.object(session_module, "agent_registry", _registry_with([raw])):
        out = session_module.reconcile_sessions(task)
    assert [s.session_id for s in out.sessions] == ["found-on-disk"]


def test_adopt_orphan_sessions_scans_workspace_for_multi_repo(tmp_path: Path) -> None:
    from goblin_watcher.models import TaskRepo

    ws = tmp_path / "ws"
    task = _make_task(tmp_path).model_copy(
        update={
            "worktree_path": ws / "p",
            "workspace_path": ws,
            "secondary_repos": [
                TaskRepo(project="q", branch="b", worktree_path=ws / "q", base_branch="main")
            ],
        }
    )
    seen_cwds: list[Path] = []

    class _FakeAgent:
        def list_sessions(self, cwd: Path) -> list[RawSession]:
            seen_cwds.append(cwd)
            return []

    with patch.object(session_module, "agent_registry", {"claude": _FakeAgent}):
        session_module.adopt_orphan_sessions(task)
    assert seen_cwds == [ws]
