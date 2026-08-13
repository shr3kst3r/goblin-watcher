"""SessionRecord helpers: rolling-summary refresh, upsert into a Task."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import cast

from goblin_watcher import config, description, state
from goblin_watcher.agents import get_agent
from goblin_watcher.agents import registry as agent_registry
from goblin_watcher.errors import TaskNotFoundError
from goblin_watcher.models import AgentName, Project, SessionRecord, Task

SUMMARY_TTL_DEFAULT = 30  # seconds; overridable via config.defaults.summary_ttl_seconds


def _now() -> datetime:
    return datetime.now(UTC)


def _ttl_seconds() -> int:
    try:
        return int(config.load().defaults.summary_ttl_seconds)
    except Exception:
        return SUMMARY_TTL_DEFAULT


def upsert(task: Task, session: SessionRecord) -> Task:
    """Add or update a SessionRecord on `task` (matched by agent + session_id)."""
    for i, existing in enumerate(task.sessions):
        if existing.agent == session.agent and existing.session_id == session.session_id:
            merged = existing.model_copy(
                update={
                    "last_used_at": session.last_used_at,
                    "summary": session.summary or existing.summary,
                    "turn_count": session.turn_count or existing.turn_count,
                    "summary_updated_at": session.summary_updated_at or existing.summary_updated_at,
                    "transcript_path": session.transcript_path or existing.transcript_path,
                    "usage": session.usage or existing.usage,
                }
            )
            return task.model_copy(
                update={"sessions": [*task.sessions[:i], merged, *task.sessions[i + 1 :]]}
            )
    return task.model_copy(update={"sessions": [*task.sessions, session]})


def patch_session(task: Task, agent: str, session_id: str, updates: dict[str, object]) -> Task:
    """Return a copy of `task` with `updates` applied to one matching session.

    The narrow-patch primitive behind `state.update_task` callbacks (ADR 0004):
    it touches only the named fields on the session identified by
    `(agent, session_id)` and leaves every other field and session exactly as
    loaded. When no session matches — it was removed while the caller worked —
    the task is returned unchanged, which persists as a harmless no-op write.
    """
    out: list[SessionRecord] = []
    for s in task.sessions:
        if s.agent == agent and s.session_id == session_id:
            out.append(s.model_copy(update=dict(updates)))
        else:
            out.append(s)
    return task.model_copy(update={"sessions": out})


def has_session(task: Task, agent: str, session_id: str) -> bool:
    return any(s.agent == agent and s.session_id == session_id for s in task.sessions)


def refresh_summary(task: Task, session: SessionRecord) -> SessionRecord:
    """Re-read the agent's transcript and update the session's summary in-place.

    Returns the updated SessionRecord. Does not persist on its own.
    """
    agent = get_agent(session.agent)
    parsed = agent.read_transcript(session.session_id, task.agent_cwd)
    summary_text = parsed.last_user_snippet or parsed.last_assistant_snippet or session.summary
    return session.model_copy(
        update={
            "summary": summary_text,
            "turn_count": parsed.turn_count or session.turn_count,
            "summary_updated_at": _now(),
            "transcript_path": parsed.transcript_path or session.transcript_path,
            "usage": parsed.usage or session.usage,
        }
    )


def refresh_if_stale(task: Task, session: SessionRecord) -> SessionRecord:
    """Refresh the session's summary if it's older than the configured TTL."""
    if session.summary_updated_at is None:
        return refresh_summary(task, session)
    age = _now() - session.summary_updated_at
    if age > timedelta(seconds=_ttl_seconds()):
        return refresh_summary(task, session)
    return session


def refresh_task_summaries(task: Task) -> Task:
    """Lazy-refresh every session on the task. Returns a new Task with updates merged."""
    if not task.sessions:
        return task
    refreshed = [refresh_if_stale(task, s) for s in task.sessions]
    return task.model_copy(update={"sessions": refreshed})


# A record younger than this is never treated as dangling: a freshly spawned
# agent (especially via tmux, which detaches immediately) may not have written
# its transcript to disk yet.
_DANGLING_GRACE_SECONDS = 120


def reconcile_sessions(task: Task) -> Task:
    """Repair session records that no longer match the agent's on-disk store.

    A record is *dangling* when its agent can enumerate sessions for the
    task's cwd but the recorded id isn't among them — resuming it would fail.
    The two known ways to get there: tmux-mode spawns used to record a
    synthetic placeholder id that was never reconciled with the agent's real
    one, and agents garbage-collect old transcripts (claude's
    `cleanupPeriodDays`). Dangling records are re-bound to on-disk sessions
    gw isn't tracking yet (paired oldest-to-oldest — in the common case one
    placeholder maps to the one real session), and dropped when no candidate
    remains. Agents whose discovery comes back empty are left untouched:
    that's either an agent with no enumerable store (gemini) or a store we
    can't see, and dropping records on absent evidence loses data.

    Tasks with no records at all fall through to `adopt_orphan_sessions`.
    Pure; callers persist. Callers that persist under a lock should use
    `plan_reconciliation` + `apply_reconciliation` instead, so the on-disk
    discovery this does happens *outside* the lock (ADR 0004).
    """
    if not task.sessions:
        return adopt_orphan_sessions(task)
    now = _now()
    # None marks a record for removal; replacement happens in place so the
    # picker's ordering is preserved.
    result: list[SessionRecord | None] = list(task.sessions)
    changed = False
    for name, agent_cls in agent_registry.items():
        indices = [i for i, s in enumerate(task.sessions) if s.agent == name]
        if not indices:
            continue
        discovered = agent_cls().list_sessions(task.agent_cwd)
        if not discovered:
            continue
        on_disk_ids = {raw.session_id for raw in discovered}
        tracked_ids = {task.sessions[i].session_id for i in indices}
        dangling = [
            i
            for i in indices
            if task.sessions[i].session_id not in on_disk_ids
            and (now - task.sessions[i].created_at).total_seconds() > _DANGLING_GRACE_SECONDS
        ]
        if not dangling:
            continue
        untracked = sorted(
            (raw for raw in discovered if raw.session_id not in tracked_ids),
            key=lambda r: r.created_at,
        )
        for i in sorted(dangling, key=lambda i: task.sessions[i].created_at):
            record = task.sessions[i]
            if untracked:
                raw = untracked.pop(0)
                result[i] = record.model_copy(
                    update={
                        "session_id": raw.session_id,
                        "transcript_path": raw.transcript_path,
                        "label": record.label or raw.first_message_snippet,
                        # Summary/description were derived from the missing
                        # transcript; clear the timestamps so both refresh.
                        "summary_updated_at": None,
                        "description_updated_at": None,
                    }
                )
            else:
                result[i] = None
            changed = True
    if not changed:
        return task
    return task.model_copy(update={"sessions": [s for s in result if s is not None]})


def adopt_orphan_sessions(task: Task) -> Task:
    """Discover sessions on disk that gw doesn't yet know about and adopt them.

    No-op when `task.sessions` is non-empty. Otherwise walks every registered
    agent's on-disk session store for `task.agent_cwd` and upserts whatever
    it finds. Recovers tasks whose pre-record save was lost (older launcher
    bug) or whose agent was spawned outside `gw run`.
    """
    if task.sessions:
        return task
    updated = task
    for name, agent_cls in agent_registry.items():
        agent = agent_cls()
        raw_sessions = agent.list_sessions(task.agent_cwd)
        for raw in raw_sessions:
            record = SessionRecord(
                agent=cast(AgentName, name),
                session_id=raw.session_id,
                created_at=raw.created_at,
                last_used_at=raw.created_at,
                label=raw.first_message_snippet,
            )
            updated = upsert(updated, record)
    return updated


# ---------------------------------------------------------------------------
# Lock-friendly reconciliation: expensive discovery outside, cheap apply inside.
#
# `reconcile_sessions` walks every registered agent's on-disk session store,
# which for codex means globbing the entire `~/.codex/sessions` tree. That must
# not happen while a task lock is held (ADR 0004), so callers that persist split
# it: plan first (no lock), then apply the plan to the freshly-loaded record
# inside `state.update_task`. The plan keys everything by (agent, session_id)
# rather than list position, so it stays correct even if the session list
# changed while the discovery ran.


@dataclass(frozen=True)
class Rebind:
    """Re-point a dangling record at an on-disk session gw wasn't tracking."""

    agent: str
    session_id: str
    new_session_id: str
    transcript_path: Path | None
    label_fallback: str | None


