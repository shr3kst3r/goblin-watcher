import subprocess
from pathlib import Path

import pytest
from pytest_httpx import HTTPXMock
from typer.testing import CliRunner

from goblin_watcher import state
from goblin_watcher.cli import _rewrite_linear_shortcut, app
from goblin_watcher.linear.client import LINEAR_ENDPOINT


def _init_repo(path: Path) -> None:
    path.mkdir(parents=True, exist_ok=True)
    subprocess.run(["git", "init", "-q", "-b", "main", str(path)], check=True)
    subprocess.run(["git", "-C", str(path), "config", "user.email", "t@t"], check=True)
    subprocess.run(["git", "-C", str(path), "config", "user.name", "tester"], check=True)
    (path / "README.md").write_text("hi")
    subprocess.run(["git", "-C", str(path), "add", "."], check=True)
    subprocess.run(["git", "-C", str(path), "commit", "-qm", "init"], check=True)


def _mock_issue(
    httpx_mock: HTTPXMock,
    identifier: str,
    title: str,
    body: str = "Do it",
    comments: list[dict[str, object]] | None = None,
) -> None:
    team, _number = identifier.split("-")
    httpx_mock.add_response(
        url=LINEAR_ENDPOINT,
        method="POST",
        json={
            "data": {
                "issues": {
                    "nodes": [
                        {
                            "id": f"uuid-{identifier}",
                            "identifier": identifier,
                            "title": title,
                            "description": body,
                            "url": f"https://linear.app/x/issue/{identifier}",
                            "state": {"name": "Todo"},
                            "team": {"key": team},
                            "comments": {"nodes": comments or []},
                        }
                    ]
                }
            }
        },
    )


