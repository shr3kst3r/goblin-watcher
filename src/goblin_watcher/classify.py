"""Advisory ticket classification at task-creation time (ADR 0011).

`gw new` picks the agent, the work mode, and the seed prompt before anything has
read the ticket. This module reads it — once, with the same cheap model the
description machinery already uses — and prints what it made of it:

- a **mode suggestion**, when a registered mode's `suggest_when` fits the
  ticket's shape (a question-shaped ticket wants `--mode research`, not an
  implementation session);
- the **ambiguities** an engineer would otherwise discover an hour in, while the
  agent works the wrong reading of them.

Two rules, and they are the whole contract:

1. **Advisory only.** Nothing here changes the task, the mode, the agent, or the
   prompt. `advise` prints and returns; the caller's next line is the launch it
   was always going to do.
2. **Never in the way.** Missing binary, timeout, banner-wrapped output,
   malformed JSON, a mode name the model invented — every one of those returns
   None and the caller proceeds. The worktree already exists by the time we run,
   so a failure here has nothing to protect the user from.

Which modes are suggestable is data, not a name check: `ModeSpec.suggest_when`
describes the ticket shape that should trigger its mode, so a user's own mode in
`[modes.<name>]` becomes suggestable by writing one sentence — ADR 0009's rule
that no consumer branches on a mode's name applies here like everywhere else.
"""

from __future__ import annotations

import json
import logging
import os
from typing import Any

from pydantic import BaseModel, Field

from goblin_watcher import config, description, modes
from goblin_watcher.agents.launcher import format_ticket_context
from goblin_watcher.console import console
from goblin_watcher.models import Task
from goblin_watcher.modes import ModeSpec

_log = logging.getLogger(__name__)

# The ticket goes into the prompt clipped: a ticket with a pasted stack trace or
# a long comment thread is not more classifiable for the extra tokens, and this
# call sits between the user and their agent.
_MAX_TICKET_CHARS = 8_000
# Ambiguities are a glance, not a report. The issue's own framing — "three things
# here are ambiguous" — is the cap.
_MAX_AMBIGUITIES = 3
_MAX_AMBIGUITY_CHARS = 240
_MAX_REASON_CHARS = 200
# Values of $GW_CLASSIFY that turn the pass off. The env var exists so a script,
# a cron job, or a test suite can guarantee gw makes no model call, without
# editing the user's config.
_OFF_VALUES = frozenset({"0", "off", "false", "no"})


class Classification(BaseModel):
    """What the model made of one ticket.

    Both halves are optional and independent: a well-specified question-shaped
    ticket yields a mode and no ambiguities, a muddled implementation ticket the
    other way round, and a clear change-shaped ticket neither — which is the
    common case, and is reported as such rather than silently.
    """

    # Name of a suggested mode, always one that was offered as a candidate.
    suggested_mode: str | None = None
    # One sentence saying what in the ticket made that mode fit. Only meaningful
    # alongside `suggested_mode`.
    reason: str = ""
    ambiguities: list[str] = Field(default_factory=list)

    @property
    def is_empty(self) -> bool:
        return self.suggested_mode is None and not self.ambiguities


_PROMPT_TEMPLATE = """\
You are triaging one work ticket for an engineer who is about to point an AI
coding agent at it. Your job is to catch two things before that agent starts:
that the ticket wants a different kind of session than an implementation one,
and that the ticket cannot be worked as written without guessing.

Ticket:

{ticket}

{modes_block}Reply with a single JSON object and nothing else:

{{"mode": <mode name or null>, "reason": "<one sentence>", "ambiguities": ["...", "..."]}}

- "mode": the name of the work mode above whose condition this ticket meets, or
  null when none of them does. Null is the common answer: most tickets are
  ordinary change-shaped work. Never invent a name that is not listed.
- "reason": one sentence, at most 25 words, saying what in the ticket made that
  mode fit. Use an empty string when "mode" is null.
- "ambiguities": at most {max_ambiguities} things the engineer would have to
  guess to work this ticket — a decision the ticket leaves open, a term it never
  defines, a sentence with two readings, a missing acceptance criterion. Each
  one short, specific, and about this ticket; quote it where that helps. Return
  an empty list when the ticket is specific enough to work from. Do not pad the
  list, and do not list ordinary implementation detail that any competent
  engineer would simply choose.
"""

_MODES_HEADER = "Work modes available for this ticket, and the condition for each:"
_NO_MODES_BLOCK = 'No alternate work modes are available here, so "mode" must be null.\n\n'


def ticket_text(task: Task) -> str | None:
    """The tracking item as text, clipped, or None when the task carries none.

    Rendered through the seed prompt's own ticket block so the classifier reads
    exactly the document the agent will be handed. `--branch`, `--dir`, `--pr`
    and scratch tasks carry no tracking item at all, and None is how they opt
    out — there is nothing to classify, and inventing something from the branch
    name would be advice about a slug.
    """
    if task.linear is None and task.github_issue is None:
        return None
    header = f"{task.ticket_id}: {task.ticket_title or task.id}"
    body = format_ticket_context(task).strip()
    return _clip(f"{header}\n\n{body}", _MAX_TICKET_CHARS)


def suggestable_modes(cfg: config.Config, *, chosen: ModeSpec | None = None) -> list[ModeSpec]:
    """Registered modes that say when they should be suggested.

    A mode with no `suggest_when` is never offered, which is how
    `adversarial-review` stays out of it: it answers to how you want to work,
    not to anything readable in the ticket. `chosen` — the mode the command line
    already asked for — is dropped too, since "suggests --mode research" under a
    command that said `--mode research` is noise dressed as insight.
    """
    return [
        spec
        for name, spec in sorted(modes.available(cfg.modes).items())
        if spec.suggest_when.strip() and (chosen is None or name != chosen.name)
    ]