@dataclass(frozen=True)
class ReconcilePlan:
    rebinds: tuple[Rebind, ...] = ()
    drops: tuple[tuple[str, str], ...] = ()  # (agent, session_id)
    adoptions: tuple[SessionRecord, ...] = ()

    @property
    def is_empty(self) -> bool:
        return not (self.rebinds or self.drops or self.adoptions)


def plan_reconciliation(task: Task) -> ReconcilePlan:
    """Discover what reconciliation *would* change. Hits the filesystem; no lock."""
    if not task.sessions:
        adopted = adopt_orphan_sessions(task)
        return ReconcilePlan(adoptions=tuple(adopted.sessions))
    now = _now()
    rebinds: list[Rebind] = []
    drops: list[tuple[str, str]] = []
    for name, agent_cls in agent_registry.items():
        mine = [s for s in task.sessions if s.agent == name]
        if not mine:
            continue
        discovered = agent_cls().list_sessions(task.agent_cwd)
        if not discovered:
            continue
        on_disk_ids = {raw.session_id for raw in discovered}
        tracked_ids = {s.session_id for s in mine}
        dangling = [
            s
            for s in mine
            if s.session_id not in on_disk_ids
            and (now - s.created_at).total_seconds() > _DANGLING_GRACE_SECONDS
        ]
        if not dangling:
            continue
        untracked = sorted(
            (raw for raw in discovered if raw.session_id not in tracked_ids),
            key=lambda r: r.created_at,
        )
        for record in sorted(dangling, key=lambda s: s.created_at):
            if untracked:
                raw = untracked.pop(0)
                rebinds.append(
                    Rebind(
                        agent=name,
                        session_id=record.session_id,
                        new_session_id=raw.session_id,
                        transcript_path=raw.transcript_path,
                        label_fallback=raw.first_message_snippet,
                    )
                )
            else:
                drops.append((name, record.session_id))
    return ReconcilePlan(rebinds=tuple(rebinds), drops=tuple(drops))


