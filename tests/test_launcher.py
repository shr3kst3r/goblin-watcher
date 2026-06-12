import subprocess
from datetime import UTC, datetime
from pathlib import Path

from typer.testing import CliRunner

from goblin_watcher import state
from goblin_watcher.agents.base import RawSession, TranscriptSummary
from goblin_watcher.agents.launcher import Fresh, Resume, launch
from goblin_watcher.cli import app
from goblin_watcher.models import Task


class _StubAgent:
    name = "claude"
    binary = "stub"

    def __init__(self, captured_id: str | None = None, preassign_id: str | None = None) -> None:
        self._captured = captured_id
        self._preassign = preassign_id
        self.capture_calls = 0
        self.spawn_session_ids: list[str | None] = []

    def spawn_command(
        self, *, prompt: str, cwd: Path, unsafe: bool = False, session_id: str | None = None
    ) -> list[str]:
        del prompt, cwd, unsafe
        self.spawn_session_ids.append(session_id)
        return ["stub", "spawn"]

    def new_session_id(self) -> str | None:
        return self._preassign

    def resume_command(
        self, *, session_id: str | None, cwd: Path, unsafe: bool = False
    ) -> list[str]:
        del session_id, cwd, unsafe
        return ["stub", "resume"]

    def env(self) -> dict[str, str]:
        return {}

    def capture_session_id(self, cwd: Path) -> str | None:
        del cwd
        self.capture_calls += 1
        return self._captured

    def list_sessions(self, cwd: Path) -> list[RawSession]:
        del cwd
        return []

    def read_transcript(self, session_id: str, cwd: Path) -> TranscriptSummary:
        del session_id, cwd
        return TranscriptSummary()


class _AsyncWindower:
    """Mimics tmux: returns immediately after dispatch, without running the agent."""

    name = "tmux"

    def __init__(self) -> None:
        self.observed_task: Task | None = None

    def run(self, *, task: Task, cmd: list[str], cwd: Path, env: dict[str, str]) -> int:
        del cmd, cwd, env
        self.observed_task = task
        return 0

    def is_live(self, task: Task) -> bool:
        del task
        return False


class _InlineWindower:
    name = "inline"

    def run(self, *, task: Task, cmd: list[str], cwd: Path, env: dict[str, str]) -> int:
        del task, cmd, cwd, env
        return 0

    def is_live(self, task: Task) -> bool:
        del task
        return False


def _init_repo(path: Path) -> None:
    path.mkdir(parents=True, exist_ok=True)
    subprocess.run(["git", "init", "-q", "-b", "main", str(path)], check=True)
    subprocess.run(["git", "-C", str(path), "config", "user.email", "t@t"], check=True)
    subprocess.run(["git", "-C", str(path), "config", "user.name", "tester"], check=True)
    (path / "README.md").write_text("hi")
    subprocess.run(["git", "-C", str(path), "add", "."], check=True)
    subprocess.run(["git", "-C", str(path), "commit", "-qm", "init"], check=True)


def _bootstrap(tmp_path: Path) -> Task:
    repo = tmp_path / "alpha"
    _init_repo(repo)
    runner = CliRunner()
    runner.invoke(app, ["project", "new", "alpha", "--dir", str(repo)])
    runner.invoke(app, ["new", "--branch-name", "spike/foo", "--no-launch"])
    proj = state.get_project("alpha")
    [task] = state.list_tasks(proj)
    return task


def test_async_windower_persists_session_before_dispatch(
    isolated_xdg: Path, tmp_path: Path
) -> None:
    """Tmux mode can `execvp` away before `capture_session_id` runs. The record
    must therefore be saved before the windower hands off the agent."""
    task = _bootstrap(tmp_path)
    proj = state.get_project("alpha")
    assert task.sessions == []

    windower = _AsyncWindower()
    _, returned = launch(
        project=proj,
        task=task,
        agent=_StubAgent(captured_id=None),
        choice=Fresh(prompt="kick off"),
        windower=windower,
    )
    # The windower observed the task with the record already attached.
    assert windower.observed_task is not None
    assert len(windower.observed_task.sessions) == 1
    # And the record survived to the persisted state.
    [persisted] = state.list_tasks(proj)
    assert len(persisted.sessions) == 1
    record = persisted.sessions[0]
    assert record.agent == "claude"
    assert record.session_id  # synthesized; just must be non-empty
    assert record.label == "kick off"
    # The returned task matches what we persisted.
    assert returned.sessions == persisted.sessions


def test_preassigned_id_survives_async_windower(isolated_xdg: Path, tmp_path: Path) -> None:
    """Agents that accept a caller-chosen session id (claude --session-id) must
    have that exact id recorded even when the windower (tmux) detaches before
    the transcript exists — this was the bug that left placeholder ids in
    state, making every later `gw run` resume fail."""
    task = _bootstrap(tmp_path)
    proj = state.get_project("alpha")

    agent = _StubAgent(preassign_id="11111111-2222-4333-8444-555555555555")
    launch(
        project=proj,
        task=task,
        agent=agent,
        choice=Fresh(prompt="kick off"),
        windower=_AsyncWindower(),
    )
    # The id was handed to spawn_command and stored verbatim.
    assert agent.spawn_session_ids == ["11111111-2222-4333-8444-555555555555"]
    [persisted] = state.list_tasks(proj)
    assert [s.session_id for s in persisted.sessions] == ["11111111-2222-4333-8444-555555555555"]


