"""What a sync pass *does* about the edges it detects (ADR 0012).

`gw sync` has always been able to see that CI went red, that a PR landed, or
that an agent stopped — and its only response was a notification. This module
is the other half: a small, static registry of things a pass may do about an
edge, switched on per event in `[sync.on]` and empty by default.

    [sync.on]
    checks-failed = ["spawn-fix-session"]
    pr-merged     = ["prune"]

Three properties hold by construction, and are the reason this is safe to leave
running unattended:

* **Edge-triggered.** Actions are queued from `engine._fire`, which every event
  already routes through, so the existing `_edge` / `last_seen` machinery gives
  once-per-transition for free. A branch that stays red does not re-spawn a
  fixer every five minutes.
* **Static.** The registry below is the whole vocabulary. `[sync.on]` names an
  action, it never *supplies* one — letting config name a command to run would
  make this the plugin system AGENTS.md forbids, and would hand a scheduled
  launchd job an arbitrary execve.
* **Headless.** A spawn action uses `HeadlessWindower` unconditionally, not
  `defaults.windowing`. Nobody is at the terminal when launchd fires a pass, so
  an action that needs one isn't an action.

Every handler is free to *decline*: it returns `ActionResult(ran=False, …)`
carrying the reason, which is journaled as `action-skipped` and — importantly —
does not start the rate-limit clock or consume the pass's action budget. That is
what lets the guards below be conservative without wedging anything: a task
whose agent is still working is skipped this pass and reconsidered on the next.

Deciding *when* an action runs (rate limit, per-pass cap, error isolation) lives
in `engine._run_actions`. This module only knows what each action means.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime

from goblin_watcher import activity, config, gh
from goblin_watcher.agents import get_agent, validate_agent_for_project
from goblin_watcher.agents.launcher import Fresh, build_seed_prompt
from goblin_watcher.agents.launcher import launch as launch_agent
from goblin_watcher.commands.task import (
    archive_task,
    destroy_task,
    dirty_worktrees,
    merge_detection,
)
from goblin_watcher.config import SyncAction
from goblin_watcher.errors import GoblinError
from goblin_watcher.models import Project, Task
from goblin_watcher.windowing import HeadlessWindower


@dataclass(frozen=True)
class PendingAction:
    """One action queued by one edge, addressed by name rather than by object.

    The project and task are held as ids, not records: a pass queues actions
    while it walks its tasks and runs them once the walk is over, so the record
    that fired the edge is stale by the time the action executes. Re-reading it
    at execution time is also what makes "the task was pruned in step 7" a clean
    skip rather than an action against a deleted checkout.
    """

    project: str
    task_id: str
    event: str
    action: str
    body: str

    @property
    def key(self) -> str:
        """Rate-limit key. Shares `last_seen`'s `<project>/<task>:…` shape, so
        the dead-state sweep can drop both with the same split."""
        return f"{self.project}/{self.task_id}:{self.event}:{self.action}"


@dataclass(frozen=True)
class ActionResult:
    ran: bool
    detail: str
    # True when the handler deleted the task record, so the caller knows to drop
    # the derived state that pointed at it.
    removed_task: bool = False


def _acted(detail: str, *, removed_task: bool = False) -> ActionResult:
    return ActionResult(ran=True, detail=detail, removed_task=removed_task)


def _declined(reason: str) -> ActionResult:
    return ActionResult(ran=False, detail=reason)


@dataclass
class ActionContext:
    """Everything a handler is allowed to know. Records are freshly re-read."""

    proj: Project
    task: Task
    pending: PendingAction
    cfg: config.Config
    # This task's PR, from the pass's batched lookup — so `prune` doesn't
    # reintroduce the per-task `gh pr view` that batching exists to remove.
    snapshot: gh.PrSnapshot | None
    now: datetime


Handler = Callable[[ActionContext], ActionResult]


def actions_for(cfg: config.Config, event: str) -> list[SyncAction]:
    """Actions configured for `event`, in the order they were written.

    Iterates rather than indexing because `sync.on` is keyed by the `SyncEvent`
    literal (so a typo in `config.toml` is a validation error, not a silently
    dead rule) while callers hold a plain `str`. The table has at most one row
    per event, so the scan is free.

    Deliberately *not* aliased the way `notify_events` aliases `agent-idle` onto
    the three transcript-derived states: `sync.on` is new, there is no legacy
    config to stay compatible with, and quietly widening "act when this agent
    goes quiet for reasons unknown" into "act whenever any agent stops" is not a
    guess to make on someone's behalf.
    """
    for name, names in cfg.sync.on.items():
        if name == event:
            return list(names)
    return []


class ActionQueue:
    """Actions queued by this pass's edges, deduped, in the order they fired."""

    def __init__(self) -> None:
        self._pending: list[PendingAction] = []
        self._seen: set[str] = set()

    def enqueue(
        self, *, cfg: config.Config, proj: Project, task: Task, event: str, body: str
    ) -> None:
        for name in actions_for(cfg, event):
            item = PendingAction(
                project=proj.name, task_id=task.id, event=event, action=name, body=body
            )
            # Two edges on the same task in one pass (a PR that merged *and*
            # went green) must not run the same action twice.
            if item.key in self._seen:
                continue
            self._seen.add(item.key)
            self._pending.append(item)

    @property
    def pending(self) -> list[PendingAction]:
        return list(self._pending)

    def __len__(self) -> int:
        return len(self._pending)


