"""Tests for `gw scratch` — standalone scratch spaces not tied to any project."""

import re
import subprocess
from datetime import UTC, datetime
from pathlib import Path
from unittest.mock import patch

from click.testing import Result
from typer.testing import CliRunner

from goblin_watcher import paths, state
from goblin_watcher.agents.launcher import build_seed_prompt
from goblin_watcher.cli import app
from goblin_watcher.models import Project


def _init_repo(path: Path) -> None:
    path.mkdir(parents=True, exist_ok=True)
    subprocess.run(["git", "init", "-q", "-b", "main", str(path)], check=True)
    subprocess.run(["git", "-C", str(path), "config", "user.email", "t@t"], check=True)
    subprocess.run(["git", "-C", str(path), "config", "user.name", "tester"], check=True)
    (path / "README.md").write_text("hi")
    subprocess.run(["git", "-C", str(path), "add", "."], check=True)
    subprocess.run(["git", "-C", str(path), "commit", "-qm", "init"], check=True)


def _scratch(*args: str) -> Result:
    runner = CliRunner()
    return runner.invoke(app, ["scratch", *args])


def test_scratch_creates_dir_and_task(isolated_xdg: Path) -> None:
    res = _scratch("My Experiment", "--no-launch")
    assert res.exit_code == 0, res.output

    proj = state.get_project("scratch")
    assert proj.kind == "scratch"
    assert proj.root == paths.scratch_root()

    [task] = state.list_tasks(proj)
    assert task.kind == "scratch"
    assert task.id == "my-experiment"
    assert task.worktree_path == paths.scratch_root() / "my-experiment"
    assert task.worktree_path.is_dir()
    assert not (task.worktree_path / ".git").exists()


def test_scratch_auto_generates_name(isolated_xdg: Path) -> None:
    res = _scratch("--no-launch")
    assert res.exit_code == 0, res.output
    [task] = state.list_tasks(state.get_project("scratch"))
    assert re.fullmatch(r"[a-z]+-[a-z]+", task.id), task.id


def test_scratch_duplicate_name_gets_suffix(isolated_xdg: Path) -> None:
    assert _scratch("foo", "--no-launch").exit_code == 0
    assert _scratch("foo", "--no-launch").exit_code == 0
    proj = state.get_project("scratch")
    ids = {t.id for t in state.list_tasks(proj)}
    assert ids == {"foo", "foo-2"}
    assert (proj.root / "foo-2").is_dir()


def test_scratch_seed_prompt_has_no_pr_instructions(isolated_xdg: Path) -> None:
    assert _scratch("spike", "--no-launch").exit_code == 0
    [task] = state.list_tasks(state.get_project("scratch"))
    prompt = build_seed_prompt(task)
    assert "Scratch space: spike" in prompt
    assert str(task.worktree_path) in prompt
    assert "gw pr open" not in prompt
    assert "Wait for my next message" in prompt


def test_scratch_prompt_conflicts_with_no_launch(isolated_xdg: Path) -> None:
    res = _scratch("foo", "--prompt", "do things", "--no-launch")
    assert res.exit_code != 0


def test_scratch_launches_agent_in_directory(isolated_xdg: Path) -> None:
    with patch("goblin_watcher.commands.scratch.launch", return_value=(0, None)) as launch:
        res = _scratch("foo")
    assert res.exit_code == 0, res.output
    kwargs = launch.call_args.kwargs
    assert kwargs["project"].name == "scratch"
    assert kwargs["task"].kind == "scratch"
    assert "Scratch space: foo" in kwargs["choice"].prompt


def test_scratch_rejects_managed_agent(isolated_xdg: Path) -> None:
    res = _scratch("foo", "--agent", "managed")
    assert res.exit_code != 0
    assert "managed agent" in str(res.exception)


