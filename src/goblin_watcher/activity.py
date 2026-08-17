"""What an agent is actually doing, read off the end of its transcript.

`gw` used to answer that from the transcript file's mtime: written recently →
active, otherwise idle. mtime can only tell "moving" from "not moving", and the
three states that matter to someone running six agents at once are not
separable that way:

* **working** — mid tool call, or the turn has been handed to the agent and it
  hasn't answered yet. Nothing for you to do.
* **needs-you** — the agent finished a turn with a question. This is the state
  worth interrupting you for.
* **done** — the agent finished a turn and asked nothing. Look at it when you
  get to it.

The shape of the transcript's tail says which (`Agent.read_tail`): an
outstanding tool call with no result is *working*; a completed turn whose final
assistant text ends on a question is *needs-you*; anything else that completed
is *done*. Two states exist for the cases the transcript can't answer:

* **idle** — quiet, but we can't say why. Agents whose transcripts gw can't
  parse (gemini, managed) land here once their file stops moving,
  which is exactly the old mtime behaviour, and so does a session the
  transcript calls "working" that has written nothing for `stalled_after`
  seconds — an agent killed mid-tool-call, whose pending call would otherwise
  read as working forever.
* **unknown** — no transcript on disk at all. Nothing to infer from.

Nothing here is persisted. Classification is a pure function of the file on
disk, so `gw status` and a sync pass never disagree, and no `SessionRecord`
field can go stale against the transcript it describes. The cost is bounded by
`agents._tail`, which reads a fixed window from the end of the file rather than
the whole thing.
"""

from __future__ import annotations

import hashlib
import logging
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal

from goblin_watcher.agents import get_agent
from goblin_watcher.agents.base import TranscriptTail
from goblin_watcher.models import SessionRecord

_log = logging.getLogger(__name__)

AgentState = Literal["working", "needs-you", "done", "idle", "unknown"]

# How the state was arrived at. Worth keeping distinct from the state itself:
# a `working` read off the transcript is a fact, the same word off an mtime is
# a guess, and the docs and the badge both say so.
ActivitySource = Literal["transcript", "mtime", "none"]

# States that mean the agent has stopped. These are the ones a sync pass turns
# into a notification, and the ones issue-#26 actions will hang off.
TERMINAL_STATES: frozenset[AgentState] = frozenset({"needs-you", "done", "idle"})

_DEFAULT_ACTIVE_SECONDS = 120
_DEFAULT_STALLED_AFTER = 900


@dataclass(frozen=True)
class Activity:
    """One session's classified state, plus what it was classified from."""

    state: AgentState
    source: ActivitySource
    since: datetime | None = None
    detail: str | None = None

    @property
    def needs_you(self) -> bool:
        return self.state == "needs-you"

    @property
    def is_terminal(self) -> bool:
        """The agent has stopped — done, blocked, or quiet for reasons unknown."""
        return self.state in TERMINAL_STATES

    @property
    def edge_token(self) -> str:
        """The value a sync pass records to make notification edge-triggered.

        The state alone is not enough. An agent that asks one question, gets an
        answer, works, and asks a *second* question is in state `needs-you`
        both times, and comparing state names would silently swallow the second
        — the notification that mattered most. Folding a digest of the evidence
        into the token makes "same state, different question" a transition.
        """
        if self.detail is None:
            return self.state
        digest = hashlib.sha256(self.detail.encode("utf-8", "replace")).hexdigest()[:12]
        return f"{self.state}:{digest}"


