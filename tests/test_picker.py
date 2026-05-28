from datetime import UTC, datetime
from pathlib import Path
from unittest.mock import patch

from goblin_watcher.models import Project, SessionRecord, Task
from goblin_watcher.picker import (
    CancelChoice,
    FreshChoice,
    ResumeChoice,
    choose_project,
    choose_session,
    choose_task,
)


def _s(agent: str, sid: str, summary: str) -> SessionRecord:
    now = datetime.now(UTC)
    return SessionRecord(
        agent=agent, session_id=sid, created_at=now, last_used_at=now, summary=summary
    )


def test_choose_session_empty_returns_fresh() -> None:
    assert isinstance(choose_session([]), FreshChoice)


def test_choose_session_resume_branch() -> None:
    sessions = [
        _s("claude", "id-1", "First"),
        _s("claude", "id-2", "Second"),
    ]
    with patch("goblin_watcher.picker.questionary.select") as mocked:
        mocked.return_value.ask.return_value = ResumeChoice(session_id="id-2", agent="claude")
        out = choose_session(sessions)
    assert isinstance(out, ResumeChoice)
    assert out.session_id == "id-2"


def test_choose_session_fresh_branch() -> None:
    sessions = [_s("claude", "id-1", "First")]
    with patch("goblin_watcher.picker.questionary.select") as mocked:
        mocked.return_value.ask.return_value = FreshChoice()
        out = choose_session(sessions)
    assert isinstance(out, FreshChoice)


def test_choose_session_cancel_branch() -> None:
    sessions = [_s("claude", "id-1", "First")]
    with patch("goblin_watcher.picker.questionary.select") as mocked:
        mocked.return_value.ask.return_value = None
        out = choose_session(sessions)
    assert isinstance(out, CancelChoice)


def _p(name: str) -> Project:
    return Project(name=name, root=Path(f"/tmp/{name}"), created_at=datetime.now(UTC))


def test_choose_project_returns_selection() -> None:
    a, b = _p("alpha"), _p("beta")
    rows = [(a, 0, None), (b, 2, datetime.now(UTC))]
    with patch("goblin_watcher.picker.questionary.select") as mocked:
        mocked.return_value.ask.return_value = a
        assert choose_project(rows) is a


def test_choose_project_cancel_returns_none() -> None:
    a = _p("alpha")
    with patch("goblin_watcher.picker.questionary.select") as mocked:
        mocked.return_value.ask.return_value = None
        assert choose_project([(a, 0, None)]) is None


def test_choose_project_empty_list_returns_none() -> None:
    assert choose_project([]) is None


def _t(task_id: str) -> Task:
    now = datetime.now(UTC)
    return Task(
        id=task_id,
        project="alpha",
        branch=task_id,
        worktree_path=Path(f"/tmp/wt/{task_id}"),
        base_branch="main",
        created_at=now,
    )


def test_choose_task_returns_selection() -> None:
    one, two = _t("eng-1"), _t("eng-2")
    with patch("goblin_watcher.picker.questionary.select") as mocked:
        mocked.return_value.ask.return_value = two
        assert choose_task([one, two]) is two


def test_choose_task_empty_list_returns_none() -> None:
    assert choose_task([]) is None