def test_run_resumes_scratch_task(isolated_xdg: Path) -> None:
    assert _scratch("foo", "--no-launch").exit_code == 0
    runner = CliRunner()
    with patch("goblin_watcher.commands.run.launch_agent", return_value=(0, None)) as launch:
        res = runner.invoke(app, ["run", "foo", "--new"])
    assert res.exit_code == 0, res.output
    kwargs = launch.call_args.kwargs
    assert kwargs["task"].kind == "scratch"
    assert kwargs["project"].name == "scratch"


def test_new_rejects_scratch_project(isolated_xdg: Path) -> None:
    assert _scratch("foo", "--no-launch").exit_code == 0
    runner = CliRunner()
    res = runner.invoke(
        app, ["new", "--branch-name", "spike/x", "--project", "scratch", "--no-launch"]
    )
    assert res.exit_code != 0
    assert "scratch" in str(res.exception)


def test_project_new_rejects_reserved_name(isolated_xdg: Path, tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    _init_repo(repo)
    runner = CliRunner()
    res = runner.invoke(app, ["project", "new", "scratch", "--dir", str(repo)])
    assert res.exit_code != 0
    assert "reserved" in str(res.exception)


def test_scratch_blocked_by_preexisting_regular_project(isolated_xdg: Path, tmp_path: Path) -> None:
    """A user project that grabbed the name 'scratch' before it was reserved
    must produce a clear error instead of silently hosting scratch tasks."""
    repo = tmp_path / "oldscratch"
    _init_repo(repo)
    state.register_project(Project(name="scratch", root=repo, created_at=datetime.now(UTC)))
    res = _scratch("foo", "--no-launch")
    assert res.exit_code != 0
    assert "already registered" in str(res.exception)


def test_task_rm_scratch_removes_dir_and_record(isolated_xdg: Path) -> None:
    assert _scratch("doomed", "--no-launch").exit_code == 0
    proj = state.get_project("scratch")
    [task] = state.list_tasks(proj)
    (task.worktree_path / "notes.txt").write_text("work in progress")

    runner = CliRunner()
    res = runner.invoke(app, ["task", "rm", "doomed", "--force"])
    assert res.exit_code == 0, res.output
    assert not task.worktree_path.exists()
    assert state.list_tasks(proj) == []


def test_task_rm_scratch_confirm_warns_about_contents(isolated_xdg: Path) -> None:
    assert _scratch("doomed", "--no-launch").exit_code == 0
    runner = CliRunner()
    res = runner.invoke(app, ["task", "rm", "doomed"], input="n\n")
    assert res.exit_code != 0
    assert "permanently deletes" in res.output
    [task] = state.list_tasks(state.get_project("scratch"))
    assert task.worktree_path.exists()


def test_task_prune_skips_scratch(isolated_xdg: Path) -> None:
    assert _scratch("keepme", "--no-launch").exit_code == 0
    runner = CliRunner()
    res = runner.invoke(app, ["task", "prune", "--dry-run", "--no-fetch"])
    assert res.exit_code == 0, res.output
    assert "Nothing to prune" in res.output


def test_pr_open_rejects_scratch_task(isolated_xdg: Path) -> None:
    assert _scratch("foo", "--no-launch").exit_code == 0
    runner = CliRunner()
    res = runner.invoke(app, ["pr", "open", "foo"])
    assert res.exit_code != 0
    assert "scratch space" in str(res.exception)


def test_status_renders_scratch_task(isolated_xdg: Path) -> None:
    assert _scratch("foo", "--no-launch").exit_code == 0
    runner = CliRunner()
    res = runner.invoke(app, ["status", "--no-linear"])
    assert res.exit_code == 0, res.output
    assert "foo" in res.output


def test_project_pull_skips_scratch(isolated_xdg: Path) -> None:
    assert _scratch("foo", "--no-launch").exit_code == 0
    runner = CliRunner()
    res = runner.invoke(app, ["project", "pull"])
    assert res.exit_code == 0, res.output
    assert "scratch — skipped" in res.output
