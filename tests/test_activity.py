"""Transcript-shape classification: working / needs-you / done / idle (ADR 0010).

Transcripts are hand-written JSONL fixtures — no agent binary is ever invoked.
"""

from __future__ import annotations

import json
import os
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from goblin_watcher import activity
from goblin_watcher.agents._tail import TAIL_WINDOW_BYTES, tail_records, tail_text
from goblin_watcher.agents.claude import ClaudeAgent
from goblin_watcher.agents.codex import CodexAgent
from goblin_watcher.models import SessionRecord

# ---------------------------------------------------------------------------
# Fixtures / builders.


def _write(path: Path, records: list[dict]) -> Path:
    path.write_text("".join(json.dumps(r) + "\n" for r in records))
    return path


def _session(path: Path | None, agent: str = "claude") -> SessionRecord:
    now = datetime.now(UTC)
    return SessionRecord(
        agent=agent,  # type: ignore[arg-type]
        session_id="s1",
        created_at=now,
        last_used_at=now,
        transcript_path=path,
    )


def _age(path: Path, seconds: float) -> None:
    stamp = datetime.now(UTC).timestamp() - seconds
    os.utime(path, (stamp, stamp))


def _assistant(*blocks: dict) -> dict:
    return {"type": "assistant", "message": {"role": "assistant", "content": list(blocks)}}


def _text(text: str) -> dict:
    return {"type": "text", "text": text}


def _tool_use(tool_id: str) -> dict:
    return {"type": "tool_use", "id": tool_id, "name": "Bash", "input": {}}


def _tool_result(tool_id: str) -> dict:
    return {
        "type": "user",
        "message": {"role": "user", "content": [{"type": "tool_result", "tool_use_id": tool_id}]},
    }


def _user(text: str) -> dict:
    return {"type": "user", "message": {"role": "user", "content": text}}


def _classify(path: Path | None, agent: str = "claude", **kw: object) -> activity.Activity:
    return activity.classify(
        _session(path, agent),
        active_seconds=kw.pop("active_seconds", 120),  # type: ignore[arg-type]
        stalled_after=kw.pop("stalled_after", 900),  # type: ignore[arg-type]
        **kw,  # type: ignore[arg-type]
    )


# ---------------------------------------------------------------------------
# claude: the three states the issue names.


def test_unmatched_tool_use_is_working(tmp_path: Path) -> None:
    path = _write(tmp_path / "t.jsonl", [_assistant(_text("Running it"), _tool_use("toolu_1"))])
    act = _classify(path)
    assert act.state == "working"
    assert act.source == "transcript"


def test_a_tool_call_that_came_back_is_not_working(tmp_path: Path) -> None:
    path = _write(
        tmp_path / "t.jsonl",
        [
            _assistant(_tool_use("toolu_1")),
            _tool_result("toolu_1"),
            _assistant(_text("Tests pass.")),
        ],
    )
    assert _classify(path).state == "done"


def test_a_tool_result_is_not_mistaken_for_a_human_turn(tmp_path: Path) -> None:
    """Tool results are stored with `type: "user"`; counting one as a human
    turn would make a busy agent look like it was waiting on you."""
    path = _write(tmp_path / "t.jsonl", [_assistant(_tool_use("toolu_1")), _tool_result("toolu_1")])
    act = _classify(path)
    # Nothing said after the result, nothing pending — mtime decides.
    assert act.state == "working"
    assert act.source == "mtime"


def test_turn_ending_on_a_question_needs_you(tmp_path: Path) -> None:
    path = _write(
        tmp_path / "t.jsonl",
        [_assistant(_text("Two options here.\n\nWhich one do you want?"))],
    )
    act = _classify(path)
    assert act.state == "needs-you"
    assert act.needs_you
    assert act.detail is not None and act.detail.endswith("?")


def test_completed_turn_with_no_question_is_done(tmp_path: Path) -> None:
    path = _write(tmp_path / "t.jsonl", [_assistant(_text("Fixed it. Tests are green."))])
    assert _classify(path).state == "done"


def test_a_question_the_agent_then_answered_itself_is_done(tmp_path: Path) -> None:
    """The heuristic reads the *last* line, not any '?' in the message."""
    path = _write(
        tmp_path / "t.jsonl",
        [_assistant(_text("Should I rebase first? Yes — doing that now, then landing it."))],
    )
    assert _classify(path).state == "done"


def test_a_human_turn_with_no_reply_yet_is_working(tmp_path: Path) -> None:
    path = _write(tmp_path / "t.jsonl", [_assistant(_text("Which branch?")), _user("the main one")])
    act = _classify(path)
    assert act.state == "working"
    # The answer supersedes the question: it must not still read as blocked.
    assert act.detail is None


def test_meta_records_are_not_human_turns(tmp_path: Path) -> None:
    path = _write(
        tmp_path / "t.jsonl",
        [
            _assistant(_text("Anything else you want changed?")),
            {"type": "user", "isMeta": True, "message": {"role": "user", "content": "<system>"}},
        ],
    )
    assert _classify(path).state == "needs-you"