def build_prompt(ticket: str, candidates: list[ModeSpec]) -> str:
    """Render the classification prompt for `ticket` and the offered modes."""
    if candidates:
        lines = "\n".join(f"- {s.name}: {s.suggest_when.strip()}" for s in candidates)
        modes_block = f"{_MODES_HEADER}\n{lines}\n\n"
    else:
        modes_block = _NO_MODES_BLOCK
    return _PROMPT_TEMPLATE.format(
        ticket=ticket,
        modes_block=modes_block,
        max_ambiguities=_MAX_AMBIGUITIES,
    )


def parse(raw: str | None, *, allowed: set[str]) -> Classification | None:
    """Turn the model's raw stdout into a `Classification`, or None.

    Agent CLIs prepend banners and wrap answers in code fences, so we scan for
    the first decodable JSON object rather than trusting all of stdout to be
    one. Everything the model could invent is then dropped rather than shown: an
    unregistered mode name, a fourth ambiguity, a non-string entry. Advice that
    is only sometimes real is worse than no advice, because the reader has to
    check it.
    """
    if not raw or not raw.strip():
        return None
    data = _first_json_object(raw)
    if data is None:
        return None

    mode = data.get("mode")
    name = mode.strip().lower() if isinstance(mode, str) else ""
    suggested = name if name in allowed else None

    reason = data.get("reason")
    reason_text = _collapse(reason) if isinstance(reason, str) else ""

    items: list[str] = []
    entries = data.get("ambiguities")
    if isinstance(entries, list):
        for entry in entries:
            if not isinstance(entry, str):
                continue
            text = _collapse(entry)
            if text:
                items.append(_clip(text, _MAX_AMBIGUITY_CHARS))

    return Classification(
        suggested_mode=suggested,
        # A reason with no mode has nothing to justify, so it is dropped rather
        # than printed on its own.
        reason=_clip(reason_text, _MAX_REASON_CHARS) if suggested else "",
        ambiguities=items[:_MAX_AMBIGUITIES],
    )


def classify(ticket: str, *, candidates: list[ModeSpec], timeout: int) -> Classification | None:
    """Classify one ticket. Returns None when the model gave nothing usable."""
    raw = description.run_llm(build_prompt(ticket, candidates), timeout=timeout)
    return parse(raw, allowed={s.name for s in candidates})


def print_advice(result: Classification) -> None:
    """Print a classification as the advisory block under a task's settings."""
    if result.is_empty:
        # Said out loud: silence would read as "the check is broken" rather than
        # "the check found nothing", and the reader can't tell them apart.
        console.print("[muted]Ticket check: reads as change-shaped and specific enough.[/]")
        return
    console.print("Ticket check [muted](advisory — nothing below was applied)[/]:")
    if result.suggested_mode is not None:
        suffix = f" — {result.reason}" if result.reason else ""
        console.print(f"  [hint]suggests[/] --mode {result.suggested_mode}{suffix}")
    if result.ambiguities:
        count = len(result.ambiguities)
        console.print(
            f"  [hint]{count} thing{'' if count == 1 else 's'} "
            f"here {'is' if count == 1 else 'are'} ambiguous[/]:"
        )
        for item in result.ambiguities:
            console.print(f"    - {item}")


def advise(
    task: Task, *, mode: ModeSpec | None = None, enabled: bool = True
) -> Classification | None:
    """Read `task`'s ticket, print the advice, and return it. Never raises.

    Callers ignore the return value — the printing is the feature — but it is
    returned so tests can assert on what was decided rather than on rendered
    text. The blanket `except` is deliberate and is the point of the function:
    this runs after the task, branch, and worktree already exist, so there is no
    failure here worth taking a created task down with.
    """
    try:
        return _advise(task, mode=mode, enabled=enabled)
    except Exception as exc:  # advisory: a failed check says nothing, it doesn't raise
        _log.debug("classify: skipped after %s: %s", type(exc).__name__, exc)
        return None


def _advise(task: Task, *, mode: ModeSpec | None, enabled: bool) -> Classification | None:
    if not enabled or _disabled_by_env():
        return None
    cfg = config.load()
    if not cfg.defaults.classify_tickets:
        return None
    ticket = ticket_text(task)
    if ticket is None:
        return None
    result = classify(
        ticket,
        candidates=suggestable_modes(cfg, chosen=mode),
        timeout=int(cfg.defaults.classify_timeout_seconds),
    )
    if result is None:
        return None
    print_advice(result)
    return result


def _disabled_by_env() -> bool:
    return os.environ.get("GW_CLASSIFY", "").strip().lower() in _OFF_VALUES


def _first_json_object(text: str) -> dict[str, Any] | None:
    """The first JSON object embedded anywhere in `text`, or None."""
    decoder = json.JSONDecoder()
    for index, char in enumerate(text):
        if char != "{":
            continue
        try:
            value, _ = decoder.raw_decode(text[index:])
        except ValueError:
            continue
        if isinstance(value, dict):
            return value
    return None


def _collapse(text: str) -> str:
    return " ".join(text.split())


def _clip(text: str, max_chars: int) -> str:
    if len(text) <= max_chars:
        return text
    return text[: max_chars - 1].rstrip() + "…"


__all__ = [
    "Classification",
    "advise",
    "build_prompt",
    "classify",
    "parse",
    "print_advice",
    "suggestable_modes",
    "ticket_text",
]
