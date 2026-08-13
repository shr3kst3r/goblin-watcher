"""Opt-in Linear workflow-state moves on session start and PR open (ADR 0012).

Tickets go stale in exactly two places: work starts and the ticket still says
Todo, a PR opens and it still says In Progress. Both moments are ones gw already
knows about, so it can make the move — but only when asked:

    [linear.transitions]
    on_session_start = "In Progress"
    on_pr_open       = "In Review"

Unset means no write, which is the read-only default AGENTS.md describes. Three
rules, and they are the whole contract:

1. **Opt-in, and only into what the user named.** No default state, no inferred
   one, no "close it when the PR merges". A key that isn't set is a code path
   that never reaches the network.
2. **Never in the way.** A missing API key, an unreachable Linear, a slow one, a
   state name the team doesn't define, a mutation that comes back unconfirmed —
   every one of those prints one muted line and returns the task untouched. The
   session still launches; the PR is already open. `apply` does not raise.
3. **Idempotent.** A ticket already in the target state costs one read and no
   write, so resuming a session all day doesn't spam the ticket's activity feed.

The successful move is written back to the task's cached Linear state so
`gw status` doesn't keep showing the state gw just moved away from until its TTL
expires.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime
from typing import Literal

from goblin_watcher import config, secrets, state
from goblin_watcher.console import console, print_success
from goblin_watcher.errors import GoblinError
from goblin_watcher.linear import LinearClient
from goblin_watcher.models import Project, Task

_log = logging.getLogger(__name__)

# The two moments gw can move a ticket at. The values are the config keys, so a
# trigger is looked up rather than branched on.
Trigger = Literal["on_session_start", "on_pr_open"]

_TRIGGER_LABELS: dict[Trigger, str] = {
    "on_session_start": "session start",
    "on_pr_open": "PR open",
}


def target_state(trigger: Trigger, cfg: config.Config | None = None) -> str | None:
    """The state name configured for `trigger`, or None when the user set none.

    Blank and whitespace-only values read as unset: a key someone emptied out
    means the same thing as one they never wrote.
    """
    try:
        transitions = (cfg or config.load()).linear.transitions
    except Exception as e:  # A broken config file must not take the caller down.
        _log.debug("could not read [linear.transitions]: %s", e)
        return None
    raw = getattr(transitions, trigger, None)
    if raw is None:
        return None
    name = raw.strip()
    return name or None


def apply(project: Project, task: Task, trigger: Trigger) -> Task:
    """Move `task`'s Linear ticket for `trigger`, if one is configured.

    Returns the task — updated with the new cached state when the move
    happened, unchanged in every other case, including every failure. Never
    raises: a ticket that didn't move is not a reason to abandon the session or
    the PR that just opened.
    """
    if task.linear is None:
        return task
    try:
        cfg = config.load()
    except Exception as e:  # A broken config file must not take the caller down.
        _log.debug("could not load config for a Linear transition: %s", e)
        return task
    wanted = target_state(trigger, cfg)
    if wanted is None:
        return task

    label = _TRIGGER_LABELS[trigger]
    try:
        return _transition(project, task, wanted, label, cfg.linear.transitions.timeout_seconds)
    except GoblinError as e:
        console.print(f"[muted]Skipped Linear transition on {label}: {e.message}[/]")
    except Exception as e:
        # Deliberately broad, for the same reason `classify.advise` is: this runs
        # between the user and the thing they actually asked for.
        _log.debug("linear transition on %s failed: %s", label, e)
        console.print(f"[muted]Skipped Linear transition on {label}: {e}[/]")
    return task


def _transition(project: Project, task: Task, wanted: str, label: str, timeout: float) -> Task:
    """The move itself: resolve, compare, write, cache. Raises on any failure."""
    assert task.linear is not None  # guarded by `apply`
    identifier = task.linear.identifier

    with LinearClient(secrets.get_linear_api_key(), timeout=float(timeout)) as client:
        workflow = client.fetch_issue_workflow(identifier)
        if workflow.state.casefold() == wanted.casefold():
            # Already there. Resuming a session shouldn't write to the ticket.
            return _cache_state(project, task, workflow.state)
        target = workflow.find_state(wanted)
        if target is None:
            available = ", ".join(workflow.state_names) or "(none reported)"
            raise GoblinError(
                f"{wanted!r} is not a workflow state on Linear team "
                f"{workflow.team_key!r}. Available: {available}.",
                hint="Set `linear.transitions` to a state name your team defines.",
            )
        client.update_issue_state(workflow.issue_id, target.id)

    print_success(f"Moved {identifier} to {target.name!r} on {label}")
    return _cache_state(project, task, target.name)


def _cache_state(project: Project, task: Task, state_name: str) -> Task:
    """Write `state_name` into the task's cached Linear state, best effort.

    Narrow patch under the task lock (ADR 0004), the same shape
    `linear_state.LinearStateFetcher.refresh` uses: the round-trips above took
    real time, so `task` may already be stale. A failure here costs nothing but
    a stale badge until the next `gw status` refresh.
    """
    if task.linear is None or task.linear.state == state_name:
        return task

    def _patch(latest: Task) -> Task:
        if latest.linear is None:
            return latest
        return latest.model_copy(
            update={
                "linear": latest.linear.model_copy(update={"state": state_name}),
                "linear_state_updated_at": datetime.now(UTC),
            }
        )

    try:
        return state.update_task(project, task.id, _patch)
    except GoblinError:
        return task
