"""Tests for the opt-in Linear workflow-state moves (`[linear.transitions]`)."""

import json
import subprocess
from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path
from unittest.mock import patch

import httpx
import pytest
from pytest_httpx import HTTPXMock
from typer.testing import CliRunner

from goblin_watcher import config, linear_transitions, state
from goblin_watcher.cli import app
from goblin_watcher.errors import LinearAuthError
from goblin_watcher.models import LinearIssue, Project, Task

_STATES = [
    {"id": "state-todo", "name": "Todo"},
    {"id": "state-doing", "name": "In Progress"},
    {"id": "state-review", "name": "In Review"},
]


def _workflow_response(current: str = "Todo") -> dict:
    return {
        "data": {
            "issues": {
                "nodes": [
                    {
                        "id": "issue-uuid",
                        "state": {"id": "state-todo", "name": current},
                        "team": {"key": "ENG", "states": {"nodes": _STATES}},
                    }
                ]
            }
        }
    }


def _init_repo(path: Path) -> None:
    path.mkdir(parents=True, exist_ok=True)
    subprocess.run(["git", "init", "-q", "-b", "main", str(path)], check=True)
    subprocess.run(["git", "-C", str(path), "config", "user.email", "t@t"], check=True)
    subprocess.run(["git", "-C", str(path), "config", "user.name", "tester"], check=True)
    (path / "README.md").write_text("hi")
    subprocess.run(["git", "-C", str(path), "add", "."], check=True)
    subprocess.run(["git", "-C", str(path), "commit", "-qm", "init"], check=True)


def _bootstrap(tmp_path: Path, *, linear_state: str = "Todo") -> tuple[Project, Task]:
    """A registered project with one task carrying a Linear ticket in `linear_state`."""
    repo = tmp_path / "alpha"
    _init_repo(repo)
    runner = CliRunner()
    runner.invoke(app, ["project", "new", "alpha", "--dir", str(repo)])
    runner.invoke(app, ["new", "--branch-name", "eng-1-add-thing", "--no-launch"])
    proj = state.get_project("alpha")
    [task] = state.list_tasks(proj)
    task = task.model_copy(
        update={
            "linear": LinearIssue(
                id="issue-uuid",
                identifier="ENG-1",
                title="Add thing",
                state=linear_state,
                team_key="ENG",
                url="https://linear.app/x/issue/ENG-1",
            )
        }
    )
    state.save_task(proj, task)
    return proj, task


def _configure(**transitions: object) -> None:
    cfg = config.load()
    cfg.linear.transitions = config.LinearTransitionsConfig.model_validate(transitions)
    config.save(cfg)


@pytest.fixture
def api_key() -> Iterator[None]:
    with patch("goblin_watcher.linear_transitions.secrets.get_linear_api_key", return_value="k"):
        yield


def _variables(request: httpx.Request) -> dict:
    return json.loads(request.content)["variables"]


def _flat(text: str) -> str:
    """Console output with Rich's soft wrapping collapsed back to one line."""
    return " ".join(text.split())


def test_unset_config_makes_no_request(
    isolated_xdg: Path, tmp_path: Path, httpx_mock: HTTPXMock, api_key: None
) -> None:
    """The default is the read-only posture: no config key, no network at all."""
    proj, task = _bootstrap(tmp_path)

    out = linear_transitions.apply(proj, task, "on_session_start")

    assert out is task
    assert httpx_mock.get_requests() == []


def test_task_without_a_ticket_is_a_no_op(
    isolated_xdg: Path, tmp_path: Path, httpx_mock: HTTPXMock, api_key: None
) -> None:
    _configure(on_session_start="In Progress")
    proj, task = _bootstrap(tmp_path)
    task = task.model_copy(update={"linear": None})

    assert linear_transitions.apply(proj, task, "on_session_start") is task
    assert httpx_mock.get_requests() == []