# ---------------------------------------------------------------------------
# Handlers.


def _working_session(ctx: ActionContext) -> str | None:
    """A session on this task whose agent is mid-flight, or None.

    The edge trigger already bounds how often an action fires, but "CI went red"
    routinely arrives while an agent is still pushing fixes for it. Acting there
    would put a second agent in the same worktree, or archive a checkout out
    from under a live one. Both guards read the transcript through
    `activity.classify` (ADR 0010) — the same call `gw status` renders, so the
    dashboard and the supervisor cannot disagree about who is busy.
    """
    for session in ctx.task.sessions:
        act = activity.classify(
            session,
            now=ctx.now,
            active_seconds=int(ctx.cfg.defaults.activity_active_seconds),
            stalled_after=int(ctx.cfg.defaults.activity_grace_seconds),
        )
        if act.state == "working":
            return session.session_id
    return None


def spawn_fix_session(ctx: ActionContext) -> ActionResult:
    """Start a fresh headless agent session, briefed on the event that fired.

    Windowing is `headless` unconditionally — not `defaults.windowing`. A
    scheduled pass has no terminal to attach to, so `inline` would block the
    pass on the agent and `tmux` would either fail or hijack the user's session.
    """
    task = ctx.task
    if task.archived:
        # Rematerializing means `git worktree add` plus the project's whole
        # `[setup]` bootstrap. Recreating a checkout the user deliberately gave
        # up is not a decision to make unattended.
        return _declined("task is archived — `gw run` restores its worktree first")
    cwd = task.agent_cwd
    if not cwd.exists():
        return _declined(f"nothing to run in: {cwd} does not exist")
    busy = _working_session(ctx)
    if busy is not None:
        return _declined(f"session {busy} is still working")

    agent_name = ctx.cfg.defaults.agent or "claude"
    validate_agent_for_project(agent_name, ctx.proj)
    prompt = build_seed_prompt(task, user_prompt=_brief(ctx.pending))
    exit_code, _ = launch_agent(
        project=ctx.proj,
        task=task,
        agent=get_agent(agent_name),
        choice=Fresh(prompt=prompt),
        windower=HeadlessWindower(),
        unsafe=ctx.cfg.defaults.unsafe,
    )
    if exit_code != 0:
        raise GoblinError(
            f"Headless {agent_name} spawn for task {task.id!r} exited {exit_code}.",
            hint=f"Check {task.project}/.goblin/logs/ for the run's output.",
        )
    return _acted(f"spawned a headless {agent_name} session")


def prune(ctx: ActionContext) -> ActionResult:
    """Destroy a merged, clean task — the edge-triggered form of step 7.

    Reuses `merge_detection` and `engine.prune_blocker`, so this cannot be
    weaker than the periodic prune no matter which event it is wired to. A user
    who writes `checks-passed = ["prune"]` gets a decline, not a deleted branch.
    """
    task = ctx.task
    if task.kind == "scratch":
        return _declined("scratch tasks have no merge signal — see sync.scratch_prune_days")
    detected = merge_detection(ctx.proj, task, snapshot=ctx.snapshot)
    if detected is None:
        return _declined("branch is not merged")
    # Imported here, not at module scope: the engine imports this module to
    # queue actions, so a top-level import back into it would be a cycle.
    from goblin_watcher.sync import engine

    blocker = engine.prune_blocker(ctx.proj, task)
    if blocker is not None:
        return _declined(blocker[1])
    destroy_task(ctx.proj, task, force=False)
    return _acted(f"pruned ({detected})", removed_task=True)