def test_preassigned_id_skips_capture_on_inline(isolated_xdg: Path, tmp_path: Path) -> None:
    """With a preassigned id there is nothing to capture; capturing anyway
    could grab an older transcript when the agent exits without writing one."""
    task = _bootstrap(tmp_path)
    proj = state.get_project("alpha")

    agent = _StubAgent(captured_id="stale-other-session", preassign_id="pre-id")
    _, returned = launch(
        project=proj,
        task=task,
        agent=agent,
        choice=Fresh(prompt="kick off"),
        windower=_InlineWindower(),
    )
    assert agent.capture_calls == 0
    assert [s.session_id for s in returned.sessions] == ["pre-id"]


def test_inline_replaces_synthetic_id_with_captured_id(isolated_xdg: Path, tmp_path: Path) -> None:
    """Inline mode lets us read the agent's real session id post-launch; the
    synthetic placeholder we pre-saved should be swapped out."""
    task = _bootstrap(tmp_path)
    proj = state.get_project("alpha")

    real_id = "real-id-from-disk"
    _, returned = launch(
        project=proj,
        task=task,
        agent=_StubAgent(captured_id=real_id),
        choice=Fresh(prompt="kick off"),
        windower=_InlineWindower(),
    )
    assert [s.session_id for s in returned.sessions] == [real_id]
    [persisted] = state.list_tasks(proj)
    assert [s.session_id for s in persisted.sessions] == [real_id]


def test_inline_resume_adds_record_when_capture_diverges(
    isolated_xdg: Path, tmp_path: Path
) -> None:
    """Resuming an existing session is unaffected when capture returns the same
    id; if the agent forks into a new transcript, we add the new record
    alongside the resumed one rather than dropping either."""
    task = _bootstrap(tmp_path)
    proj = state.get_project("alpha")
    seed = task.model_copy(
        update={
            "sessions": [
                _stub_record("resumed-id", "earlier"),
            ]
        }
    )
    state.save_task(proj, seed)

    # Capture returns a different id → new record appended.
    _, returned = launch(
        project=proj,
        task=seed,
        agent=_StubAgent(captured_id="forked-id"),
        choice=Resume(session_id="resumed-id"),
        windower=_InlineWindower(),
    )
    ids = sorted(s.session_id for s in returned.sessions)
    assert ids == ["forked-id", "resumed-id"]


def test_build_seed_prompt_no_user_prompt_uses_default_trailer(
    isolated_xdg: Path, tmp_path: Path
) -> None:
    from goblin_watcher.agents.launcher import build_seed_prompt

    task = _bootstrap(tmp_path)
    seed = build_seed_prompt(task)
    assert "Wait for my next message before taking any action." in seed
    assert "do not begin working" in seed


def test_build_seed_prompt_with_user_prompt_replaces_trailer(
    isolated_xdg: Path, tmp_path: Path
) -> None:
    from goblin_watcher.agents.launcher import build_seed_prompt

    task = _bootstrap(tmp_path)
    seed = build_seed_prompt(task, user_prompt="Refactor the foo module.")
    assert "Wait for my next message" not in seed
    assert "do not begin working" not in seed
    assert "Refactor the foo module." in seed
    # User prompt appears after the PR-open instruction.
    assert seed.index("Refactor the foo module.") > seed.index("open a PR via")


def test_build_seed_prompt_blank_user_prompt_treated_as_none(
    isolated_xdg: Path, tmp_path: Path
) -> None:
    from goblin_watcher.agents.launcher import build_seed_prompt

    task = _bootstrap(tmp_path)
    seed = build_seed_prompt(task, user_prompt="   ")
    assert "Wait for my next message before taking any action." in seed


def test_build_seed_prompt_single_repo_unchanged(isolated_xdg: Path, tmp_path: Path) -> None:
    from goblin_watcher.agents.launcher import build_seed_prompt

    task = _bootstrap(tmp_path)
    seed = build_seed_prompt(task)
    assert f"Branch: {task.branch} (off {task.base_branch})" in seed
    assert f"Worktree: {task.worktree_path}" in seed
    assert "spans" not in seed


def test_build_seed_prompt_multi_repo_lists_every_repo(isolated_xdg: Path, tmp_path: Path) -> None:
    from goblin_watcher.agents.launcher import build_seed_prompt
    from goblin_watcher.models import TaskRepo

    task = _bootstrap(tmp_path)
    multi = task.model_copy(
        update={
            "workspace_path": Path("/ws"),
            "worktree_path": Path("/ws/alpha"),
            "secondary_repos": [
                TaskRepo(
                    project="beta",
                    branch=task.branch,
                    worktree_path=Path("/ws/beta"),
                    base_branch="main",
                )
            ],
        }
    )
    seed = build_seed_prompt(multi)
    assert "spans 2 repositories" in seed
    assert "Workspace: /ws" in seed
    assert "- alpha: /ws/alpha" in seed
    assert "- beta: /ws/beta" in seed


def _stub_record(session_id: str, label: str | None):
    from goblin_watcher.models import SessionRecord

    return SessionRecord(
        agent="claude",
        session_id=session_id,
        created_at=datetime.now(UTC),
        last_used_at=datetime.now(UTC),
        label=label,
    )
