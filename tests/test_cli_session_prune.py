import subprocess
from datetime import UTC, datetime, timedelta
from pathlib import Path

from typer.testing import CliRunner

from goblin_watcher import sessions, state
from goblin_watcher.cli import app
from goblin_watcher.models import SessionRecord


def _init_repo(path: Path) -> None:
    path.mkdir(parents=True, exist_ok=True)
    subprocess.run(["git", "init", "-q", "-b", "main", str(path)], check=True)
    subprocess.run(["git", "-C", str(path), "config", "user.email", "t@t"], check=True)
    subprocess.run(["git", "-C", str(path), "config", "user.name", "tester"], check=True)
    (path / "README.md").write_text("hi")
    subprocess.run(["git", "-C", str(path), "add", "."], check=True)
    subprocess.run(["git", "-C", str(path), "commit", "-qm", "init"], check=True)


def _bootstrap_task_with_sessions(tmp_path: Path) -> None:
    repo = tmp_path / "alpha"
    _init_repo(repo)
    runner = CliRunner()
    runner.invoke(app, ["project", "new", "alpha", "--dir", str(repo)])
    runner.invoke(app, ["new", "--branch-name", "spike/foo", "--no-launch"])

    proj = state.get_project("alpha")
    [task] = state.list_tasks(proj)
    now = datetime.now(UTC)
    fresh = SessionRecord(
        agent="claude",
        session_id="recent-id",
        created_at=now,
        last_used_at=now - timedelta(days=2),
    )
    stale = SessionRecord(
        agent="claude",
        session_id="stale-id",
        created_at=now - timedelta(days=90),
        last_used_at=now - timedelta(days=60),
    )
    task = sessions.upsert(task, fresh)
    task = sessions.upsert(task, stale)
    state.save_task(proj, task)


def test_session_prune_drops_stale_only(isolated_xdg: Path, tmp_path: Path) -> None:
    _bootstrap_task_with_sessions(tmp_path)
    runner = CliRunner()
    res = runner.invoke(app, ["session", "prune", "--older-than", "30", "--force"])
    assert res.exit_code == 0, res.output

    proj = state.get_project("alpha")
    [task] = state.list_tasks(proj)
    ids = {s.session_id for s in task.sessions}
    assert ids == {"recent-id"}


def test_session_prune_dry_run_changes_nothing(isolated_xdg: Path, tmp_path: Path) -> None:
    _bootstrap_task_with_sessions(tmp_path)
    runner = CliRunner()
    res = runner.invoke(app, ["session", "prune", "--older-than", "30", "--dry-run"])
    assert res.exit_code == 0, res.output
    assert "stale-id" in res.output

    proj = state.get_project("alpha")
    [task] = state.list_tasks(proj)
    assert {s.session_id for s in task.sessions} == {"recent-id", "stale-id"}


def test_session_prune_nothing_to_do(isolated_xdg: Path, tmp_path: Path) -> None:
    _bootstrap_task_with_sessions(tmp_path)
    runner = CliRunner()
    # 365d threshold is older than even the "stale" fixture.
    res = runner.invoke(app, ["session", "prune", "--older-than", "365", "--force"])
    assert res.exit_code == 0
    assert "No sessions older than" in res.output


def _dirty_worktree(proj_name: str) -> Path:
    """Drop an untracked file in the alpha task's worktree, returning its path."""
    proj = state.get_project(proj_name)
    [task] = state.list_tasks(proj)
    scratch = task.worktree_path / "scratch.txt"
    scratch.write_text("uncommitted work")
    return scratch


def test_session_prune_skips_dirty_worktree(isolated_xdg: Path, tmp_path: Path) -> None:
    _bootstrap_task_with_sessions(tmp_path)
    _dirty_worktree("alpha")
    runner = CliRunner()
    res = runner.invoke(app, ["session", "prune", "--older-than", "30", "--force"])
    assert res.exit_code == 0, res.output
    assert "uncommitted changes" in res.output

    proj = state.get_project("alpha")
    [task] = state.list_tasks(proj)
    # Stale session preserved because the worktree has uncommitted changes.
    assert {s.session_id for s in task.sessions} == {"recent-id", "stale-id"}


def test_session_prune_include_dirty_overrides(isolated_xdg: Path, tmp_path: Path) -> None:
    _bootstrap_task_with_sessions(tmp_path)
    _dirty_worktree("alpha")
    runner = CliRunner()
    res = runner.invoke(
        app,
        ["session", "prune", "--older-than", "30", "--include-dirty", "--force"],
    )
    assert res.exit_code == 0, res.output

    proj = state.get_project("alpha")
    [task] = state.list_tasks(proj)
    assert {s.session_id for s in task.sessions} == {"recent-id"}


def test_session_prune_missing_worktree_is_clean(isolated_xdg: Path, tmp_path: Path) -> None:
    _bootstrap_task_with_sessions(tmp_path)
    proj = state.get_project("alpha")
    [task] = state.list_tasks(proj)
    # Wipe the worktree directory without going through `gw task rm`.
    import shutil

    shutil.rmtree(task.worktree_path)

    runner = CliRunner()
    res = runner.invoke(app, ["session", "prune", "--older-than", "30", "--force"])
    assert res.exit_code == 0, res.output

    [task] = state.list_tasks(proj)
    assert {s.session_id for s in task.sessions} == {"recent-id"}


def test_session_prune_project_filter(isolated_xdg: Path, tmp_path: Path) -> None:
    _bootstrap_task_with_sessions(tmp_path)
    # A second project with its own stale session; should be untouched when
    # --project filters to alpha.
    beta_repo = tmp_path / "beta"
    _init_repo(beta_repo)
    runner = CliRunner()
    runner.invoke(app, ["project", "new", "beta", "--dir", str(beta_repo)])
    runner.invoke(
        app,
        ["new", "--branch-name", "spike/bar", "--project", "beta", "--no-launch"],
    )
    beta = state.get_project("beta")
    [beta_task] = state.list_tasks(beta)
    now = datetime.now(UTC)
    beta_stale = SessionRecord(
        agent="claude",
        session_id="beta-stale",
        created_at=now - timedelta(days=90),
        last_used_at=now - timedelta(days=60),
    )
    state.save_task(beta, sessions.upsert(beta_task, beta_stale))

    res = runner.invoke(
        app,
        ["session", "prune", "--older-than", "30", "--project", "alpha", "--force"],
    )
    assert res.exit_code == 0, res.output

    alpha = state.get_project("alpha")
    [alpha_task] = state.list_tasks(alpha)
    assert {s.session_id for s in alpha_task.sessions} == {"recent-id"}
    # beta is untouched by the alpha-scoped prune.
    [beta_after] = state.list_tasks(beta)
    assert "beta-stale" in {s.session_id for s in beta_after.sessions}