def apply_reconciliation(task: Task, plan: ReconcilePlan) -> Task:
    """Apply `plan` to `task`. Pure and cheap — safe to run under a lock."""
    if plan.is_empty:
        return task
    updated = task
    for rb in plan.rebinds:
        existing = next(
            (s for s in updated.sessions if s.agent == rb.agent and s.session_id == rb.session_id),
            None,
        )
        if existing is None:
            continue  # someone else already reconciled or removed it
        updated = patch_session(
            updated,
            rb.agent,
            rb.session_id,
            {
                "session_id": rb.new_session_id,
                "transcript_path": rb.transcript_path,
                "label": existing.label or rb.label_fallback,
                # Summary/description were derived from the missing transcript;
                # clear the timestamps so both refresh.
                "summary_updated_at": None,
                "description_updated_at": None,
            },
        )
    if plan.drops:
        dropped = set(plan.drops)
        updated = updated.model_copy(
            update={
                "sessions": [s for s in updated.sessions if (s.agent, s.session_id) not in dropped]
            }
        )
    for record in plan.adoptions:
        if not has_session(updated, record.agent, record.session_id):
            updated = upsert(updated, record)
    return updated


# Fields owned by snippet-summary refresh. Anything else on the session record
# belongs to another writer and must not be carried over from a stale snapshot.
_SUMMARY_FIELDS = ("summary", "turn_count", "summary_updated_at", "transcript_path", "usage")


def persist_refresh(project: Project, task: Task, plan: ReconcilePlan | None = None) -> Task:
    """Persist reconciliation + refreshed summaries as a narrow patch (ADR 0004).

    `task` may be a snapshot the caller has been holding; only the
    summary-owned fields of each session are carried across, applied to the
    record as freshly loaded under the lock. Returns the persisted task.
    """
    refreshed_by_key = {(s.agent, s.session_id): s for s in task.sessions}

    def _mutate(latest: Task) -> Task:
        out = apply_reconciliation(latest, plan) if plan is not None else latest
        for key, snapshot in refreshed_by_key.items():
            if not has_session(out, key[0], key[1]):
                continue
            out = patch_session(
                out,
                key[0],
                key[1],
                {f: getattr(snapshot, f) for f in _SUMMARY_FIELDS},
            )
        return out

    try:
        return state.update_task(project, task.id, _mutate)
    except TaskNotFoundError:
        # Task removed underneath us (e.g. concurrent `gw task rm`). Nothing to
        # persist; hand back what we had so callers can still render.
        return task


def persist(project: Project, task: Task) -> None:
    """Write a whole task record.

    Prefer `state.update_task` (or `persist_refresh`) for anything derived from
    a snapshot — see ADR 0004. This remains for callers that just created the
    record and are its only writer.
    """
    state.save_task(project, task)


def schedule_descriptions(project: Project, task: Task) -> None:
    """Fire-and-forget background refresh of every stale session description.

    Caller passes the post-snippet-refresh task. Never blocks; failures are
    logged but otherwise swallowed.
    """
    for s in task.sessions:
        description.schedule_if_stale(project, task, s)