def classify(
    session: SessionRecord,
    *,
    now: datetime | None = None,
    active_seconds: int | None = None,
    stalled_after: int | None = None,
) -> Activity:
    """Classify what `session`'s agent is doing right now.

    `active_seconds` and `stalled_after` default to
    `defaults.activity_active_seconds` / `defaults.activity_grace_seconds`.
    Callers that already hold a `Config` should pass them: this runs once per
    session per render, and `gw status --watch` renders every couple of
    seconds.
    """
    path = session.transcript_path
    mtime = _mtime(path)
    if path is None or mtime is None:
        return Activity(state="unknown", source="none")

    now = now or datetime.now(UTC)
    if active_seconds is None or stalled_after is None:
        loaded_active, loaded_stalled = _thresholds()
        active_seconds = loaded_active if active_seconds is None else active_seconds
        stalled_after = loaded_stalled if stalled_after is None else stalled_after
    quiet_for = (now - mtime).total_seconds()

    tail = _read_tail(session.agent, path)
    if tail is None:
        return _from_mtime(mtime, quiet_for, active_seconds)

    detail = tail.last_assistant
    if tail.pending_tool or tail.last_role == "user":
        # An agent killed mid-call leaves its tool_use unmatched forever. Past
        # the stall window, believe the silence over the shape — otherwise a
        # dead session sits in `gw status --active` for good.
        if quiet_for > stalled_after:
            return Activity(state="idle", source="mtime", since=mtime, detail=detail)
        return Activity(state="working", source="transcript", since=mtime, detail=detail)

    if tail.last_assistant is None:
        # Turn closed but nothing was said — an aborted turn, or a tail window
        # holding only bookkeeping. mtime is the better answer.
        return _from_mtime(mtime, quiet_for, active_seconds)

    state: AgentState = "needs-you" if ends_on_question(tail.last_assistant) else "done"
    return Activity(state=state, source="transcript", since=mtime, detail=detail)


# ---------------------------------------------------------------------------
# Question detection.

# Phrases that hand control back without a question mark. Deliberately short:
# every entry here is a chance to call a finished run "blocked", and a false
# `needs-you` is the notification fatigue this whole change exists to avoid.
_HANDOFF_PHRASES = (
    "let me know",
    "which would you prefer",
    "waiting for your",
    "waiting on your",
    "your call",
    "tell me which",
)

# Trailing decoration to peel off before asking "does this end in '?'":
# bold/italic markers, code fences, quotes, and closing brackets.
_TRAILING_MARKUP = " \t*_`\"')]}>~"


def ends_on_question(text: str) -> bool:
    """Does this assistant turn hand control back to a human?

    The test is the *last* non-empty line, after trailing markdown decoration
    is peeled off — `**Which approach?**` counts, and a question buried in
    paragraph three followed by "I'll go with the first one" does not. A short
    list of explicit hand-back phrases covers the turns that ask without a
    question mark.

    A heuristic, and knowingly so: it is wrong in the cheap direction. A missed
    question shows up as `done`, which is a badge that undersells rather than a
    notification you didn't need.
    """
    lines = [line.strip() for line in text.strip().splitlines() if line.strip()]
    if not lines:
        return False
    if lines[-1].rstrip(_TRAILING_MARKUP).endswith("?"):
        return True
    tail = " ".join(lines[-3:]).lower()
    return any(phrase in tail for phrase in _HANDOFF_PHRASES)


# ---------------------------------------------------------------------------
# Internals.


def _from_mtime(mtime: datetime, quiet_for: float, active_seconds: int) -> Activity:
    """The pre-transcript heuristic, kept for agents that can't do better."""
    state: AgentState = "working" if quiet_for <= active_seconds else "idle"
    return Activity(state=state, source="mtime", since=mtime)


def _mtime(path: Path | None) -> datetime | None:
    if path is None:
        return None
    try:
        return datetime.fromtimestamp(path.stat().st_mtime, tz=UTC)
    except OSError:
        return None


def _read_tail(agent_name: str, path: Path) -> TranscriptTail | None:
    """`agent_name`'s reading of the transcript tail, or None if it can't.

    Every failure is swallowed: a corrupt transcript, a transcript being
    rewritten under us, or an agent whose format has drifted must degrade to
    the mtime heuristic, never break `gw status`.
    """
    try:
        agent = get_agent(agent_name)
    except Exception as e:
        _log.debug("activity: unknown agent %r: %s", agent_name, e)
        return None
    try:
        return agent.read_tail(path)
    except Exception as e:
        _log.debug("activity: read_tail failed for %s: %s", path, e)
        return None


def _thresholds() -> tuple[int, int]:
    """(`activity_active_seconds`, `activity_grace_seconds`) from config."""
    from goblin_watcher import config

    try:
        defaults = config.load().defaults
        return int(defaults.activity_active_seconds), int(defaults.activity_grace_seconds)
    except Exception:
        return _DEFAULT_ACTIVE_SECONDS, _DEFAULT_STALLED_AFTER


__all__ = [
    "TERMINAL_STATES",
    "Activity",
    "AgentState",
    "classify",
    "ends_on_question",
]
