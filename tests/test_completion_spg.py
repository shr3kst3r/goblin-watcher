"""Tests for the spg-protocol completion hook.

spg's hook contract: receives `<cursor-index> <word1> <word2> ...` where
`cursor-index` is the 0-indexed position in the original zsh `$words`
array (words[0] = "gw") and `word1..wordN` are `words[1:]`. The hook
prints `value:description` (or bare `value`) lines.
"""

from datetime import UTC, datetime
from pathlib import Path

import pytest
from typer.testing import CliRunner

from goblin_watcher import state
from goblin_watcher.cli import app
from goblin_watcher.completion_spg import run as run_hook
from goblin_watcher.models import Project, SessionRecord, Task


def _make_project(root: Path, name: str = "alpha") -> Project:
    return Project(
        name=name,
        root=root,
        repo_url=None,
        default_branch="main",
        branch_prefix="",
        created_at=datetime.now(UTC),
    )


def _make_task(
    project_name: str,
    task_id: str,
    worktree: Path,
    sessions: list[SessionRecord] | None = None,
) -> Task:
    return Task(
        id=task_id,
        project=project_name,
        branch=task_id,
        worktree_path=worktree,
        base_branch="main",
        created_at=datetime.now(UTC),
        sessions=sessions or [],
    )


def _values_of(lines: list[str]) -> list[str]:
    """Strip the ':description' suffix that spg's protocol allows."""
    return [line.split(":", 1)[0] for line in lines if line.strip()]


@pytest.fixture
def populated_state(isolated_xdg: Path, tmp_path: Path) -> None:
    for name in ("alpha", "beta"):
        root = tmp_path / name
        root.mkdir()
        state.register_project(_make_project(root, name))
    proj = state.get_project("alpha")
    state.save_task(proj, _make_task("alpha", "eng-1", tmp_path / "wt1"))
    state.save_task(proj, _make_task("alpha", "eng-2", tmp_path / "wt2"))


def test_root_subcommands(isolated_xdg: Path, capsys: pytest.CaptureFixture[str]) -> None:
    # `gw <TAB>`: words=["gw", ""], CURRENT=2 → index=1, args=[""]
    run_hook(1, [""])
    lines = capsys.readouterr().out.splitlines()
    values = _values_of(lines)
    assert "new" in values
    assert "task" in values
    assert "project" in values
    # Hidden commands (e.g. `__complete`, `_describe`) must not surface.
    assert "__complete" not in values
    assert "_describe" not in values


def test_group_subcommands(isolated_xdg: Path, capsys: pytest.CaptureFixture[str]) -> None:
    # `gw task <TAB>`: words=["gw", "task", ""], CURRENT=3 → index=2
    run_hook(2, ["task", ""])
    values = _values_of(capsys.readouterr().out.splitlines())
    assert "ls" in values
    assert "rm" in values
    assert "show" in values


def test_flag_prefix_emits_flags(isolated_xdg: Path, capsys: pytest.CaptureFixture[str]) -> None:
    # `gw new --<TAB>` (use --li to avoid Click swallowing the bare `--`).
    run_hook(2, ["new", "--li"])
    values = _values_of(capsys.readouterr().out.splitlines())
    assert "--linear" in values
    assert "--branch" in values
    assert "--project" in values


def test_value_taking_flag_emits_projects(
    populated_state: None, capsys: pytest.CaptureFixture[str]
) -> None:
    # `gw new --project <TAB>`: words=["gw", "new", "--project", ""], CURRENT=4
    run_hook(3, ["new", "--project", ""])
    values = _values_of(capsys.readouterr().out.splitlines())
    assert values == ["alpha", "beta"]


def test_positional_task_id(populated_state: None, capsys: pytest.CaptureFixture[str]) -> None:
    # `gw task rm <TAB>`: words=["gw", "task", "rm", ""], CURRENT=4
    run_hook(3, ["task", "rm", ""])
    values = _values_of(capsys.readouterr().out.splitlines())
    # Task ids come first; flag fallback may follow.
    assert "eng-1" in values
    assert "eng-2" in values


def test_leaf_with_no_positional_emits_flags(
    isolated_xdg: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    # `gw new <TAB>`: `new` has no positional, fall back to flags.
    run_hook(2, ["new", ""])
    values = _values_of(capsys.readouterr().out.splitlines())
    assert "--linear" in values
    assert "--branch" in values


def test_empty_words_emits_root_subcommands(
    isolated_xdg: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    # Defensive: index=0 or empty args should still produce the top-level
    # subcommand list, not crash.
    run_hook(0, [])
    values = _values_of(capsys.readouterr().out.splitlines())
    assert "new" in values
    assert "task" in values


def test_cli_entrypoint_dispatches_to_hook(
    populated_state: None,
) -> None:
    """`gw __complete spg <index> [words...]` is the surface spg invokes."""
    res = CliRunner().invoke(app, ["__complete", "spg", "3", "new", "--project", ""])
    assert res.exit_code == 0, res.output
    values = _values_of(res.stdout.splitlines())
    assert values == ["alpha", "beta"]


def test_cli_entrypoint_ignores_unknown_flags(
    isolated_xdg: Path,
) -> None:
    """Click must pass through user-typed flags like `--li` instead of erroring."""
    res = CliRunner().invoke(app, ["__complete", "spg", "2", "new", "--li"])
    assert res.exit_code == 0, res.output
    values = _values_of(res.stdout.splitlines())
    assert "--linear" in values
