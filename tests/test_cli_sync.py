"""`gw sync` CLI surface: the pass, watch, status, install/uninstall."""

from __future__ import annotations

import subprocess
from datetime import UTC, datetime
from pathlib import Path
from unittest.mock import patch

from typer.testing import CliRunner

from goblin_watcher import paths, state
from goblin_watcher.cli import app
from goblin_watcher.models import Project
from goblin_watcher.sync import journal, store
from goblin_watcher.sync.models import PassReport, SyncState

runner = CliRunner()


def _register(tmp_path: Path) -> Project:
    root = tmp_path / "repo"
    root.mkdir(parents=True, exist_ok=True)
    subprocess.run(["git", "-C", str(root), "init", "-q", "-b", "main"], check=True)
    project = Project(
        name="demo",
        root=root,
        repo_url=None,
        default_branch="main",
        created_at=datetime.now(UTC),
    )
    state.register_project(project)
    return project


def test_sync_is_in_the_help_surface(isolated_xdg: Path) -> None:
    res = runner.invoke(app, ["--help"])
    assert res.exit_code == 0
    assert "sync" in res.output


def test_bare_sync_runs_a_pass_and_narrates(isolated_xdg: Path, tmp_path: Path) -> None:
    _register(tmp_path)
    res = runner.invoke(app, ["sync"])
    assert res.exit_code == 0, res.exception
    assert "Sync ok" in res.output
    # The journal records the pass even for a foreground run.
    events = [e["event"] for e in journal.read_entries()]
    assert "pass-start" in events
    assert "pass-end" in events


def test_sync_run_is_quiet(isolated_xdg: Path, tmp_path: Path) -> None:
    _register(tmp_path)
    res = runner.invoke(app, ["sync", "run"])
    assert res.exit_code == 0, res.exception
    assert "Sync ok" not in res.output


def test_sync_run_reports_when_another_pass_holds_the_lock(
    isolated_xdg: Path, tmp_path: Path
) -> None:
    from goblin_watcher import locks, paths

    _register(tmp_path)
    with locks.exclusive(paths.sync_lock_file()):
        res = runner.invoke(app, ["sync", "run"])
    assert res.exit_code == 0
    assert "already running" in res.output


def test_sync_status_before_first_run(isolated_xdg: Path) -> None:
    with (
        patch("goblin_watcher.sync.launchd.is_supported", return_value=True),
        patch("goblin_watcher.sync.launchd.plist_path", return_value=Path("/nonexistent.plist")),
    ):
        res = runner.invoke(app, ["sync", "status"])
    assert res.exit_code == 0
    assert "not installed" in res.output
    assert "never" in res.output


def test_sync_status_reports_last_pass(isolated_xdg: Path) -> None:
    st = SyncState(
        last_pass=PassReport(pass_id="p1", status="ok", tasks=4, finished_at=datetime.now(UTC))
    )
    store.save_state(st)
    with (
        patch("goblin_watcher.sync.launchd.is_supported", return_value=True),
        patch("goblin_watcher.sync.launchd.plist_path", return_value=Path("/nonexistent.plist")),
    ):
        res = runner.invoke(app, ["sync", "status"])
    assert res.exit_code == 0
    assert "4 task(s)" in res.output


def test_sync_status_flags_misconfigured_command_transport(isolated_xdg: Path) -> None:
    from goblin_watcher import config

    cfg = config.Config()
    cfg.sync.notify = "command"
    cfg.sync.notify_command = []
    with (
        patch("goblin_watcher.commands.sync.config.load", return_value=cfg),
        patch("goblin_watcher.sync.launchd.is_supported", return_value=True),
        patch("goblin_watcher.sync.launchd.plist_path", return_value=Path("/nope.plist")),
    ):
        res = runner.invoke(app, ["sync", "status"])
    assert res.exit_code == 1
    assert "notify_command is empty" in res.output


def test_sync_status_reports_which_mode_sync_is_in(isolated_xdg: Path) -> None:
    """`[sync.on]` is opt-in, so the table has to say plainly whether this
    install reports or acts (ADR 0012)."""
    from goblin_watcher import config

    def _status(cfg: config.Config) -> str:
        with (
            patch("goblin_watcher.commands.sync.config.load", return_value=cfg),
            patch("goblin_watcher.sync.launchd.is_supported", return_value=True),
            patch("goblin_watcher.sync.launchd.plist_path", return_value=Path("/nope.plist")),
        ):
            res = runner.invoke(app, ["sync", "status"])
        assert res.exit_code == 0
        return res.output

    assert "doesn't act" in _status(config.Config())

    wired = config.Config.model_validate({"sync": {"on": {"checks-failed": ["spawn-fix-session"]}}})
    output = _status(wired)
    assert "checks-failed" in output
    assert "spawn-fix-session" in output