def archive(ctx: ActionContext) -> ActionResult:
    """Drop the task's worktree, keeping its record, branch, and sessions.

    The cheap counterpart to `prune` for anyone who wants the disk back but not
    the amnesia (gh-23). Never forces: a dirty worktree or a live agent is a
    decline, because the checkout is the only copy of anything uncommitted.
    """
    task = ctx.task
    if task.kind == "scratch":
        return _declined("a scratch space's directory is the only copy of its work")
    if task.archived:
        return _declined("already archived")
    busy = _working_session(ctx)
    if busy is not None:
        return _declined(f"session {busy} is still working")
    dirty = dirty_worktrees(task)
    if dirty:
        return _declined(f"the worktree has uncommitted changes ({dirty[0]})")
    removed = archive_task(ctx.proj, task, force=False)
    return _acted(f"archived, dropping {len(removed)} worktree(s)")


REGISTRY: dict[str, Handler] = {
    "spawn-fix-session": spawn_fix_session,
    "prune": prune,
    "archive": archive,
}

ACTION_NAMES: tuple[str, ...] = tuple(REGISTRY)


def dispatch(name: str, ctx: ActionContext) -> ActionResult:
    handler = REGISTRY.get(name)
    if handler is None:
        raise GoblinError(
            f"Unknown sync action {name!r}.",
            hint=f"`sync.on` accepts: {', '.join(ACTION_NAMES)}.",
        )
    return handler(ctx)


# ---------------------------------------------------------------------------
# The brief a spawned session wakes up holding.

# Per-event standing instruction. Everything not listed falls back to the
# generic line: an action wired to an event with no tailored brief still gets a
# session that knows what happened, rather than nothing.
_INSTRUCTIONS: dict[str, str] = {
    "checks-failed": (
        "CI is failing on this branch. Find out which checks failed and why "
        "(`gw pr checks` shows the per-check detail behind the badge), fix the cause, "
        "commit, and push."
    ),
    "checks-passed": (
        "CI is green on this branch. Check the work over — is it complete and ready "
        "for review? Open or update the PR if that is what is left to do."
    ),
    "agent-needs-you": (
        "The previous session on this task ended by asking a question and nobody "
        "answered it. Read back over what it was doing, work out what it was blocked "
        "on, decide it yourself, and carry on."
    ),
    "agent-done": (
        "The previous session on this task finished its turn. Check its work over: is "
        "it complete, verified, committed, and pushed? Finish anything it left."
    ),
    "agent-idle": (
        "The previous session on this task went quiet and its transcript cannot say "
        "why. Work out where it got to and pick the task back up."
    ),
    "pr-merged": (
        "This task's PR has landed. Confirm there is nothing left uncommitted or "
        "unpushed on the branch, and report what remains."
    ),
    "parent-merged": (
        "The branch this one is stacked on has landed. Rebase this branch onto its "
        "base branch and retarget its PR."
    ),
    "prunable": (
        "This branch is merged but its worktree still has uncommitted changes, so it "
        "was not cleaned up. Work out whether those changes matter — land them if they "
        "do, discard them if they don't — and say which you did."
    ),
}

_DEFAULT_INSTRUCTION = "Work out what that means for this task and deal with it."

_BRIEF = """\
`gw sync` started this session unattended, because the `{event}` event fired on this task:

{body}

{instruction}

You are running headless: there is no human here to answer a question. Make the \
reasonable call, state any assumption you had to make in what you write, and stop with \
an explanation rather than guessing at anything destructive."""


def _brief(pending: PendingAction) -> str:
    """The trailer a spawned session is seeded with.

    Rendered into the ordinary `spawn_prompt.md` brief as its `{trailer}`, so
    the session gets the task's ticket, branch, and worktree context exactly as
    a hand-run `gw run --prompt` would — this only supplies the "and here is why
    you were woken up" part.
    """
    return _BRIEF.format(
        event=pending.event,
        body=pending.body.strip() or "(no detail recorded)",
        instruction=_INSTRUCTIONS.get(pending.event, _DEFAULT_INSTRUCTION),
    )


__all__ = [
    "ACTION_NAMES",
    "REGISTRY",
    "ActionContext",
    "ActionQueue",
    "ActionResult",
    "PendingAction",
    "actions_for",
    "dispatch",
]
