"""CLI wiring for worktree setup: `gw new`, `gw scratch`, `gw task setup` (gh-14)."""

from __future__ import annotations

import subprocess
from pathlib import Path

from typer.testing import CliRunner

from goblin_watcher import paths, state
from goblin_watcher.cli import app


def _init_repo(path: Path) -> None:
    path.mkdir(parents=True, exist_ok=True)
    subprocess.run(["git", "init", "-q", "-b", "main", str(path)], check=True)
    subprocess.run(["git", "-C", str(path), "config", "user.email", "t@t"], check=True)
    subprocess.run(["git", "-C", str(path), "config", "user.name", "tester"], check=True)
    (path / "README.md").write_text("hi")
    subprocess.run(["git", "-C", str(path), "add", "."], check=True)
    subprocess.run(["git", "-C", str(path), "commit", "-qm", "init"], check=True)


def _write_config(body: str) -> None:
    f = paths.config_file()
    f.parent.mkdir(parents=True, exist_ok=True)
    f.write_text(body)


def _register(tmp_path: Path) -> Path:
    repo = tmp_path / "alpha"
    _init_repo(repo)
    CliRunner().invoke(app, ["project", "new", "alpha", "--dir", str(repo)])
    return repo


def test_new_applies_setup_to_the_fresh_worktree(isolated_xdg: Path, tmp_path: Path) -> None:
    repo = _register(tmp_path)
    (repo / ".env").write_text("TOKEN=1")
    _write_config('[setup]\ncopy = [".env"]\nrun = ["touch bootstrapped"]\n')

    res = CliRunner().invoke(app, ["new", "--branch-name", "spike/foo", "--no-launch"])
    assert res.exit_code == 0, res.output

    [task] = state.list_tasks(state.get_project("alpha"))
    assert (task.worktree_path / ".env").read_text() == "TOKEN=1"
    assert (task.worktree_path / "bootstrapped").exists()


def test_no_setup_skips_it(isolated_xdg: Path, tmp_path: Path) -> None:
    repo = _register(tmp_path)
    (repo / ".env").write_text("TOKEN=1")
    _write_config('[setup]\ncopy = [".env"]\n')

    res = CliRunner().invoke(
        app, ["new", "--branch-name", "spike/foo", "--no-launch", "--no-setup"]
    )
    assert res.exit_code == 0, res.output

    [task] = state.list_tasks(state.get_project("alpha"))
    assert not (task.worktree_path / ".env").exists()


def test_a_failed_setup_fails_the_command_but_keeps_the_task(
    isolated_xdg: Path, tmp_path: Path
) -> None:
    _register(tmp_path)
    _write_config('[setup]\nrun = ["exit 4"]\n')

    res = CliRunner().invoke(app, ["new", "--branch-name", "spike/foo", "--no-launch"])
    assert res.exit_code != 0
    assert "setup failed" in str(res.exception).lower()

    # The worktree and record survive so `gw task setup` can retry.
    [task] = state.list_tasks(state.get_project("alpha"))
    assert task.worktree_path.exists()


def test_dir_source_does_not_run_setup(isolated_xdg: Path, tmp_path: Path) -> None:
    """An adopted checkout is the user's own; setup would clobber it."""
    repo = _register(tmp_path)
    _write_config('[setup]\nrun = ["touch bootstrapped"]\n')

    res = CliRunner().invoke(app, ["new", "--dir", str(repo), "--no-launch"])
    assert res.exit_code == 0, res.output
    assert not (repo / "bootstrapped").exists()


def test_task_setup_re_runs_the_steps(isolated_xdg: Path, tmp_path: Path) -> None:
    repo = _register(tmp_path)
    runner = CliRunner()
    res = runner.invoke(app, ["new", "--branch-name", "spike/foo", "--no-launch"])
    assert res.exit_code == 0, res.output

    # Configured only after the task exists, so this is purely the re-run path.
    (repo / ".env").write_text("late")
    _write_config('[setup]\ncopy = [".env"]\n')
    [task] = state.list_tasks(state.get_project("alpha"))
    assert not (task.worktree_path / ".env").exists()

    res = runner.invoke(app, ["task", "setup", task.id])
    assert res.exit_code == 0, res.output
    assert (task.worktree_path / ".env").read_text() == "late"