def test_sync_install_on_non_darwin_prints_cron_and_writes_nothing(
    isolated_xdg: Path,
) -> None:
    with patch("goblin_watcher.sync.launchd.is_supported", return_value=False):
        res = runner.invoke(app, ["sync", "install"])
    assert res.exit_code == 0
    assert "sync run" in res.output
    assert "crontab" in res.output.lower() or "cron" in res.output.lower()


def test_sync_install_rejects_a_sub_minute_interval(isolated_xdg: Path) -> None:
    res = runner.invoke(app, ["sync", "install", "--interval", "5"])
    assert res.exit_code != 0


def test_sync_install_and_uninstall_drive_launchctl(isolated_xdg: Path, tmp_path: Path) -> None:
    ok = subprocess.CompletedProcess(args=[], returncode=0, stdout="", stderr="")
    with (
        patch("goblin_watcher.sync.launchd.is_supported", return_value=True),
        patch("goblin_watcher.sync.launchd.shutil.which", return_value="/usr/bin/launchctl"),
        patch("goblin_watcher.sync.launchd.subprocess.run", return_value=ok),
        patch.object(Path, "home", return_value=tmp_path),
    ):
        res = runner.invoke(app, ["sync", "install", "--interval", "600"])
        assert res.exit_code == 0, res.exception
        assert "every 600s" in res.output

        res = runner.invoke(app, ["sync", "uninstall"])
        assert res.exit_code == 0
        assert "Removed" in res.output


def test_sync_watch_replays_existing_journal_lines(isolated_xdg: Path) -> None:
    journal.append(pass_id="p1", level="notify", event="pr-merged", project="demo", task="demo-1")

    # follow() blocks forever by design; stop after the replayed lines.
    with patch("goblin_watcher.sync.journal.follow", side_effect=KeyboardInterrupt):
        res = runner.invoke(app, ["sync", "watch"])
    assert res.exit_code == 0
    assert "pr-merged" in res.output
    assert "demo/demo-1" in res.output


def test_sync_watch_does_not_eat_bracketed_detail_as_markup(isolated_xdg: Path) -> None:
    """Journal details carry raw git/gh text; `[...]` must not vanish as markup."""
    journal.append(
        pass_id="p1",
        level="error",
        event="fetch-failed",
        project="demo",
        detail="fatal: could not read from remote [origin]",
    )
    with patch("goblin_watcher.sync.journal.follow", side_effect=KeyboardInterrupt):
        res = runner.invoke(app, ["sync", "watch"])
    assert res.exit_code == 0
    assert "[origin]" in res.output


def test_sync_status_reports_the_scheduled_interval_not_the_config_key(
    isolated_xdg: Path, tmp_path: Path
) -> None:
    """`gw sync install --interval N` doesn't write config; status reads the plist."""
    ok = subprocess.CompletedProcess(args=[], returncode=0, stdout="", stderr="")
    with (
        patch("goblin_watcher.sync.launchd.is_supported", return_value=True),
        patch("goblin_watcher.sync.launchd.shutil.which", return_value="/usr/bin/launchctl"),
        patch("goblin_watcher.sync.launchd.subprocess.run", return_value=ok),
        patch.object(Path, "home", return_value=tmp_path),
    ):
        assert runner.invoke(app, ["sync", "install", "--interval", "900"]).exit_code == 0
        res = runner.invoke(app, ["sync", "status"])

    assert "every 900s" in res.output
    assert "every 300s" not in res.output


def test_sync_prune_journal_trims_old_records(isolated_xdg: Path) -> None:
    import json
    from datetime import timedelta

    path = paths.sync_journal_file()
    path.parent.mkdir(parents=True, exist_ok=True)
    old = (datetime.now(UTC) - timedelta(days=40)).isoformat().replace("+00:00", "Z")
    path.write_text(json.dumps({"ts": old, "level": "info", "event": "ancient"}) + "\n")
    journal.append(pass_id="p", level="info", event="fresh")

    dry = runner.invoke(app, ["sync", "prune-journal", "--days", "30", "--dry-run"])
    assert dry.exit_code == 0
    assert "Would remove 1" in dry.output
    assert len(journal.read_entries()) == 2, "--dry-run must not write"

    res = runner.invoke(app, ["sync", "prune-journal", "--days", "30"])
    assert res.exit_code == 0
    assert [e["event"] for e in journal.read_entries()] == ["fresh"]


def test_sync_prune_journal_rejects_negative_days(isolated_xdg: Path) -> None:
    journal.append(pass_id="p", level="info", event="fresh")
    res = runner.invoke(app, ["sync", "prune-journal", "--days", "-1"])
    assert res.exit_code != 0