def test_moves_the_ticket_and_caches_the_new_state(
    isolated_xdg: Path, tmp_path: Path, httpx_mock: HTTPXMock, api_key: None
) -> None:
    _configure(on_session_start="In Progress")
    proj, task = _bootstrap(tmp_path)
    httpx_mock.add_response(json=_workflow_response("Todo"))
    httpx_mock.add_response(json={"data": {"issueUpdate": {"success": True}}})

    out = linear_transitions.apply(proj, task, "on_session_start")

    lookup, mutation = httpx_mock.get_requests()
    assert _variables(lookup) == {"team": "ENG", "number": 1.0}
    assert "issueUpdate" in json.loads(mutation.content)["query"]
    assert _variables(mutation) == {"issueId": "issue-uuid", "stateId": "state-doing"}
    # The task's cached state follows the move, on disk and not just in memory.
    assert out.linear is not None and out.linear.state == "In Progress"
    assert out.linear_state_updated_at is not None
    reloaded = state.load_task(proj, task.id)
    assert reloaded.linear is not None and reloaded.linear.state == "In Progress"


def test_state_name_matches_case_insensitively(
    isolated_xdg: Path, tmp_path: Path, httpx_mock: HTTPXMock, api_key: None
) -> None:
    _configure(on_pr_open="in review")
    proj, task = _bootstrap(tmp_path)
    httpx_mock.add_response(json=_workflow_response("In Progress"))
    httpx_mock.add_response(json={"data": {"issueUpdate": {"success": True}}})

    out = linear_transitions.apply(proj, task, "on_pr_open")

    _, mutation = httpx_mock.get_requests()
    assert _variables(mutation)["stateId"] == "state-review"
    # The team's own spelling wins over the config's.
    assert out.linear is not None and out.linear.state == "In Review"


def test_already_in_the_target_state_skips_the_write(
    isolated_xdg: Path, tmp_path: Path, httpx_mock: HTTPXMock, api_key: None
) -> None:
    """Resuming a session all day must not spam the ticket's activity feed."""
    _configure(on_session_start="In Progress")
    proj, task = _bootstrap(tmp_path, linear_state="In Progress")
    httpx_mock.add_response(json=_workflow_response("In Progress"))

    linear_transitions.apply(proj, task, "on_session_start")

    assert len(httpx_mock.get_requests()) == 1


def test_each_trigger_reads_only_its_own_key(
    isolated_xdg: Path, tmp_path: Path, httpx_mock: HTTPXMock, api_key: None
) -> None:
    _configure(on_pr_open="In Review")
    proj, task = _bootstrap(tmp_path)

    assert linear_transitions.apply(proj, task, "on_session_start") is task
    assert httpx_mock.get_requests() == []

    httpx_mock.add_response(json=_workflow_response("Todo"))
    httpx_mock.add_response(json={"data": {"issueUpdate": {"success": True}}})
    linear_transitions.apply(proj, task, "on_pr_open")
    assert len(httpx_mock.get_requests()) == 2


def test_blank_state_name_reads_as_unset(isolated_xdg: Path) -> None:
    _configure(on_session_start="   ")
    assert linear_transitions.target_state("on_session_start") is None


def test_unknown_state_name_warns_and_writes_nothing(
    isolated_xdg: Path,
    tmp_path: Path,
    httpx_mock: HTTPXMock,
    api_key: None,
    capsys: pytest.CaptureFixture[str],
) -> None:
    _configure(on_session_start="Doing")
    proj, task = _bootstrap(tmp_path)
    httpx_mock.add_response(json=_workflow_response("Todo"))

    out = linear_transitions.apply(proj, task, "on_session_start")

    assert len(httpx_mock.get_requests()) == 1  # the lookup only; no mutation.
    assert out is task
    printed = _flat(capsys.readouterr().out)
    assert "Skipped Linear transition" in printed
    assert "In Progress" in printed  # the available states are named.