def test_task_setup_reports_when_nothing_is_configured(isolated_xdg: Path, tmp_path: Path) -> None:
    _register(tmp_path)
    runner = CliRunner()
    runner.invoke(app, ["new", "--branch-name", "spike/foo", "--no-launch"])
    [task] = state.list_tasks(state.get_project("alpha"))

    res = runner.invoke(app, ["task", "setup", task.id])
    assert res.exit_code == 0, res.output
    assert "No setup configured" in res.output


def test_task_setup_rejects_an_unknown_repo(isolated_xdg: Path, tmp_path: Path) -> None:
    _register(tmp_path)
    runner = CliRunner()
    runner.invoke(app, ["new", "--branch-name", "spike/foo", "--no-launch"])
    [task] = state.list_tasks(state.get_project("alpha"))

    res = runner.invoke(app, ["task", "setup", task.id, "--repo", "beta"])
    assert res.exit_code != 0
    assert "no repo for project" in str(res.exception).lower()


def test_scratch_applies_setup_from_the_scratch_root(isolated_xdg: Path) -> None:
    _write_config('[setup]\ncopy = [".env"]\nrun = ["touch bootstrapped"]\n')
    scratch_root = paths.scratch_root()
    scratch_root.mkdir(parents=True, exist_ok=True)
    (scratch_root / ".env").write_text("shared")

    res = CliRunner().invoke(app, ["scratch", "pad", "--no-launch"])
    assert res.exit_code == 0, res.output

    space = scratch_root / "pad"
    assert (space / ".env").read_text() == "shared"
    assert (space / "bootstrapped").exists()


def test_multi_repo_setup_uses_each_project_s_own_config(
    isolated_xdg: Path, tmp_path: Path
) -> None:
    _register(tmp_path)
    repo_b = tmp_path / "beta"
    _init_repo(repo_b)
    runner = CliRunner()
    runner.invoke(app, ["project", "new", "beta", "--dir", str(repo_b)])

    # Global config for alpha; beta overrides it with its own file.
    _write_config('[setup]\nrun = ["touch from-global"]\n')
    beta_setup = paths.project_setup_file(repo_b)
    beta_setup.parent.mkdir(parents=True, exist_ok=True)
    beta_setup.write_text('run = ["touch from-beta"]\n')

    res = runner.invoke(
        app,
        [
            "new",
            "--project",
            "alpha",
            "--branch-name",
            "spike/foo",
            "--with-project",
            "beta",
            "--no-launch",
        ],
    )
    assert res.exit_code == 0, res.output

    [task] = state.list_tasks(state.get_project("alpha"))
    primary, secondary = task.all_repos()
    assert (primary.worktree_path / "from-global").exists()
    assert (secondary.worktree_path / "from-beta").exists()
    assert not (secondary.worktree_path / "from-global").exists()


def test_a_config_escape_is_refused_before_anything_is_copied(
    isolated_xdg: Path, tmp_path: Path
) -> None:
    _register(tmp_path)
    (tmp_path / "outside.txt").write_text("secret")
    _write_config('[setup]\ncopy = ["README.md", "../outside.txt"]\n')

    res = CliRunner().invoke(app, ["new", "--branch-name", "spike/foo", "--no-launch"])
    assert res.exit_code != 0
    assert "escapes the project root" in str(res.exception)

    [task] = state.list_tasks(state.get_project("alpha"))
    # README.md is tracked, so it's in the worktree regardless — assert the
    # *escape* never landed rather than that nothing ran.
    assert not (task.worktree_path / "outside.txt").exists()
