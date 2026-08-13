"""Tests for the LLM-description refresh path."""

from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from goblin_watcher import description, state
from goblin_watcher.config import Config, DefaultsConfig
from goblin_watcher.models import Project, SessionRecord, Task


def _make_session(
    *,
    description_updated_at: datetime | None = None,
    transcript_path: Path | None = None,
    description_text: str | None = None,
    summary: str | None = "fallback",
    label: str | None = "initial label",
) -> SessionRecord:
    now = datetime.now(UTC)
    return SessionRecord(
        agent="claude",
        session_id="sid",
        created_at=now,
        last_used_at=now,
        label=label,
        summary=summary,
        description=description_text,
        description_updated_at=description_updated_at,
        transcript_path=transcript_path,
    )


def _patch_config(monkeypatch: pytest.MonkeyPatch, **overrides: Any) -> None:
    """Force `config.load()` to return a Config with `defaults.<override>` applied."""
    defaults = DefaultsConfig(**overrides)
    monkeypatch.setattr(
        "goblin_watcher.description.config.load",
        lambda: Config(defaults=defaults),
    )


# ---------------------------------------------------------------------------
# should_refresh


def test_should_refresh_true_when_never_described(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_config(monkeypatch)
    s = _make_session(description_updated_at=None)
    assert description.should_refresh(s) is True


def test_should_refresh_false_when_within_ttl(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _patch_config(monkeypatch, description_ttl_seconds=900)
    transcript = tmp_path / "t.jsonl"
    transcript.write_text("hi")
    s = _make_session(
        description_updated_at=datetime.now(UTC),
        transcript_path=transcript,
    )
    assert description.should_refresh(s) is False


def test_should_refresh_false_when_transcript_unchanged(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _patch_config(monkeypatch, description_ttl_seconds=60)
    transcript = tmp_path / "t.jsonl"
    transcript.write_text("hi")
    # Description written *after* the transcript mtime — TTL old enough to
    # satisfy the time gate, but the transcript hasn't been touched since.
    transcript_mtime = datetime.fromtimestamp(transcript.stat().st_mtime, tz=UTC)
    desc_ts = transcript_mtime + timedelta(seconds=1)
    s = _make_session(description_updated_at=desc_ts, transcript_path=transcript)
    # Pretend "now" is far in the future so the TTL has definitely elapsed.
    far_future = desc_ts + timedelta(hours=1)
    assert description.should_refresh(s, now=far_future) is False


def test_should_refresh_true_when_stale_and_transcript_newer(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _patch_config(monkeypatch, description_ttl_seconds=60)
    transcript = tmp_path / "t.jsonl"
    transcript.write_text("hi")
    # Description ts older than ttl AND older than transcript mtime.
    s = _make_session(
        description_updated_at=datetime.now(UTC) - timedelta(hours=1),
        transcript_path=transcript,
    )
    assert description.should_refresh(s) is True


def test_should_refresh_false_when_agent_off(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_config(monkeypatch, description_agent="off")
    s = _make_session(description_updated_at=None)
    assert description.should_refresh(s) is False


def test_should_refresh_false_when_transcript_missing(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_config(monkeypatch, description_ttl_seconds=60)
    # Stale but no transcript on disk yet (e.g. tmux race).
    s = _make_session(
        description_updated_at=datetime.now(UTC) - timedelta(hours=1),
        transcript_path=None,
    )
    assert description.should_refresh(s) is False


# ---------------------------------------------------------------------------
# schedule_if_stale


def _stub_project(tmp_path: Path, name: str = "demo") -> Project:
    return Project(
        name=name,
        root=tmp_path,
        default_branch="main",
        created_at=datetime.now(UTC),
    )


def _stub_task(project: Project, sessions: list[SessionRecord]) -> Task:
    return Task(
        id="t1",
        project=project.name,
        branch="b1",
        worktree_path=project.root,
        base_branch="main",
        created_at=datetime.now(UTC),
        sessions=sessions,
    )


def test_schedule_if_stale_noop_when_fresh(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_config(monkeypatch)
    transcript = tmp_path / "t.jsonl"
    transcript.write_text("hi")
    s = _make_session(
        description_updated_at=datetime.now(UTC),
        transcript_path=transcript,
    )
    project = _stub_project(tmp_path)
    task = _stub_task(project, [s])

    called: list[Any] = []

    def fake_popen(*args: Any, **kwargs: Any) -> Any:
        called.append((args, kwargs))
        return object()

    monkeypatch.setattr("goblin_watcher.description.subprocess.Popen", fake_popen)
    assert description.schedule_if_stale(project, task, s) is False
    assert called == []


def test_schedule_if_stale_spawns_when_stale(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _patch_config(monkeypatch)
    s = _make_session(description_updated_at=None)
    project = _stub_project(tmp_path)
    task = _stub_task(project, [s])

    captured: dict[str, Any] = {}

    def fake_popen(cmd: Any, **kwargs: Any) -> Any:
        captured["cmd"] = cmd
        captured["kwargs"] = kwargs
        return object()

    monkeypatch.setattr("goblin_watcher.description.subprocess.Popen", fake_popen)
    assert description.schedule_if_stale(project, task, s) is True
    cmd = captured["cmd"]
    assert "_describe" in cmd
    assert project.name in cmd
    assert task.id in cmd
    assert s.session_id in cmd
    assert captured["kwargs"]["start_new_session"] is True


def test_schedule_if_stale_swallows_oserror(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _patch_config(monkeypatch)
    s = _make_session(description_updated_at=None)
    project = _stub_project(tmp_path)
    task = _stub_task(project, [s])

    def boom(*_a: Any, **_kw: Any) -> Any:
        raise OSError("nope")

    monkeypatch.setattr("goblin_watcher.description.subprocess.Popen", boom)
    # Must not raise.
    assert description.schedule_if_stale(project, task, s) is False


# ---------------------------------------------------------------------------
# apply (subprocess-side workflow)


@pytest.fixture
def registered_project(
    isolated_xdg: Path,
) -> Iterator[tuple[Project, Task, SessionRecord]]:
    project_root = isolated_xdg / "proj"
    project_root.mkdir()
    project = Project(
        name="demo",
        root=project_root,
        default_branch="main",
        created_at=datetime.now(UTC),
    )
    state.register_project(project)
    transcript = project_root / "t.jsonl"
    transcript.write_text("hi")
    session = _make_session(transcript_path=transcript)
    task = _stub_task(project, [session])
    state.save_task(project, task)
    yield project, task, session


def test_apply_persists_description_on_success(
    registered_project: tuple[Project, Task, SessionRecord],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    project, task, session = registered_project
    _patch_config(monkeypatch)
    monkeypatch.setattr(
        "goblin_watcher.description._invoke_llm",
        lambda _s, _task: "tightening up the picker layout for two-line rows",
    )
    code = description.apply(project.name, task.id, session.session_id)
    assert code == 0
    reloaded = state.load_task(project, task.id)
    s = reloaded.sessions[0]
    assert s.description == "tightening up the picker layout for two-line rows"
    assert s.description_updated_at is not None
    # Cheap snippet untouched.
    assert s.summary == session.summary


def test_apply_keeps_old_description_on_llm_failure(
    registered_project: tuple[Project, Task, SessionRecord],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    project, task, session = registered_project
    _patch_config(monkeypatch)
    # Seed an existing description so we can verify it's preserved.
    existing = "previously generated description"
    seeded = session.model_copy(
        update={
            "description": existing,
            "description_updated_at": datetime.now(UTC) - timedelta(hours=1),
        }
    )
    state.save_task(project, task.model_copy(update={"sessions": [seeded]}))

    monkeypatch.setattr("goblin_watcher.description._invoke_llm", lambda _s, _task: None)
    code = description.apply(project.name, task.id, session.session_id)
    assert code == 0
    reloaded = state.load_task(project, task.id)
    assert reloaded.sessions[0].description == existing


def test_apply_noop_when_session_missing(
    registered_project: tuple[Project, Task, SessionRecord],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    project, task, _session = registered_project
    _patch_config(monkeypatch)
    calls: list[Any] = []
    monkeypatch.setattr(
        "goblin_watcher.description._invoke_llm",
        lambda s, _task: calls.append(s) or "X",
    )
    code = description.apply(project.name, task.id, "nonexistent")
    assert code == 0
    assert calls == []  # never reached the LLM


def test_apply_noop_when_freshness_gate_blocks(
    registered_project: tuple[Project, Task, SessionRecord],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    project, task, session = registered_project
    _patch_config(monkeypatch)
    # Mark already-described very recently → should_refresh False.
    seeded = session.model_copy(
        update={
            "description": "already there",
            "description_updated_at": datetime.now(UTC),
        }
    )
    state.save_task(project, task.model_copy(update={"sessions": [seeded]}))

    calls: list[Any] = []
    monkeypatch.setattr(
        "goblin_watcher.description._invoke_llm",
        lambda s, _task: calls.append(s) or "X",
    )
    code = description.apply(project.name, task.id, session.session_id)
    assert code == 0
    assert calls == []


# ---------------------------------------------------------------------------
# claude/codex subprocess wrappers


def test_run_claude_returns_none_when_binary_missing(monkeypatch: pytest.MonkeyPatch) -> None:
    def boom(*_a: Any, **_kw: Any) -> Any:
        raise FileNotFoundError("no claude")

    monkeypatch.setattr("goblin_watcher.description.subprocess.run", boom)
    assert description._run_claude("p", "claude-haiku-4-5") is None


def test_run_claude_returns_none_on_nonzero_exit(monkeypatch: pytest.MonkeyPatch) -> None:
    class _Proc:
        returncode = 2
        stdout = ""
        stderr = "bang"

    monkeypatch.setattr("goblin_watcher.description.subprocess.run", lambda *a, **kw: _Proc())
    assert description._run_claude("p", "claude-haiku-4-5") is None


def test_run_claude_returns_stdout_on_success(monkeypatch: pytest.MonkeyPatch) -> None:
    class _Proc:
        returncode = 0
        stdout = "neat one-liner"
        stderr = ""

    monkeypatch.setattr("goblin_watcher.description.subprocess.run", lambda *a, **kw: _Proc())
    assert description._run_claude("p", "claude-haiku-4-5") == "neat one-liner"


def test_run_llm_dispatches_to_the_configured_agent(monkeypatch: pytest.MonkeyPatch) -> None:
    """The shared cheap-model call `classify` reuses (ADR 0011)."""
    _patch_config(monkeypatch, description_agent="codex", description_model="gpt-5-codex")
    seen: dict[str, Any] = {}

    def fake_run_codex(prompt: str, model: str, timeout: int = 0) -> str | None:
        seen.update(prompt=prompt, model=model, timeout=timeout)
        return "raw stdout"

    monkeypatch.setattr("goblin_watcher.description._run_codex", fake_run_codex)
    # Raw, not cleaned: each caller shapes the output for itself.
    assert description.run_llm("classify this", timeout=5) == "raw stdout"
    assert seen == {"prompt": "classify this", "model": "gpt-5-codex", "timeout": 5}


def test_run_llm_returns_none_when_agent_off(monkeypatch: pytest.MonkeyPatch) -> None:
    """`description_agent = "off"` turns off every model call gw makes."""
    _patch_config(monkeypatch, description_agent="off")

    def boom(*_a: Any, **_kw: Any) -> Any:
        raise AssertionError("should not have been called")

    monkeypatch.setattr("goblin_watcher.description._run_claude", boom)
    assert description.run_llm("anything") is None


def test_clean_strips_banner_and_quotes() -> None:
    raw = 'banner: warming up\n\n  "the real answer"  \n'
    assert description._clean(raw) == "the real answer"


def test_clean_strips_trailing_period() -> None:
    raw = "Adding test coverage and refactoring the parser."
    assert description._clean(raw) == "Adding test coverage and refactoring the parser"


def test_clean_collapses_multiline_paragraph() -> None:
    # Multi-line output within a single paragraph (no blank line) gets
    # collapsed onto one line so the display layer can wrap it itself.
    raw = "Refactoring the loader.\nAdding tests for the new code path."
    assert description._clean(raw) == ("Refactoring the loader. Adding tests for the new code path")


def test_clean_caps_overly_long_output() -> None:
    raw = "x" * 1000
    cleaned = description._clean(raw)
    assert cleaned is not None
    assert len(cleaned) <= 320
    assert cleaned.endswith("…")


def test_clean_handles_empty() -> None:
    assert description._clean(None) is None
    assert description._clean("   \n  ") is None


# ---------------------------------------------------------------------------
# display_text


def test_display_text_prefers_description() -> None:
    s = _make_session(description_text="LLM desc", summary="snippet", label="label")
    assert description.display_text(s) == "LLM desc"


def test_display_text_falls_back_to_summary() -> None:
    s = _make_session(description_text=None, summary="snippet", label="label")
    assert description.display_text(s) == "snippet"


def test_display_text_falls_back_to_label() -> None:
    s = _make_session(description_text=None, summary=None, label="label")
    assert description.display_text(s) == "label"


def test_display_text_placeholder() -> None:
    s = _make_session(description_text=None, summary=None, label=None)
    assert description.display_text(s) == "(no summary yet)"


def test_display_text_collapses_whitespace() -> None:
    # A description that snuck in a newline (defensive: the LLM is told not
    # to, but if it does we shouldn't break picker rows or table cells).
    s = _make_session(description_text="first sentence.\n  second sentence")
    assert description.display_text(s) == "first sentence. second sentence"


# ---------------------------------------------------------------------------
# wrap_for_tree


def test_wrap_for_tree_short_text_stays_one_line() -> None:
    assert description.wrap_for_tree("short text", indent_cols=8, width=72) == "short text"


def test_wrap_for_tree_wraps_with_hanging_indent() -> None:
    text = (
        "Refactoring the description display so longer summaries wrap with a "
        "hanging indent under the description start. Continuation lines should "
        "not start at column zero."
    )
    wrapped = description.wrap_for_tree(text, indent_cols=8, width=50)
    lines = wrapped.split("\n")
    assert len(lines) >= 2
    # First line un-indented so the caller can prepend the agent badge.
    assert not lines[0].startswith(" ")
    # Every subsequent line starts with the 8-space hanging indent.
    for line in lines[1:]:
        assert line.startswith("        ")


def test_wrap_for_tree_empty() -> None:
    assert description.wrap_for_tree("", indent_cols=8, width=72) == ""


# ---------------------------------------------------------------------------
# Rich prompt construction


def test_build_prompt_uses_transcript_when_available() -> None:
    from goblin_watcher.agents.base import TranscriptSummary

    parsed = TranscriptSummary(
        turn_count=7,
        first_user_snippet="set up the new pipeline",
        recent_user_snippets=["debug the failing parquet read", "rerun the unit tests"],
        recent_assistant_snippets=["ran the tests; one failure in test_loader"],
    )
    s = _make_session(label="initial label", summary="last snippet")
    prompt = description._build_prompt(s, parsed)
    assert "set up the new pipeline" in prompt
    assert "debug the failing parquet read" in prompt
    assert "rerun the unit tests" in prompt
    assert "ran the tests; one failure in test_loader" in prompt
    assert "Turn count: 7" in prompt


def test_build_prompt_falls_back_when_transcript_empty() -> None:
    from goblin_watcher.agents.base import TranscriptSummary

    parsed = TranscriptSummary()  # codex/gemini — nothing parsed
    s = _make_session(label="initial label", summary="cheap snippet")
    prompt = description._build_prompt(s, parsed)
    # `first_user` falls back to the label; recent_users falls back to the
    # cheap snippet so we don't ask the LLM to characterize nothing.
    assert "initial label" in prompt
    assert "cheap snippet" in prompt


def test_build_prompt_handles_none_parsed() -> None:
    s = _make_session(label="initial label", summary="cheap snippet")
    prompt = description._build_prompt(s, None)
    assert "initial label" in prompt
    assert "cheap snippet" in prompt


# ---------------------------------------------------------------------------
# Claude transcript parser populates the rich snippet fields


# ---------------------------------------------------------------------------
# Full-transcript prompt path


def test_clamp_transcript_head_and_tail() -> None:
    text = "A" * 100 + "B" * 100 + "C" * 100
    clamped = description._clamp_transcript(text, max_chars=80)
    # Start with A's (head), end with C's (tail), marker in the middle.
    assert clamped.startswith("A")
    assert clamped.endswith("C")
    assert "transcript truncated" in clamped
    assert len(clamped) <= 80 + len("\n\n[… transcript truncated for length …]\n\n")


def test_clamp_transcript_noop_when_under_cap() -> None:
    text = "hello world"
    assert description._clamp_transcript(text, max_chars=1000) == text


def test_build_full_prompt_contains_transcript() -> None:
    s = _make_session(label="implement feature X")
    transcript = "[user]\nhi there\n\n[assistant]\nhello back"
    prompt = description._build_full_prompt(s, transcript, max_chars=10_000)
    assert "implement feature X" in prompt
    assert "[user]" in prompt
    assert "hi there" in prompt
    assert "[assistant]" in prompt
    assert "hello back" in prompt


def test_invoke_llm_uses_full_transcript_when_available(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """When the agent renders a full transcript, the prompt path uses it."""
    _patch_config(monkeypatch)

    rendered: dict[str, str] = {}

    def fake_run_claude(prompt: str, _model: str, _timeout: int = 0) -> str | None:
        rendered["prompt"] = prompt
        return "described it"

    monkeypatch.setattr("goblin_watcher.description._run_claude", fake_run_claude)

    # Stand up a project/task/session with a real claude jsonl on disk so the
    # agent's `render_transcript` returns content.
    from goblin_watcher.agents.claude import ClaudeAgent

    worktree = tmp_path / "wt"
    worktree.mkdir()
    encoded = ClaudeAgent._encode_cwd(worktree)
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    proj_dir = home / ".claude" / "projects" / encoded
    proj_dir.mkdir(parents=True)
    (proj_dir / "sid.jsonl").write_text(
        '{"type":"user","message":{"content":"please refactor the loader"}}\n'
        '{"type":"assistant","message":{"content":"refactored, here are the diffs"}}\n'
    )

    project = _stub_project(worktree, name="full")
    task = Task(
        id="t1",
        project=project.name,
        branch="b1",
        worktree_path=worktree,
        base_branch="main",
        created_at=datetime.now(UTC),
        sessions=[],
    )
    session = SessionRecord(
        agent="claude",
        session_id="sid",
        created_at=datetime.now(UTC),
        last_used_at=datetime.now(UTC),
        label="initial label",
    )

    result = description._invoke_llm(session, task)
    assert result == "described it"
    assert "please refactor the loader" in rendered["prompt"]
    assert "refactored, here are the diffs" in rendered["prompt"]
    assert "[user]" in rendered["prompt"]
    assert "Full transcript" in rendered["prompt"]


def test_invoke_llm_falls_back_to_snippets_when_render_empty(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """No transcript on disk → fall back to the snippet-based prompt."""
    _patch_config(monkeypatch)
    captured: dict[str, str] = {}

    def fake_run_claude(prompt: str, _model: str, _timeout: int = 0) -> str | None:
        captured["prompt"] = prompt
        return "ok"

    monkeypatch.setattr("goblin_watcher.description._run_claude", fake_run_claude)
    # Point HOME at an empty dir so ClaudeAgent.render_transcript returns None.
    monkeypatch.setenv("HOME", str(tmp_path / "empty_home"))

    project = _stub_project(tmp_path, name="fallback")
    task = Task(
        id="t1",
        project=project.name,
        branch="b1",
        worktree_path=tmp_path,
        base_branch="main",
        created_at=datetime.now(UTC),
        sessions=[],
    )
    session = SessionRecord(
        agent="claude",
        session_id="sid",
        created_at=datetime.now(UTC),
        last_used_at=datetime.now(UTC),
        label="initial label",
        summary="cheap snippet",
    )
    description._invoke_llm(session, task)
    # The snippet-based template uses "Turn count:" and "First user message"
    # neither of which appear in the full-transcript template.
    assert "Full transcript" not in captured["prompt"]
    assert "Turn count" in captured["prompt"]
    assert "initial label" in captured["prompt"]


# ---------------------------------------------------------------------------
# Claude full-transcript renderer


def test_claude_render_transcript_labels_messages(tmp_path: Path) -> None:
    from goblin_watcher.agents.claude import _render_transcript

    transcript = tmp_path / "abc.jsonl"
    transcript.write_text(
        '{"type":"user","message":{"content":"first prompt"}}\n'
        '{"type":"assistant","message":{"content":"first reply"}}\n'
        '{"type":"system","message":{"content":"ignored"}}\n'
    )
    rendered = _render_transcript(transcript)
    assert rendered is not None
    assert "[user]\nfirst prompt" in rendered
    assert "[assistant]\nfirst reply" in rendered
    assert "ignored" not in rendered


def test_claude_render_transcript_returns_none_when_missing(tmp_path: Path) -> None:
    from goblin_watcher.agents.claude import _render_transcript

    assert _render_transcript(tmp_path / "does-not-exist.jsonl") is None


def test_claude_parser_collects_first_and_recent_snippets(tmp_path: Path) -> None:
    from goblin_watcher.agents.claude import _parse_transcript

    transcript = tmp_path / "abc.jsonl"
    # 1 first user message, 4 user / 4 assistant messages total — we keep the
    # last 3 of each.
    lines = []
    for body in ["set up pipeline", "second user msg", "third user msg", "fourth user msg"]:
        lines.append(f'{{"type":"user","message":{{"content":"{body}"}}}}')
    for body in [
        "first assistant reply",
        "second assistant reply",
        "third assistant reply",
        "fourth assistant reply",
    ]:
        lines.append(f'{{"type":"assistant","message":{{"content":"{body}"}}}}')
    transcript.write_text("\n".join(lines) + "\n")

    summary = _parse_transcript(transcript)
    assert summary.turn_count == 4
    assert summary.first_user_snippet == "set up pipeline"
    assert summary.recent_user_snippets == [
        "second user msg",
        "third user msg",
        "fourth user msg",
    ]
    assert summary.recent_assistant_snippets == [
        "second assistant reply",
        "third assistant reply",
        "fourth assistant reply",
    ]
