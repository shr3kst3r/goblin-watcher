"""Tests for advisory ticket classification (ADR 0011)."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

from goblin_watcher import classify
from goblin_watcher.config import Config, DefaultsConfig
from goblin_watcher.models import GhIssue, LinearComment, LinearIssue, Task
from goblin_watcher.modes import ModeSpec


def _task(
    *,
    issue: GhIssue | None = None,
    linear: LinearIssue | None = None,
) -> Task:
    return Task(
        id="gh-42",
        project="alpha",
        github_issue=issue,
        linear=linear,
        branch="gh-42-add-rate-limit",
        worktree_path=Path("/tmp/wt"),
        base_branch="main",
        created_at=datetime.now(UTC),
    )


def _issue(body: str | None = "We need a token bucket.") -> GhIssue:
    return GhIssue(
        number=42,
        repo="org/repo",
        title="Add rate limit",
        body=body,
        state="OPEN",
        url="https://github.com/org/repo/issues/42",
        labels=["enhancement"],
    )


def _patch_config(monkeypatch: pytest.MonkeyPatch, **overrides: Any) -> None:
    defaults = DefaultsConfig(**overrides)
    monkeypatch.setattr("goblin_watcher.classify.config.load", lambda: Config(defaults=defaults))


def _patch_llm(monkeypatch: pytest.MonkeyPatch, answer: str | None) -> list[dict[str, Any]]:
    """Stand in for the cheap model, recording every call. Never spawns anything."""
    calls: list[dict[str, Any]] = []

    def fake_run_llm(prompt: str, *, timeout: int = 0) -> str | None:
        calls.append({"prompt": prompt, "timeout": timeout})
        return answer

    monkeypatch.setattr("goblin_watcher.classify.description.run_llm", fake_run_llm)
    return calls


# ---------------------------------------------------------------------------
# ticket_text


def test_ticket_text_none_without_a_tracking_item() -> None:
    """`--branch` / `--dir` / `--pr` tasks have nothing to classify."""
    assert classify.ticket_text(_task()) is None


def test_ticket_text_includes_the_github_issue() -> None:
    text = classify.ticket_text(_task(issue=_issue()))
    assert text is not None
    assert text.startswith("org/repo#42: Add rate limit")
    assert "We need a token bucket." in text
    # Rendered through the seed prompt's own block, so labels come along.
    assert "enhancement" in text


def test_ticket_text_includes_linear_description_and_comments() -> None:
    linear = LinearIssue(
        id="uuid",
        identifier="ENG-7",
        title="Sync is slow",
        description="Two of the three fetches are serial.",
        state="Todo",
        team_key="ENG",
        url="https://linear.app/x/issue/ENG-7",
        comments=[
            LinearComment(body="Only for the nightly run?", created_at=datetime.now(UTC)),
        ],
    )
    text = classify.ticket_text(_task(linear=linear))
    assert text is not None
    assert "ENG-7: Sync is slow" in text
    assert "Two of the three fetches are serial." in text
    assert "Only for the nightly run?" in text


def test_ticket_text_is_clipped() -> None:
    text = classify.ticket_text(_task(issue=_issue(body="x" * 40_000)))
    assert text is not None
    assert len(text) <= 8_000
    assert text.endswith("…")


# ---------------------------------------------------------------------------
# suggestable_modes


def test_suggestable_modes_offers_research_but_not_adversarial_review() -> None:
    """`suggest_when` is the opt-in: a working style isn't a ticket shape."""
    names = [s.name for s in classify.suggestable_modes(Config())]
    assert names == ["research"]


def test_suggestable_modes_drops_the_mode_already_chosen() -> None:
    from goblin_watcher import modes

    chosen = modes.BUILTIN_MODES["research"]
    assert classify.suggestable_modes(Config(), chosen=chosen) == []


def test_suggestable_modes_includes_a_user_mode_that_opts_in() -> None:
    """A user mode becomes suggestable by writing one sentence — no code."""
    cfg = Config()
    cfg.modes["spike"] = ModeSpec(
        template="spike_prompt.md",
        suggest_when="the ticket is a timeboxed experiment with no committed outcome",
    )
    cfg.modes["quiet"] = ModeSpec(template="quiet_prompt.md")
    names = [s.name for s in classify.suggestable_modes(cfg)]
    assert names == ["research", "spike"]


# ---------------------------------------------------------------------------
# build_prompt


def test_build_prompt_lists_each_candidate_with_its_condition() -> None:
    from goblin_watcher import modes

    prompt = classify.build_prompt("ENG-1: do a thing", [modes.BUILTIN_MODES["research"]])
    assert "ENG-1: do a thing" in prompt
    assert "- research: the ticket is question-shaped" in prompt
    assert "at most 3 things" in prompt


def test_build_prompt_forbids_a_mode_when_none_are_offered() -> None:
    prompt = classify.build_prompt("ENG-1: do a thing", [])
    assert '"mode" must be null' in prompt
    assert "research" not in prompt


# ---------------------------------------------------------------------------
# parse


def test_parse_reads_a_plain_json_answer() -> None:
    raw = (
        '{"mode": "research", "reason": "It asks whether sharding would help.",'
        ' "ambiguities": ["Which table?"]}'
    )
    result = classify.parse(raw, allowed={"research"})
    assert result is not None
    assert result.suggested_mode == "research"
    assert result.reason == "It asks whether sharding would help."
    assert result.ambiguities == ["Which table?"]
    assert result.is_empty is False


