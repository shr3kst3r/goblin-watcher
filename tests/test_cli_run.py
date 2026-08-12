"""Tests for `gw run`, covering the --project filter."""

import subprocess
from pathlib import Path
from unittest.mock import patch

from typer.testing import CliRunner

from goblin_watcher import state
from goblin_watcher.cli import app
from goblin_watcher.models import Task


def _init_repo(path: Path) -> None:
    path.mkdir(parents=True, exist_ok=True)
    subprocess.run(["git", "init", "-q", "-b", "main", str(path)], check=True)
    subprocess.run(["git", "-C", str(path), "config", "user.email", "t@t"], check=True)
    subprocess.run(["git", "-C", str(path), "config", "user.name", "tester"], check=True)
    (path / "README.md").write_text("hi")
    subprocess.run(["git", "-C", str(path), "add", "."], check=True)
    subprocess.run(["git", "-C", str(path), "commit", "-qm", "init"], check=True)


def _bootstrap_two_projects(tmp_path: Path) -> None:
    repo_a = tmp_path / "alpha"
    repo_b = tmp_path / "beta"
    _init_repo(repo_a)
    _init_repo(repo_b)
    runner = CliRunner()
    runner.invoke(app, ["project", "new", "alpha", "--dir", str(repo_a)])
    runner.invoke(app, ["project", "new", "beta", "--dir", str(repo_b)])
    runner.invoke(app, ["new", "--branch-name", "spike/foo", "--project", "alpha", "--no-launch"])
    runner.invoke(app, ["new", "--branch-name", "spike/foo", "--project", "beta", "--no-launch"])


def test_run_project_flag_scopes_task_lookup(isolated_xdg: Path, tmp_path: Path) -> None:
    """A task id that exists in two projects resolves to the --project one."""
    _bootstrap_two_projects(tmp_path)
    proj_a = state.get_project("alpha")
    proj_b = state.get_project("beta")
    [task_a] = state.list_tasks(proj_a)
    [task_b] = state.list_tasks(proj_b)
    assert task_a.id == task_b.id  # shared "spike-foo" id; only --project disambiguates.

    runner = CliRunner()
    with patch(
        "goblin_watcher.commands.run.launch_agent",
        return_value=(0, task_b),
    ) as launch:
        res = runner.invoke(app, ["run", task_b.id, "--project", "beta"])
    assert res.exit_code == 0, res.output
    kwargs = launch.call_args.kwargs
    assert kwargs["project"].name == "beta"
    assert kwargs["task"].id == task_b.id


def test_run_ambiguous_task_id_errors_without_project(isolated_xdg: Path, tmp_path: Path) -> None:
    """Without --project, a task id shared across projects errors instead of
    silently resolving to whichever project registered first."""
    from goblin_watcher.errors import GoblinError

    _bootstrap_two_projects(tmp_path)
    runner = CliRunner()
    with patch("goblin_watcher.commands.run.launch_agent") as launch:
        res = runner.invoke(app, ["run", "spike-foo"])
    assert res.exit_code != 0
    assert isinstance(res.exception, GoblinError)
    assert "more than one project" in res.exception.message
    assert "alpha" in res.exception.message
    assert "beta" in res.exception.message
    assert res.exception.hint is not None
    assert "--project" in res.exception.hint
    launch.assert_not_called()


def test_run_project_flag_unknown_errors(isolated_xdg: Path, tmp_path: Path) -> None:
    _bootstrap_two_projects(tmp_path)
    runner = CliRunner()
    res = runner.invoke(app, ["run", "spike-foo", "--project", "nope"])
    assert res.exit_code != 0


def test_run_new_and_session_are_mutually_exclusive(isolated_xdg: Path, tmp_path: Path) -> None:
    _bootstrap_two_projects(tmp_path)
    runner = CliRunner()
    res = runner.invoke(app, ["run", "spike-foo", "--project", "alpha", "--new", "--session", "x"])
    assert res.exit_code != 0
    assert res.exception is not None
    assert "mutually exclusive" in str(res.exception)


def test_run_prompt_and_session_are_mutually_exclusive(isolated_xdg: Path, tmp_path: Path) -> None:
    _bootstrap_two_projects(tmp_path)
    runner = CliRunner()
    res = runner.invoke(
        app,
        ["run", "spike-foo", "--project", "alpha", "--session", "x", "--prompt", "do work"],
    )
    assert res.exit_code != 0
    assert res.exception is not None
    assert "--prompt requires a fresh session" in str(res.exception)


