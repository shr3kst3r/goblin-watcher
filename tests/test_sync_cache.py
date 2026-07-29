"""`gw status` reading the background-sync indicator cache (ADR 0005 step 6).

The cache must be preferred while fresh, shown with its age, ignored when stale
or absent, and bypassable with `--no-cache` — so an uninstalled sync leaves
today's behaviour exactly as it was.
"""

from __future__ import annotations

import subprocess
from datetime import UTC, datetime, timedelta
from pathlib import Path

from typer.testing import CliRunner

from goblin_watcher import config, state
from goblin_watcher.cli import app
from goblin_watcher.sync import store
from goblin_watcher.sync.models import IndicatorCache, TaskIndicators

runner = CliRunner()


def _init_repo(path: Path) -> None:
    path.mkdir(parents=True, exist_ok=True)
    subprocess.run(["git", "init", "-q", "-b", "main", str(path)], check=True)
    subprocess.run(["git", "-C", str(path), "config", "user.email", "t@t"], check=True)
    subprocess.run(["git", "-C", str(path), "config", "user.name", "tester"], check=True)
    (path / "README.md").write_text("hi")
    subprocess.run(["git", "-C", str(path), "add", "."], check=True)
    subprocess.run(["git", "-C", str(path), "commit", "-qm", "init"], check=True)


def _bootstrap(tmp_path: Path) -> str:
    """Register a project with one task and return the task id."""
    repo = tmp_path / "alpha"
    _init_repo(repo)
    runner.invoke(app, ["project", "new", "alpha", "--dir", str(repo)])
    runner.invoke(app, ["new", "--branch-name", "cache-me", "--no-launch"])
    [task] = state.list_tasks(state.get_project("alpha"))
    return task.id


def _cache(task_id: str, **kwargs: object) -> None:
    cache = IndicatorCache()
    cache.entries[store.cache_key("alpha", task_id)] = TaskIndicators(**kwargs)  # type: ignore[arg-type]
    store.save_cache(cache)


def test_status_renders_fresh_cached_indicators_with_an_age(
    isolated_xdg: Path, tmp_path: Path
) -> None:
    task_id = _bootstrap(tmp_path)
    _cache(
        task_id,
        ahead=2,
        ahead_vs_remote=True,
        checks="failing",
        computed_at=datetime.now(UTC) - timedelta(minutes=3),
    )

    res = runner.invoke(app, ["status"])
    assert res.exit_code == 0, res.output
    assert "↑2 unpushed" in res.output
    assert "✗ checks" in res.output
    # The age is what stops a cached reading being mistaken for a live one.
    assert "(3m)" in res.output


def test_status_ignores_a_stale_cache_and_recomputes(isolated_xdg: Path, tmp_path: Path) -> None:
    """Past twice the interval the scheduler is presumed dead; live wins."""
    task_id = _bootstrap(tmp_path)
    interval = config.Config().sync.interval_seconds
    _cache(
        task_id,
        ahead=99,
        ahead_vs_remote=True,
        computed_at=datetime.now(UTC) - timedelta(seconds=interval * 2 + 60),
    )

    res = runner.invoke(app, ["status"])
    assert res.exit_code == 0, res.output
    assert "↑99" not in res.output


def test_status_no_cache_flag_forces_live_computation(isolated_xdg: Path, tmp_path: Path) -> None:
    task_id = _bootstrap(tmp_path)
    _cache(task_id, ahead=42, ahead_vs_remote=True)

    res = runner.invoke(app, ["status", "--no-cache"])
    assert res.exit_code == 0, res.output
    assert "↑42" not in res.output


def test_status_without_any_cache_behaves_as_before(isolated_xdg: Path, tmp_path: Path) -> None:
    """Sync never installed → no cache file at all → unchanged rendering."""
    _bootstrap(tmp_path)
    res = runner.invoke(app, ["status"])
    assert res.exit_code == 0, res.output
    assert "alpha" in res.output


def test_cache_is_keyed_by_project_so_ids_do_not_collide(
    isolated_xdg: Path, tmp_path: Path
) -> None:
    """Two projects can hold the same task id; one must not read the other's row."""
    task_id = _bootstrap(tmp_path)
    cache = IndicatorCache()
    cache.entries[store.cache_key("beta", task_id)] = TaskIndicators(ahead=7, ahead_vs_remote=True)
    store.save_cache(cache)

    assert store.get_indicators("alpha", task_id, max_age_seconds=3600) is None
    res = runner.invoke(app, ["status"])
    assert res.exit_code == 0, res.output
    assert "↑7" not in res.output