def test_parse_survives_a_banner_and_a_code_fence() -> None:
    raw = (
        "Loading model config...\n\n"
        "```json\n"
        '{"mode": null, "reason": "", "ambiguities": ["No acceptance criteria"]}\n'
        "```\n"
    )
    result = classify.parse(raw, allowed={"research"})
    assert result is not None
    assert result.suggested_mode is None
    assert result.ambiguities == ["No acceptance criteria"]


def test_parse_drops_a_mode_that_was_never_offered() -> None:
    """The model can name anything; only a registered candidate survives."""
    raw = '{"mode": "vibes", "reason": "felt right", "ambiguities": []}'
    result = classify.parse(raw, allowed={"research"})
    assert result is not None
    assert result.suggested_mode is None
    # A reason with no mode has nothing to justify.
    assert result.reason == ""
    assert result.is_empty is True


def test_parse_caps_the_ambiguity_list_and_skips_non_strings() -> None:
    raw = '{"mode": null, "ambiguities": ["one", "two", {"three": 3}, "four", "five", "six"]}'
    result = classify.parse(raw, allowed=set())
    assert result is not None
    assert result.ambiguities == ["one", "two", "four"]


def test_parse_collapses_and_clips_a_long_ambiguity() -> None:
    raw = '{"mode": null, "ambiguities": ["' + ("word " * 200).strip() + '"]}'
    result = classify.parse(raw, allowed=set())
    assert result is not None
    [item] = result.ambiguities
    assert len(item) <= 240
    assert item.endswith("…")
    assert "\n" not in item


def test_parse_returns_none_on_unusable_output() -> None:
    assert classify.parse(None, allowed={"research"}) is None
    assert classify.parse("   ", allowed={"research"}) is None
    assert classify.parse("I could not read the ticket, sorry.", allowed={"research"}) is None
    assert classify.parse("{not json at all", allowed={"research"}) is None


# ---------------------------------------------------------------------------
# classify / advise


def test_classify_passes_the_timeout_through(monkeypatch: pytest.MonkeyPatch) -> None:
    calls = _patch_llm(monkeypatch, '{"mode": null, "ambiguities": []}')
    result = classify.classify("ENG-1: thing", candidates=[], timeout=7)
    assert result is not None
    assert calls[0]["timeout"] == 7


def test_advise_prints_the_suggestion_and_the_ambiguities(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    _patch_config(monkeypatch)
    _patch_llm(
        monkeypatch,
        '{"mode": "research", "reason": "It asks whether the cache is the bottleneck.",'
        ' "ambiguities": ["Which cache?", "Is p99 or mean the target?"]}',
    )
    result = classify.advise(_task(issue=_issue()))
    assert result is not None
    out = capsys.readouterr().out
    assert "advisory" in out
    assert "suggests --mode research" in out
    assert "It asks whether the cache is the bottleneck." in out
    assert "2 things here are ambiguous" in out
    assert "- Which cache?" in out


def test_advise_says_so_when_the_ticket_looks_fine(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """Silence would read as a broken check rather than a clean ticket."""
    _patch_config(monkeypatch)
    _patch_llm(monkeypatch, '{"mode": null, "reason": "", "ambiguities": []}')
    result = classify.advise(_task(issue=_issue()))
    assert result is not None and result.is_empty
    assert "change-shaped and specific enough" in capsys.readouterr().out


def test_advise_skips_a_task_with_no_ticket(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_config(monkeypatch)
    calls = _patch_llm(monkeypatch, '{"mode": "research"}')
    assert classify.advise(_task()) is None
    assert calls == []


def test_advise_respects_the_config_switch(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_config(monkeypatch, classify_tickets=False)
    calls = _patch_llm(monkeypatch, '{"mode": "research"}')
    assert classify.advise(_task(issue=_issue())) is None
    assert calls == []


def test_advise_respects_the_env_kill_switch(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_config(monkeypatch)
    calls = _patch_llm(monkeypatch, '{"mode": "research"}')
    monkeypatch.setenv("GW_CLASSIFY", "off")
    assert classify.advise(_task(issue=_issue())) is None
    assert calls == []


def test_advise_respects_the_enabled_argument(monkeypatch: pytest.MonkeyPatch) -> None:
    """What `gw new --no-classify` passes."""
    _patch_config(monkeypatch)
    calls = _patch_llm(monkeypatch, '{"mode": "research"}')
    assert classify.advise(_task(issue=_issue()), enabled=False) is None
    assert calls == []


def test_advise_swallows_a_failing_model_call(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """The task already exists by now; nothing here is worth failing over."""
    _patch_config(monkeypatch)

    def boom(*_a: Any, **_kw: Any) -> str | None:
        raise RuntimeError("the model is having a day")

    monkeypatch.setattr("goblin_watcher.classify.description.run_llm", boom)
    assert classify.advise(_task(issue=_issue())) is None
    assert capsys.readouterr().out == ""


def test_advise_says_nothing_when_the_model_answers_nothing(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    _patch_config(monkeypatch)
    _patch_llm(monkeypatch, None)  # binary missing, timeout, or agent = "off"
    assert classify.advise(_task(issue=_issue())) is None
    assert capsys.readouterr().out == ""


def test_advise_does_not_suggest_the_mode_already_chosen(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    from goblin_watcher import modes

    _patch_config(monkeypatch)
    calls = _patch_llm(
        monkeypatch,
        '{"mode": "research", "reason": "asks a question", "ambiguities": []}',
    )
    result = classify.advise(_task(issue=_issue()), mode=modes.BUILTIN_MODES["research"])
    assert result is not None
    # No candidates were offered, so the answer can't name one.
    assert result.suggested_mode is None
    assert '"mode" must be null' in calls[0]["prompt"]
    assert "suggests --mode" not in capsys.readouterr().out