@pytest.fixture
def env_key(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("LINEAR_API_KEY", "lin_api_test")


def test_gw_new_linear_uses_matched_team_project(
    isolated_xdg: Path, tmp_path: Path, httpx_mock: HTTPXMock, env_key: None
) -> None:
    repo = tmp_path / "eng-repo"
    _init_repo(repo)
    runner = CliRunner()
    res = runner.invoke(app, ["project", "new", "eng", "--dir", str(repo), "--team", "ENG"])
    assert res.exit_code == 0, res.output

    _mock_issue(httpx_mock, "ENG-123", "Add rate limit")
    res = runner.invoke(app, ["new", "--linear", "ENG-123", "--no-launch"])
    assert res.exit_code == 0, res.output

    proj = state.get_project("eng")
    [task] = state.list_tasks(proj)
    assert task.id == "eng-123"
    assert task.linear is not None
    assert task.linear.identifier == "ENG-123"
    assert task.branch.startswith("eng-123-")
    assert task.worktree_path.exists()


def test_gw_new_linear_without_project_or_repo_errors(
    isolated_xdg: Path, httpx_mock: HTTPXMock, env_key: None
) -> None:
    from goblin_watcher.errors import GoblinError

    _mock_issue(httpx_mock, "ENG-1", "Test")
    runner = CliRunner()
    res = runner.invoke(app, ["new", "--linear", "ENG-1", "--no-launch"])
    assert res.exit_code != 0
    assert isinstance(res.exception, GoblinError)
    assert "team 'ENG'" in res.exception.message or "no registered" in res.exception.message.lower()


def test_gw_new_linear_with_repo_clones_and_registers(
    isolated_xdg: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    httpx_mock: HTTPXMock,
    env_key: None,
) -> None:
    # Local upstream that gw can clone.
    upstream = tmp_path / "upstream.git"
    _init_repo(upstream)
    workspace = tmp_path / "clone-here"
    workspace.mkdir()
    monkeypatch.chdir(workspace)

    _mock_issue(httpx_mock, "ENG-7", "Spike: token bucket")
    runner = CliRunner()
    res = runner.invoke(app, ["new", "--linear", "ENG-7", "--repo", str(upstream), "--no-launch"])
    assert res.exit_code == 0, res.output

    # Project is auto-registered as `eng` and the worktree exists.
    proj = state.get_project("eng")
    assert proj.linear_team_key == "ENG"
    [task] = state.list_tasks(proj)
    assert task.linear and task.linear.identifier == "ENG-7"


def test_gw_new_linear_with_from_uses_base_branch(
    isolated_xdg: Path, tmp_path: Path, httpx_mock: HTTPXMock, env_key: None
) -> None:
    """`gw new --linear ENG-X --from feat/pr-branch` bases the new worktree on
    that branch — even when it only exists on origin and must be fetched."""
    upstream = tmp_path / "upstream"
    _init_repo(upstream)
    subprocess.run(["git", "-C", str(upstream), "checkout", "-q", "-b", "feat/pr-base"], check=True)
    (upstream / "extra.txt").write_text("pr work")
    subprocess.run(["git", "-C", str(upstream), "add", "."], check=True)
    subprocess.run(["git", "-C", str(upstream), "commit", "-qm", "pr work"], check=True)
    subprocess.run(["git", "-C", str(upstream), "checkout", "-q", "main"], check=True)

    repo = tmp_path / "eng-repo"
    subprocess.run(["git", "clone", "-q", str(upstream), str(repo)], check=True)
    subprocess.run(["git", "-C", str(repo), "config", "user.email", "t@t"], check=True)
    subprocess.run(["git", "-C", str(repo), "config", "user.name", "tester"], check=True)
    # Sanity: the PR branch is only on origin in this clone.
    assert (
        subprocess.run(
            ["git", "-C", str(repo), "rev-parse", "--verify", "--quiet", "feat/pr-base"]
        ).returncode
        != 0
    )

    runner = CliRunner()
    res = runner.invoke(app, ["project", "new", "eng", "--dir", str(repo), "--team", "ENG"])
    assert res.exit_code == 0, res.output

    _mock_issue(httpx_mock, "ENG-7", "Stack on PR")
    res = runner.invoke(app, ["new", "--linear", "ENG-7", "--from", "feat/pr-base", "--no-launch"])
    assert res.exit_code == 0, res.output

    proj = state.get_project("eng")
    [task] = state.list_tasks(proj)
    assert task.base_branch == "feat/pr-base"
    # The worktree should contain the PR commit (it was branched off feat/pr-base, not main).
    assert (task.worktree_path / "extra.txt").exists()


def test_linear_shortcut_dispatcher_drives_new(
    isolated_xdg: Path, tmp_path: Path, httpx_mock: HTTPXMock, env_key: None
) -> None:
    repo = tmp_path / "eng-repo"
    _init_repo(repo)
    runner = CliRunner()
    runner.invoke(app, ["project", "new", "eng", "--dir", str(repo), "--team", "ENG"])

    _mock_issue(httpx_mock, "ENG-99", "Hello")

    rewritten = _rewrite_linear_shortcut(["ENG-99", "--no-launch"])
    res = runner.invoke(app, rewritten)
    assert res.exit_code == 0, res.output
    proj = state.get_project("eng")
    [task] = state.list_tasks(proj)
    assert task.id == "eng-99"


def test_gw_new_linear_rerun_errors_when_task_exists(
    isolated_xdg: Path, tmp_path: Path, httpx_mock: HTTPXMock, env_key: None
) -> None:
    """Re-running `gw new --linear <ID>` on an existing task must error rather than
    silently resume — the task already exists, so create semantics are wrong."""
    from datetime import UTC, datetime

    from goblin_watcher.models import SessionRecord

    repo = tmp_path / "eng-repo"
    _init_repo(repo)
    runner = CliRunner()
    runner.invoke(app, ["project", "new", "eng", "--dir", str(repo), "--team", "ENG"])

    _mock_issue(httpx_mock, "ENG-77", "Initial title")
    res = runner.invoke(app, ["new", "--linear", "ENG-77", "--no-launch"])
    assert res.exit_code == 0, res.output

    proj = state.get_project("eng")
    [task] = state.list_tasks(proj)
    original_branch = task.branch
    original_worktree = task.worktree_path

    # Attach a session record to simulate prior agent work; the strict re-run
    # must not touch this.
    session = SessionRecord(
        agent="claude",
        session_id="abc-123",
        created_at=datetime.now(UTC),
        last_used_at=datetime.now(UTC),
        label="seed",
    )
    state.save_task(proj, task.model_copy(update={"sessions": [session]}))

    # Second invocation: same ticket. Must error and leave the task untouched.
    from goblin_watcher.errors import GoblinError

    _mock_issue(httpx_mock, "ENG-77", "Refreshed title")
    res = runner.invoke(app, ["new", "--linear", "ENG-77", "--no-launch"])
    assert res.exit_code != 0
    assert isinstance(res.exception, GoblinError)
    assert "already exists" in res.exception.message
    assert res.exception.hint is not None
    assert "gw run eng-77" in res.exception.hint

    [persisted] = state.list_tasks(proj)
    assert persisted.id == "eng-77"
    assert persisted.branch == original_branch
    assert persisted.worktree_path == original_worktree
    assert [s.session_id for s in persisted.sessions] == ["abc-123"]
    # Linear data was NOT refreshed — the strict path bails before that.
    assert persisted.linear is not None
    assert persisted.linear.title == "Initial title"


def test_linear_comments_land_in_seed_prompt(
    isolated_xdg: Path, tmp_path: Path, httpx_mock: HTTPXMock, env_key: None
) -> None:
    from goblin_watcher.agents.launcher import build_seed_prompt

    repo = tmp_path / "eng-repo"
    _init_repo(repo)
    runner = CliRunner()
    runner.invoke(app, ["project", "new", "eng", "--dir", str(repo), "--team", "ENG"])

    _mock_issue(
        httpx_mock,
        "ENG-42",
        "Fix the foo",
        body="A description for the model.",
        comments=[
            {
                "body": "First reaction — I think we should go with option B.",
                "createdAt": "2025-01-10T12:34:56.000Z",
                "user": {"displayName": "Alice"},
            },
            {
                "body": "Pushed a draft, take a look.",
                "createdAt": "2025-01-11T09:00:00.000Z",
                "user": {"displayName": "Bob"},
            },
        ],
    )
    res = runner.invoke(app, ["new", "--linear", "ENG-42", "--no-launch"])
    assert res.exit_code == 0, res.output

    proj = state.get_project("eng")
    [task] = state.list_tasks(proj)
    assert task.linear and len(task.linear.comments) == 2

    seed = build_seed_prompt(task)
    assert "Linear comments (oldest first):" in seed
    assert "Alice" in seed and "Bob" in seed
    assert "option B" in seed
    assert "Pushed a draft" in seed
    # Oldest first ordering.
    assert seed.index("option B") < seed.index("Pushed a draft")