def test_run_prompt_implies_fresh_and_seeds_prompt(isolated_xdg: Path, tmp_path: Path) -> None:
    _bootstrap_two_projects(tmp_path)
    proj_a = state.get_project("alpha")
    [task_a] = state.list_tasks(proj_a)

    runner = CliRunner()
    with patch(
        "goblin_watcher.commands.run.launch_agent",
        return_value=(0, task_a),
    ) as launch:
        res = runner.invoke(
            app,
            [
                "run",
                task_a.id,
                "--project",
                "alpha",
                "--prompt",
                "Refactor the foo module.",
            ],
        )
    assert res.exit_code == 0, res.output
    choice = launch.call_args.kwargs["choice"]
    # Fresh, not Resume — --prompt implies a new session.
    assert type(choice).__name__ == "Fresh"
    assert "Refactor the foo module." in choice.prompt
    assert "Wait for my next message" not in choice.prompt


def test_run_adversarial_review_seeds_slash_command(isolated_xdg: Path, tmp_path: Path) -> None:
    _bootstrap_two_projects(tmp_path)
    proj_a = state.get_project("alpha")
    [task_a] = state.list_tasks(proj_a)

    runner = CliRunner()
    with patch(
        "goblin_watcher.commands.run.launch_agent",
        return_value=(0, task_a),
    ) as launch:
        res = runner.invoke(
            app,
            ["run", task_a.id, "--project", "alpha", "--adversarial-review"],
        )
    assert res.exit_code == 0, res.output
    choice = launch.call_args.kwargs["choice"]
    # Fresh, not Resume — adversarial review always starts a new session.
    assert type(choice).__name__ == "Fresh"
    # Slash command must be the entire user message, not buried in the
    # seed template — Claude Code's parser only fires it then.
    assert choice.prompt == "/codex:adversarial-review --wait"
    # The agent should resolve to claude regardless of config default.
    assert launch.call_args.kwargs["agent"].name == "claude"


def test_run_adversarial_review_conflicts_with_session(isolated_xdg: Path, tmp_path: Path) -> None:
    _bootstrap_two_projects(tmp_path)
    runner = CliRunner()
    res = runner.invoke(
        app,
        ["run", "spike-foo", "--project", "alpha", "--adversarial-review", "--session", "x"],
    )
    assert res.exit_code != 0
    assert res.exception is not None
    assert "mutually exclusive" in str(res.exception)


def test_run_adversarial_review_conflicts_with_prompt(isolated_xdg: Path, tmp_path: Path) -> None:
    _bootstrap_two_projects(tmp_path)
    runner = CliRunner()
    res = runner.invoke(
        app,
        ["run", "spike-foo", "--project", "alpha", "--adversarial-review", "--prompt", "do work"],
    )
    assert res.exit_code != 0
    assert res.exception is not None
    assert "mutually exclusive" in str(res.exception)


def test_run_adversarial_review_rejects_non_claude_agent(
    isolated_xdg: Path, tmp_path: Path
) -> None:
    _bootstrap_two_projects(tmp_path)
    runner = CliRunner()
    res = runner.invoke(
        app,
        ["run", "spike-foo", "--project", "alpha", "--adversarial-review", "--agent", "codex"],
    )
    assert res.exit_code != 0
    assert res.exception is not None
    assert "requires --agent claude" in str(res.exception)


def _issue_backed_alpha_task(tmp_path: Path) -> Task:
    """`spike/foo` in alpha, retrofitted with a GitHub issue to research."""
    from goblin_watcher.models import GhIssue

    _bootstrap_two_projects(tmp_path)
    proj = state.get_project("alpha")
    [task] = state.list_tasks(proj)
    task = task.model_copy(
        update={
            "github_issue": GhIssue(
                number=11,
                repo="org/repo",
                title="Add a research option",
                body="Investigate the ticket and report back.",
                state="OPEN",
                url="https://github.com/org/repo/issues/11",
            )
        }
    )
    state.save_task(proj, task)
    return task


def test_run_research_seeds_the_research_brief(isolated_xdg: Path, tmp_path: Path) -> None:
    task = _issue_backed_alpha_task(tmp_path)

    runner = CliRunner()
    with patch(
        "goblin_watcher.commands.run.launch_agent",
        return_value=(0, task),
    ) as launch:
        res = runner.invoke(app, ["run", task.id, "--project", "alpha", "--research"])
    assert res.exit_code == 0, res.output
    choice = launch.call_args.kwargs["choice"]
    # Fresh, not Resume — research always starts a new session.
    assert type(choice).__name__ == "Fresh"
    assert choice.prompt.startswith("Research task —")
    assert "org/repo#11: Add a research option" in choice.prompt
    assert "Investigate the ticket and report back." in choice.prompt
    assert "open a PR via" not in choice.prompt