def test_sidechain_records_are_ignored(tmp_path: Path) -> None:
    """A subagent's own calls must not be counted; the parent's pending `Task`
    call already says the main thread is working."""
    records = [
        _assistant(_text("Done — all four checks pass.")),
        {**_assistant(_tool_use("toolu_sub")), "isSidechain": True},
    ]
    path = _write(tmp_path / "t.jsonl", records)
    assert _classify(path).state == "done"


# ---------------------------------------------------------------------------
# Degradation: mtime fallback, stalls, absent transcripts.


def test_no_transcript_path_is_unknown() -> None:
    act = _classify(None)
    assert act.state == "unknown"
    assert act.source == "none"


def test_missing_transcript_file_is_unknown(tmp_path: Path) -> None:
    assert _classify(tmp_path / "nope.jsonl").state == "unknown"


def test_unparseable_transcript_falls_back_to_mtime(tmp_path: Path) -> None:
    path = tmp_path / "t.jsonl"
    path.write_text("{}\n")  # a record with no role at all
    assert _classify(path).state == "working"
    _age(path, 3600)
    assert _classify(path).state == "idle"


def test_agents_without_readable_transcripts_use_mtime(tmp_path: Path) -> None:
    path = _write(tmp_path / "t.jsonl", [_assistant(_text("Which one?"))])
    for agent in ("gemini", "antigravity", "managed"):
        act = _classify(path, agent)
        assert act.source == "mtime", agent
        assert act.state == "working", agent


def test_an_unknown_agent_name_degrades_rather_than_raising(tmp_path: Path) -> None:
    path = _write(tmp_path / "t.jsonl", [_assistant(_text("Which one?"))])
    act = activity.classify(
        _session(path).model_copy(update={"agent": "nonesuch"}),
        active_seconds=120,
        stalled_after=900,
    )
    assert act.source == "mtime"


def test_a_session_killed_mid_tool_call_stops_reading_as_working(tmp_path: Path) -> None:
    """Otherwise a dead session sits in `gw status --active` forever."""
    path = _write(tmp_path / "t.jsonl", [_assistant(_tool_use("toolu_1"))])
    assert _classify(path).state == "working"
    _age(path, 1000)
    act = _classify(path, stalled_after=900)
    assert act.state == "idle"
    assert act.source == "mtime"


def test_stall_window_does_not_cut_a_long_running_tool_call_short(tmp_path: Path) -> None:
    path = _write(tmp_path / "t.jsonl", [_assistant(_tool_use("toolu_1"))])
    _age(path, 600)
    assert _classify(path, stalled_after=900).state == "working"


def test_classify_reads_thresholds_from_config_when_not_given(tmp_path: Path) -> None:
    path = tmp_path / "t.jsonl"
    path.write_text("{}\n")
    _age(path, 300)
    assert activity.classify(_session(path)).state == "idle"


# ---------------------------------------------------------------------------
# Edge tokens: the discipline behind once-per-transition notification.


def test_edge_token_changes_when_the_question_changes(tmp_path: Path) -> None:
    first = _classify(_write(tmp_path / "a.jsonl", [_assistant(_text("Rebase or merge?"))]))
    second = _classify(_write(tmp_path / "b.jsonl", [_assistant(_text("Squash or not?"))]))
    assert first.state == second.state == "needs-you"
    assert first.edge_token != second.edge_token


def test_edge_token_is_stable_for_an_unchanged_transcript(tmp_path: Path) -> None:
    path = _write(tmp_path / "t.jsonl", [_assistant(_text("All set."))])
    assert _classify(path).edge_token == _classify(path).edge_token


def test_terminal_states_are_the_ones_worth_announcing(tmp_path: Path) -> None:
    assert set(activity.TERMINAL_STATES) == {"needs-you", "done", "idle"}
    path = _write(tmp_path / "t.jsonl", [_assistant(_tool_use("toolu_1"))])
    assert not _classify(path).is_terminal


# ---------------------------------------------------------------------------
# Question heuristic.


@pytest.mark.parametrize(
    "text",
    [
        "Which approach do you want?",
        "**Ready to land — should I merge?**",
        "Two options:\n\n- rebase\n- merge\n\nWhich?",
        "`gw pr open` next?",
        "I've stopped here. Let me know how you'd like to proceed.",
        "Both work. Your call.",
    ],
)
def test_texts_that_hand_control_back(text: str) -> None:
    assert activity.ends_on_question(text)


@pytest.mark.parametrize(
    "text",
    [
        "Landed in abc1234.",
        "Was it broken? It was — fixed now.",
        "Done. Tests pass and the PR is open.",
        "",
        "   \n\n  ",
    ],
)
def test_texts_that_do_not(text: str) -> None:
    assert not activity.ends_on_question(text)


# ---------------------------------------------------------------------------
# Bounded tail reads.


