"""Every registered agent declares whether gw can parse its transcripts.

The declaration is what `gw doctor` warns from (issue #27), so it has to stay
truthful: an agent that stubs `read_transcript` / `render_transcript` must say
so, and one that parses them for real must not.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from goblin_watcher.agents import (
    AGENT_NAMES,
    PARSEABLE_TRANSCRIPTS,
    TranscriptCapability,
    TranscriptSummary,
    get_agent,
)

_STUBBED = {"gemini", "managed"}


def test_every_agent_declares_a_capability() -> None:
    for name in AGENT_NAMES:
        assert isinstance(get_agent(name).transcripts, TranscriptCapability), name


def test_non_parseable_agents_explain_why() -> None:
    for name in AGENT_NAMES:
        capability = get_agent(name).transcripts
        if capability.parseable:
            continue
        assert capability.reason, name
        # Doctor splices the reason into a sentence; a trailing period would
        # read as a stray full stop mid-clause.
        assert not capability.reason.endswith("."), name


def test_capability_matches_the_stubbed_set() -> None:
    stubbed = {n for n in AGENT_NAMES if not get_agent(n).transcripts.parseable}
    assert stubbed == _STUBBED


@pytest.mark.parametrize("name", sorted(_STUBBED))
def test_stubbed_agents_really_return_nothing(name: str, tmp_path: Path) -> None:
    # Guards the declaration against drift: if someone implements one of these,
    # this test fails and points them at the capability that needs updating.
    agent = get_agent(name)
    assert agent.read_transcript("sid", tmp_path) == TranscriptSummary()
    assert agent.render_transcript("sid", tmp_path) is None


def test_parseable_singleton_needs_no_reason() -> None:
    assert PARSEABLE_TRANSCRIPTS.parseable
    assert PARSEABLE_TRANSCRIPTS.reason == ""


def test_non_parseable_without_reason_is_rejected() -> None:
    with pytest.raises(ValueError):
        TranscriptCapability(parseable=False)