def test_api_failure_warns_and_returns_the_task(
    isolated_xdg: Path,
    tmp_path: Path,
    httpx_mock: HTTPXMock,
    api_key: None,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """A Linear that is down (or slow enough to time out) must never block a session."""
    _configure(on_session_start="In Progress")
    proj, task = _bootstrap(tmp_path)
    httpx_mock.add_response(status_code=503, json={"errors": [{"message": "unavailable"}]})

    out = linear_transitions.apply(proj, task, "on_session_start")

    assert out.linear is not None and out.linear.state == "Todo"
    assert "Skipped Linear transition" in _flat(capsys.readouterr().out)


def test_unconfirmed_mutation_warns(
    isolated_xdg: Path,
    tmp_path: Path,
    httpx_mock: HTTPXMock,
    api_key: None,
    capsys: pytest.CaptureFixture[str],
) -> None:
    _configure(on_pr_open="In Review")
    proj, task = _bootstrap(tmp_path)
    httpx_mock.add_response(json=_workflow_response("Todo"))
    httpx_mock.add_response(json={"data": {"issueUpdate": {"success": False}}})

    out = linear_transitions.apply(proj, task, "on_pr_open")

    assert out.linear is not None and out.linear.state == "Todo"
    assert "did not confirm" in _flat(capsys.readouterr().out)


def test_missing_api_key_warns_without_touching_the_network(
    isolated_xdg: Path, tmp_path: Path, httpx_mock: HTTPXMock, capsys: pytest.CaptureFixture[str]
) -> None:
    _configure(on_session_start="In Progress")
    proj, task = _bootstrap(tmp_path)

    with patch(
        "goblin_watcher.linear_transitions.secrets.get_linear_api_key",
        side_effect=LinearAuthError("No Linear API key."),
    ):
        out = linear_transitions.apply(proj, task, "on_session_start")

    assert out is task
    assert httpx_mock.get_requests() == []
    assert "Skipped Linear transition" in _flat(capsys.readouterr().out)


def test_timeout_is_read_from_config(isolated_xdg: Path, tmp_path: Path, api_key: None) -> None:
    """The client is built with the configured cap, so a slow Linear gives up."""
    _configure(on_session_start="In Progress", timeout_seconds=2.5)
    proj, task = _bootstrap(tmp_path)

    with patch("goblin_watcher.linear_transitions.LinearClient", autospec=True) as client_cls:
        client_cls.return_value.__enter__.return_value.fetch_issue_workflow.side_effect = (
            RuntimeError("stop here")
        )
        linear_transitions.apply(proj, task, "on_session_start")

    assert client_cls.call_args.kwargs["timeout"] == 2.5


def test_config_key_is_settable_through_gw_config_set(isolated_xdg: Path) -> None:
    runner = CliRunner()
    res = runner.invoke(app, ["config", "set", "linear.transitions.on_pr_open", "In Review"])
    assert res.exit_code == 0, res.output
    assert config.load().linear.transitions.on_pr_open == "In Review"


def test_session_start_hook_fires_from_gw_run(
    isolated_xdg: Path, tmp_path: Path, httpx_mock: HTTPXMock, api_key: None
) -> None:
    """`gw run` moves the ticket before handing the terminal to the agent."""
    _configure(on_session_start="In Progress")
    _, task = _bootstrap(tmp_path)
    httpx_mock.add_response(json=_workflow_response("Todo"))
    httpx_mock.add_response(json={"data": {"issueUpdate": {"success": True}}})

    runner = CliRunner()
    with patch("goblin_watcher.commands.run.launch_agent", return_value=(0, task)) as launch_agent:
        res = runner.invoke(app, ["run", task.id, "--new"])

    assert res.exit_code == 0, res.output
    assert len(httpx_mock.get_requests()) == 2
    # The launcher gets the task carrying the state gw just moved it to.
    assert launch_agent.call_args.kwargs["task"].linear.state == "In Progress"


def test_gw_run_launches_even_when_linear_is_unreachable(
    isolated_xdg: Path, tmp_path: Path, httpx_mock: HTTPXMock, api_key: None
) -> None:
    _configure(on_session_start="In Progress")
    _, task = _bootstrap(tmp_path)
    httpx_mock.add_response(status_code=500, json={"errors": [{"message": "boom"}]})

    runner = CliRunner()
    with patch("goblin_watcher.commands.run.launch_agent", return_value=(0, task)) as launch_agent:
        res = runner.invoke(app, ["run", task.id, "--new"])

    assert res.exit_code == 0, res.output
    launch_agent.assert_called_once()


def test_task_record_carries_the_timestamp_for_the_status_cache(
    isolated_xdg: Path, tmp_path: Path, httpx_mock: HTTPXMock, api_key: None
) -> None:
    """The cached-state write reuses `linear_state_updated_at`, so `gw status`
    doesn't immediately re-fetch what gw already knows."""
    _configure(on_session_start="In Progress")
    proj, task = _bootstrap(tmp_path)
    before = datetime.now(UTC)
    httpx_mock.add_response(json=_workflow_response("Todo"))
    httpx_mock.add_response(json={"data": {"issueUpdate": {"success": True}}})

    out = linear_transitions.apply(proj, task, "on_session_start")

    assert out.linear_state_updated_at is not None
    assert out.linear_state_updated_at >= before