def test_run_research_prompt_narrows_the_focus(isolated_xdg: Path, tmp_path: Path) -> None:
    """--prompt composes with --research instead of conflicting with it."""
    task = _issue_backed_alpha_task(tmp_path)

    runner = CliRunner()
    with patch(
        "goblin_watcher.commands.run.launch_agent",
        return_value=(0, task),
    ) as launch:
        res = runner.invoke(
            app,
            ["run", task.id, "--project", "alpha", "--research", "--prompt", "Only the sync path."],
        )
    assert res.exit_code == 0, res.output
    choice = launch.call_args.kwargs["choice"]
    assert "Focus this research on the following" in choice.prompt
    assert "Only the sync path." in choice.prompt
    assert "open a PR via" not in choice.prompt


def test_run_research_conflicts_with_session(isolated_xdg: Path, tmp_path: Path) -> None:
    """Both `--session <id>` and the bare-`--session` picker sentinel (spliced in
    by `cli._inject_session_pick_sentinel`) are refused, so `--research` can
    never reach a picker branch."""
    from goblin_watcher.picker import SESSION_PICK_SENTINEL

    _bootstrap_two_projects(tmp_path)
    runner = CliRunner()
    for value in ("x", SESSION_PICK_SENTINEL):
        with patch("goblin_watcher.commands.run.choose_session") as picker:
            res = runner.invoke(
                app, ["run", "spike-foo", "--project", "alpha", "--research", "--session", value]
            )
        assert res.exit_code != 0
        assert res.exception is not None
        assert "--research and --session are mutually exclusive" in str(res.exception), value
        picker.assert_not_called()


def test_run_research_conflicts_with_adversarial_review(isolated_xdg: Path, tmp_path: Path) -> None:
    _bootstrap_two_projects(tmp_path)
    runner = CliRunner()
    res = runner.invoke(
        app, ["run", "spike-foo", "--project", "alpha", "--research", "--adversarial-review"]
    )
    assert res.exit_code != 0
    assert res.exception is not None
    assert "--research and --adversarial-review are mutually exclusive" in str(res.exception)


def test_run_research_and_adversarial_review_conflict_is_reported_first(
    isolated_xdg: Path, tmp_path: Path
) -> None:
    """The mode conflict outranks --adversarial-review's own checks: --agent and
    --prompt are both fine with --research, so pointing at them would send the
    user off fixing the wrong flag."""
    _bootstrap_two_projects(tmp_path)
    runner = CliRunner()
    for extra in (["--agent", "codex"], ["--prompt", "focus on sync"]):
        args = ["run", "spike-foo", "--project", "alpha", "--research", "--adversarial-review"]
        res = runner.invoke(app, [*args, *extra])
        assert res.exit_code != 0
        assert res.exception is not None
        assert "--research and --adversarial-review are mutually exclusive" in str(res.exception), (
            extra
        )


def test_run_research_requires_a_tracking_item(isolated_xdg: Path, tmp_path: Path) -> None:
    _bootstrap_two_projects(tmp_path)
    runner = CliRunner()
    with patch("goblin_watcher.commands.run.launch_agent") as launch:
        res = runner.invoke(app, ["run", "spike-foo", "--project", "alpha", "--research"])
    assert res.exit_code != 0
    assert res.exception is not None
    assert "has no Linear ticket or GitHub issue to research" in str(res.exception)
    launch.assert_not_called()


def test_run_project_flag_task_missing_in_scope_errors(isolated_xdg: Path, tmp_path: Path) -> None:
    """A task id present in alpha but not beta must not silently fall back to alpha
    when --project beta is set."""
    _bootstrap_two_projects(tmp_path)
    proj_b = state.get_project("beta")
    [task_b] = state.list_tasks(proj_b)
    # Add a second task only to alpha.
    runner = CliRunner()
    runner.invoke(
        app, ["new", "--branch-name", "spike/only-alpha", "--project", "alpha", "--no-launch"]
    )

    res = runner.invoke(app, ["run", "spike-only-alpha", "--project", "beta"])
    assert res.exit_code != 0
    assert "spike-only-alpha" in res.output or (
        res.exception is not None and "spike-only-alpha" in str(res.exception)
    )
    # Sanity: the same id resolves fine without the filter.
    with patch(
        "goblin_watcher.commands.run.launch_agent",
        return_value=(0, task_b),
    ):
        res2 = runner.invoke(app, ["run", "spike-only-alpha"])
    assert res2.exit_code == 0, res2.output
