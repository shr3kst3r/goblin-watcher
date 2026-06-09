"""SessionRecord helpers: rolling-summary refresh, upsert into a Task."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import cast

from goblin_watcher import config, description, state
from goblin_watcher.agents import get_agent
from goblin_watcher.agents import registry as agent_registry
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
                }
            )
            return task.model_copy(
                update={"sessions": [*task.sessions[:i], merged, *task.sessions[i + 1 :]]}
            )
    return task.model_copy(update={"sessions": [*task.sessions, session]})


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


def persist(project: Project, task: Task) -> None:
    state.save_task(project, task)


def schedule_descriptions(project: Project, task: Task) -> None:
    """Fire-and-forget background refresh of every stale session description.

    Caller passes the post-snippet-refresh task. Never blocks; failures are
    logged but otherwise swallowed.
    """
    for s in task.sessions:
        description.schedule_if_stale(project, task, s)