def test_tail_read_is_bounded_but_still_classifies(tmp_path: Path) -> None:
    """A transcript far larger than the window still classifies off its end."""
    path = tmp_path / "big.jsonl"
    filler = json.dumps(_assistant(_text("x" * 2000))) + "\n"
    with path.open("w") as f:
        while path.stat().st_size < TAIL_WINDOW_BYTES * 2:
            f.write(filler)
            f.flush()
        f.write(json.dumps(_assistant(_text("Ship it or hold?"))) + "\n")
    records = tail_records(path)
    assert 0 < len(records) < path.stat().st_size // len(filler)
    assert _classify(path).state == "needs-you"


def test_tail_read_drops_the_partial_first_line(tmp_path: Path) -> None:
    path = tmp_path / "t.jsonl"
    path.write_text('{"type": "assistant", "message": {"content": []}}\n{"type": "x"}\n')
    assert [r.get("type") for r in tail_records(path, window_bytes=20)] == ["x"]


def test_tail_read_of_a_missing_file_is_empty(tmp_path: Path) -> None:
    assert tail_records(tmp_path / "absent.jsonl") == []


def test_a_half_written_final_record_is_skipped(tmp_path: Path) -> None:
    """A transcript being appended to right now can end mid-record."""
    path = tmp_path / "t.jsonl"
    path.write_text(json.dumps(_assistant(_text("All good."))) + '\n{"type": "assis')
    assert _classify(path).state == "done"


def test_tail_text_keeps_the_end() -> None:
    assert tail_text("abcdef", max_len=4) == "…def"
    assert tail_text("abc", max_len=10) == "abc"


# ---------------------------------------------------------------------------
# codex rollouts.


def _event(kind: str, **payload: object) -> dict:
    return {"type": "event_msg", "payload": {"type": kind, **payload}}


def _item(kind: str, call_id: str) -> dict:
    return {"type": "response_item", "payload": {"type": kind, "call_id": call_id}}


def test_codex_open_turn_is_working(tmp_path: Path) -> None:
    path = _write(
        tmp_path / "r.jsonl",
        [_event("user_message", message="go"), _event("task_started")],
    )
    act = _classify(path, "codex")
    assert act.state == "working"
    assert act.source == "transcript"


def test_codex_completed_turn_classifies_its_last_message(tmp_path: Path) -> None:
    path = _write(
        tmp_path / "r.jsonl",
        [
            _event("task_started"),
            _event("agent_message", message="Done — want me to open the PR?"),
            _event("task_complete"),
        ],
    )
    assert _classify(path, "codex").state == "needs-you"

    path = _write(
        tmp_path / "r2.jsonl",
        [
            _event("task_started"),
            _event("agent_message", message="Opened the PR."),
            _event("task_complete"),
        ],
    )
    assert _classify(path, "codex").state == "done"


def test_codex_unmatched_function_call_is_working(tmp_path: Path) -> None:
    path = _write(
        tmp_path / "r.jsonl",
        [
            _event("task_started"),
            _event("agent_message", message="Checking."),
            _event("task_complete"),
            _item("function_call", "call_1"),
        ],
    )
    assert _classify(path, "codex").state == "working"


def test_codex_matched_function_call_is_not(tmp_path: Path) -> None:
    path = _write(
        tmp_path / "r.jsonl",
        [
            _item("function_call", "call_1"),
            _item("function_call_output", "call_1"),
            _event("agent_message", message="All done."),
            _event("task_complete"),
        ],
    )
    assert _classify(path, "codex").state == "done"


def test_codex_rollout_with_no_recognisable_markers_falls_back(tmp_path: Path) -> None:
    path = _write(tmp_path / "r.jsonl", [{"type": "session_meta", "payload": {"id": "x"}}])
    assert _classify(path, "codex").source == "mtime"


def test_read_tail_is_on_every_registered_agent() -> None:
    """The protocol method, not a claude/codex special case."""
    from goblin_watcher.agents import registry

    for name, cls in registry.items():
        assert hasattr(cls, "read_tail"), name


def test_read_tail_returns_none_for_agents_without_transcripts(tmp_path: Path) -> None:
    from goblin_watcher.agents.antigravity import AntigravityAgent
    from goblin_watcher.agents.gemini import GeminiAgent
    from goblin_watcher.agents.managed import ManagedAgent

    path = _write(tmp_path / "t.jsonl", [_assistant(_text("hi"))])
    assert GeminiAgent().read_tail(path) is None
    assert AntigravityAgent().read_tail(path) is None
    assert ManagedAgent().read_tail(path) is None
    assert ClaudeAgent().read_tail(path) is not None
    assert CodexAgent().read_tail(tmp_path / "absent.jsonl") is None


def test_a_transcript_older_than_the_stall_window_still_reports_needs_you(
    tmp_path: Path,
) -> None:
    """The stall window only overrides a *working* claim. A question asked an
    hour ago is still a question waiting on you."""
    path = _write(tmp_path / "t.jsonl", [_assistant(_text("Merge or rebase?"))])
    _age(path, timedelta(hours=1).total_seconds())
    assert _classify(path).state == "needs-you"
