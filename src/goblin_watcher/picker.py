"""Interactive pickers (questionary-backed).

Sessions return a SessionChoice — Resume(session_id), Fresh, or Cancel.
Projects and tasks return the picked entity directly, or None on cancel.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime

import questionary

from goblin_watcher import description
from goblin_watcher.console import AGENT_STYLES
from goblin_watcher.models import Project, SessionRecord, Task

# Inserted by cli.py when the user passes `--session` with no value.
# Command handlers recognize this string and route to the picker instead of a resume-by-id.
SESSION_PICK_SENTINEL = "__gw_pick_session__"


@dataclass
class ResumeChoice:
    session_id: str
    agent: str


@dataclass
class FreshChoice:
    pass


@dataclass
class CancelChoice:
    pass


SessionChoice = ResumeChoice | FreshChoice | CancelChoice


def _fmt_relative(ts: datetime) -> str:
    now = datetime.now(UTC)
    if ts.tzinfo is None:
        ts = ts.replace(tzinfo=UTC)
    delta = now - ts
    seconds = int(delta.total_seconds())
    if seconds < 60:
        return "just now"
    if seconds < 3600:
        return f"{seconds // 60}m ago"
    if seconds < 86400:
        return f"{seconds // 3600}h ago"
    return f"{seconds // 86400}d ago"


def _row(s: SessionRecord) -> str:
    agent = s.agent
    if agent in AGENT_STYLES:
        # questionary doesn't render Rich markup — keep plain.
        pass
    summary = description.display_text(s)
    turns = f"{s.turn_count} turn{'s' if s.turn_count != 1 else ''}" if s.turn_count else "0 turns"
    return f"{agent:<7} {_fmt_relative(s.last_used_at):<10} {summary}  [{turns}]"


def choose_session(sessions: list[SessionRecord]) -> SessionChoice:
    """Render an interactive picker. The session list should be newest-first."""
    if not sessions:
        return FreshChoice()
    fresh_label = "[ New session ]"
    choices = [
        questionary.Choice(_row(s), value=ResumeChoice(s.session_id, s.agent)) for s in sessions
    ]
    choices.append(questionary.Choice(fresh_label, value=FreshChoice()))
    answer = questionary.select(
        "Pick a session to resume:",
        choices=choices,
    ).ask()
    if answer is None:
        return CancelChoice()
    return answer  # type: ignore[no-any-return]


def _pluralize(count: int, singular: str) -> str:
    return f"{count} {singular}{'' if count == 1 else 's'}"


def _project_row(p: Project, task_count: int, last_activity: datetime | None) -> str:
    activity = _fmt_relative(last_activity) if last_activity else "no activity"
    return f"{p.name:<20} {_pluralize(task_count, 'task'):<10} last activity {activity}"


def choose_project(
    projects: list[tuple[Project, int, datetime | None]],
) -> Project | None:
    """Pick a project from `(project, task_count, last_activity)` tuples.

    Sorted alphabetically by project name. Returns the chosen Project, or None on cancel.
    """
    if not projects:
        return None
    ranked = sorted(projects, key=lambda t: t[0].name.casefold())
    choices = [questionary.Choice(_project_row(p, n, last), value=p) for p, n, last in ranked]
    answer = questionary.select("Pick a project:", choices=choices).ask()
    return answer  # type: ignore[no-any-return]


def _task_row(t: Task) -> str:
    title = (t.linear.title if t.linear else None) or "(no Linear issue)"
    sessions_label = _pluralize(len(t.sessions), "session")
    last = max((s.last_used_at for s in t.sessions), default=None)
    activity = _fmt_relative(last) if last else "—"
    return f"{t.id:<14} {title[:40]:<42} {sessions_label:<11} {activity}"


def choose_task(tasks: list[Task]) -> Task | None:
    """Pick a task. Sorted newest-session-first, then most-recently-created.

    Returns the chosen Task, or None on cancel.
    """
    if not tasks:
        return None
    ranked = sorted(
        tasks,
        key=lambda t: (
            max((s.last_used_at for s in t.sessions), default=t.created_at),
            t.created_at,
        ),
        reverse=True,
    )
    choices = [questionary.Choice(_task_row(t), value=t) for t in ranked]
    answer = questionary.select("Pick a task:", choices=choices).ask()
    return answer  # type: ignore[no-any-return]
