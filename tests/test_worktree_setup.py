"""Unit coverage for the worktree bootstrap (gh-14, ADR 0007)."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

import pytest

from goblin_watcher import config, paths, worktree_setup
from goblin_watcher.errors import GoblinError
from goblin_watcher.models import Project


def _project(root: Path) -> Project:
    root.mkdir(parents=True, exist_ok=True)
    return Project(name="alpha", root=root, created_at=datetime.now(UTC))


def _write_config(body: str) -> None:
    f = paths.config_file()
    f.parent.mkdir(parents=True, exist_ok=True)
    f.write_text(body)


# --- containment ------------------------------------------------------------


def test_resolve_inside_accepts_a_nested_relative_path(tmp_path: Path) -> None:
    resolved = worktree_setup.resolve_inside(tmp_path, ".claude/settings.local.json", key="copy")
    assert resolved == (tmp_path / ".claude/settings.local.json").resolve()


@pytest.mark.parametrize(
    "entry",
    ["../outside", "a/../../outside", "/etc/passwd", ".", "", "   "],
)
def test_resolve_inside_refuses_escapes(tmp_path: Path, entry: str) -> None:
    with pytest.raises(GoblinError):
        worktree_setup.resolve_inside(tmp_path, entry, key="copy")


def test_resolve_inside_refuses_a_symlink_pointing_out_of_the_root(tmp_path: Path) -> None:
    root = tmp_path / "repo"
    root.mkdir()
    secret = tmp_path / "secret.txt"
    secret.write_text("nope")
    (root / ".env").symlink_to(secret)
    with pytest.raises(GoblinError, match="outside the project root"):
        worktree_setup.resolve_inside(root, ".env", key="copy")


# --- copy / link ------------------------------------------------------------


def test_copy_and_link_populate_the_worktree(isolated_xdg: Path, tmp_path: Path) -> None:
    root = tmp_path / "repo"
    proj = _project(root)
    (root / ".env").write_text("TOKEN=1")
    (root / "node_modules").mkdir()
    (root / "node_modules" / "pkg.txt").write_text("dep")
    dest = tmp_path / "worktree"
    dest.mkdir()
    _write_config('[setup]\ncopy = [".env"]\nlink = ["node_modules"]\n')

    result = worktree_setup.run_setup(proj, dest, task_id="eng-1")

    assert result.ok, result.steps
    assert (dest / ".env").read_text() == "TOKEN=1"
    assert (dest / "node_modules").is_symlink()
    assert (dest / "node_modules" / "pkg.txt").read_text() == "dep"


def test_copy_creates_missing_parent_directories(isolated_xdg: Path, tmp_path: Path) -> None:
    root = tmp_path / "repo"
    proj = _project(root)
    (root / ".claude").mkdir()
    (root / ".claude" / "settings.local.json").write_text("{}")
    dest = tmp_path / "worktree"
    dest.mkdir()
    _write_config('[setup]\ncopy = [".claude/settings.local.json"]\n')

    result = worktree_setup.run_setup(proj, dest)

    assert result.ok, result.steps
    assert (dest / ".claude" / "settings.local.json").read_text() == "{}"


def test_a_missing_source_is_skipped_not_failed(isolated_xdg: Path, tmp_path: Path) -> None:
    proj = _project(tmp_path / "repo")
    dest = tmp_path / "worktree"
    dest.mkdir()
    _write_config('[setup]\ncopy = [".env.local"]\n')

    result = worktree_setup.run_setup(proj, dest)

    assert result.ok
    assert [s.status for s in result.steps] == ["skipped"]


def test_copy_replaces_a_symlink_instead_of_writing_through_it(
    isolated_xdg: Path, tmp_path: Path
) -> None:
    """A re-run must not write back into the project root via a previous link."""
    root = tmp_path / "repo"
    proj = _project(root)
    (root / ".env").write_text("TOKEN=1")
    dest = tmp_path / "worktree"
    dest.mkdir()
    (dest / ".env").symlink_to(root / ".env")
    _write_config('[setup]\ncopy = [".env"]\n')

    result = worktree_setup.run_setup(proj, dest)

    assert result.ok, result.steps
    assert not (dest / ".env").is_symlink()


def test_link_refuses_to_replace_a_real_directory(isolated_xdg: Path, tmp_path: Path) -> None:
    root = tmp_path / "repo"
    proj = _project(root)
    (root / "node_modules").mkdir()
    dest = tmp_path / "worktree"
    (dest / "node_modules").mkdir(parents=True)
    _write_config('[setup]\nlink = ["node_modules"]\n')

    result = worktree_setup.run_setup(proj, dest)

    assert not result.ok
    assert "refusing to replace" in result.failed[0].detail


def test_a_source_containing_the_worktree_is_refused(isolated_xdg: Path, tmp_path: Path) -> None:
    root = tmp_path / "repo"
    proj = _project(root)
    (root / ".worktrees").mkdir()
    dest = root / ".worktrees" / "eng-1"
    dest.mkdir()
    _write_config('[setup]\ncopy = [".worktrees"]\n')

    with pytest.raises(GoblinError, match="contains the worktree"):
        worktree_setup.run_setup(proj, dest)


# --- run --------------------------------------------------------------------


def test_run_executes_in_the_worktree_with_gw_env(isolated_xdg: Path, tmp_path: Path) -> None:
    proj = _project(tmp_path / "repo")
    dest = tmp_path / "worktree"
    dest.mkdir()
    _write_config('[setup]\nrun = ["pwd > where.txt && echo $GW_TASK_ID > who.txt"]\n')

    result = worktree_setup.run_setup(proj, dest, task_id="eng-7")

    assert result.ok, result.steps
    assert Path((dest / "where.txt").read_text().strip()).resolve() == dest.resolve()
    assert (dest / "who.txt").read_text().strip() == "eng-7"


def test_an_argv_list_runs_without_a_shell(isolated_xdg: Path, tmp_path: Path) -> None:
    proj = _project(tmp_path / "repo")
    dest = tmp_path / "worktree"
    dest.mkdir()
    # `$HOME` stays literal because no shell expands it.
    _write_config('[setup]\nrun = [["sh", "-c", "echo ok > out.txt"], ["touch", "$HOME"]]\n')

    result = worktree_setup.run_setup(proj, dest)

    assert result.ok, result.steps
    assert (dest / "out.txt").read_text().strip() == "ok"
    assert (dest / "$HOME").exists()


def test_a_failing_step_skips_the_rest(isolated_xdg: Path, tmp_path: Path) -> None:
    proj = _project(tmp_path / "repo")
    dest = tmp_path / "worktree"
    dest.mkdir()
    _write_config('[setup]\nrun = ["echo boom >&2; exit 3", "touch never.txt"]\n')

    result = worktree_setup.run_setup(proj, dest)

    assert not result.ok
    assert [s.status for s in result.steps] == ["failed", "skipped"]
    assert result.failed[0].detail == "exit 3"
    assert "boom" in result.failed[0].output
    assert not (dest / "never.txt").exists()


def test_a_run_step_times_out(isolated_xdg: Path, tmp_path: Path) -> None:
    proj = _project(tmp_path / "repo")
    dest = tmp_path / "worktree"
    dest.mkdir()
    _write_config('[setup]\nrun = ["sleep 5"]\ntimeout_seconds = 1\n')

    result = worktree_setup.run_setup(proj, dest)

    assert not result.ok
    assert "timed out" in result.failed[0].detail


def test_a_command_containing_rich_markup_prints_without_exploding(
    isolated_xdg: Path, tmp_path: Path
) -> None:
    """A glob like `[a-z]*` in a command must not be eaten as Rich markup."""
    proj = _project(tmp_path / "repo")
    dest = tmp_path / "worktree"
    dest.mkdir()
    _write_config("[setup]\nrun = [\"echo '[unclosed tag' && ls [a-z]* ; exit 1\"]\n")

    result = worktree_setup.run_setup(proj, dest)

    assert not result.ok
    assert "[a-z]*" in result.failed[0].target


# --- config resolution ------------------------------------------------------


def test_nothing_configured_is_a_silent_no_op(isolated_xdg: Path, tmp_path: Path) -> None:
    result = worktree_setup.run_setup(_project(tmp_path / "repo"), tmp_path / "worktree")
    assert result.ok
    assert not result.ran_anything


def test_a_project_setup_file_replaces_the_global_table(isolated_xdg: Path, tmp_path: Path) -> None:
    root = tmp_path / "repo"
    proj = _project(root)
    (root / ".env").write_text("global")
    (root / "project-only").write_text("project")
    dest = tmp_path / "worktree"
    dest.mkdir()
    _write_config('[setup]\ncopy = [".env"]\n')
    project_file = paths.project_setup_file(root)
    project_file.parent.mkdir(parents=True, exist_ok=True)
    project_file.write_text('copy = ["project-only"]\n')

    result = worktree_setup.run_setup(proj, dest)

    assert result.ok, result.steps
    assert (dest / "project-only").exists()
    assert not (dest / ".env").exists()


def test_a_project_setup_file_may_nest_under_a_setup_table(
    isolated_xdg: Path, tmp_path: Path
) -> None:
    root = tmp_path / "repo"
    proj = _project(root)
    (root / ".env").write_text("x")
    dest = tmp_path / "worktree"
    dest.mkdir()
    project_file = paths.project_setup_file(root)
    project_file.parent.mkdir(parents=True, exist_ok=True)
    project_file.write_text('[setup]\ncopy = [".env"]\n')

    assert worktree_setup.run_setup(proj, dest).ok
    assert (dest / ".env").exists()


def test_an_unparseable_project_setup_file_raises(isolated_xdg: Path, tmp_path: Path) -> None:
    root = tmp_path / "repo"
    proj = _project(root)
    project_file = paths.project_setup_file(root)
    project_file.parent.mkdir(parents=True, exist_ok=True)
    project_file.write_text("copy = [unclosed\n")

    with pytest.raises(GoblinError, match="not valid TOML"):
        worktree_setup.run_setup(proj, tmp_path / "worktree")


def test_setup_config_round_trips_through_the_toml_aliases(isolated_xdg: Path) -> None:
    _write_config('[setup]\ncopy = [".env"]\nlink = ["node_modules"]\n')
    cfg = config.load()
    assert cfg.setup.copy_paths == [".env"]
    assert cfg.setup.link_paths == ["node_modules"]
    assert config.dump_toml_dict(cfg)["setup"]["copy"] == [".env"]


# --- journal ----------------------------------------------------------------


def test_every_step_lands_in_the_journal(isolated_xdg: Path, tmp_path: Path) -> None:
    proj = _project(tmp_path / "repo")
    dest = tmp_path / "worktree"
    dest.mkdir()
    _write_config('[setup]\nrun = ["exit 2"]\n')

    worktree_setup.run_setup(proj, dest, task_id="eng-9")

    records = [
        json.loads(line)
        for line in paths.setup_journal_file().read_text().splitlines()
        if line.strip()
    ]
    assert len(records) == 1
    assert records[0]["status"] == "failed"
    assert records[0]["project"] == "alpha"
    assert records[0]["kind"] == "run"
