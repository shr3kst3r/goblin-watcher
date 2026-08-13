"""Sync must not report a job as healthy when the job is dying at import (gh-51).

The scheduled pass runs whatever `gw` resolves to. When that gw is an editable
install, it runs whatever is in the checkout at that moment — and a pass that
dies before it can journal leaves `last_pass` frozen at the last *good* run while
launchd keeps firing. `gw sync status` used to read that as fine.
"""

from __future__ import annotations

import json
import subprocess
from datetime import UTC, datetime, timedelta
from pathlib import Path
from unittest.mock import patch

from typer.testing import CliRunner

from goblin_watcher.cli import app
from goblin_watcher.sync import launchd, store
from goblin_watcher.sync.models import PassReport, SyncState

runner = CliRunner()


def _editable_venv(tmp_path: Path, *, source: Path, editable: bool = True) -> Path:
    """A venv laid out like uv's, with a PEP 610 record. Returns the `gw` path."""
    site = tmp_path / "venv" / "lib" / "python3.14" / "site-packages"
    dist = site / "goblin_watcher-0.1.0.dist-info"
    dist.mkdir(parents=True)
    (dist / "direct_url.json").write_text(
        json.dumps({"url": source.as_uri(), "dir_info": {"editable": editable}})
    )
    binary = tmp_path / "venv" / "bin" / "gw"
    binary.parent.mkdir(parents=True, exist_ok=True)
    binary.write_text("#!/bin/sh\n")
    return binary


def _git_checkout(tmp_path: Path) -> Path:
    root = tmp_path / "checkout"
    root.mkdir(parents=True)
    subprocess.run(["git", "-C", str(root), "init", "-q", "-b", "main"], check=True)
    return root


# --- editable_checkout ------------------------------------------------------


def test_editable_checkout_finds_the_working_tree(tmp_path: Path) -> None:
    source = _git_checkout(tmp_path)
    binary = _editable_venv(tmp_path, source=source)
    assert launchd.editable_checkout(str(binary)) == source


def test_editable_checkout_is_none_for_a_normal_install(tmp_path: Path) -> None:
    source = _git_checkout(tmp_path)
    binary = _editable_venv(tmp_path, source=source, editable=False)
    assert launchd.editable_checkout(str(binary)) is None


def test_editable_checkout_is_none_when_the_source_is_not_a_repo(tmp_path: Path) -> None:
    source = tmp_path / "plain"
    source.mkdir()
    binary = _editable_venv(tmp_path, source=source)
    assert launchd.editable_checkout(str(binary)) is None


def test_editable_checkout_tolerates_a_layout_it_does_not_recognise(tmp_path: Path) -> None:
    assert launchd.editable_checkout(str(tmp_path / "nowhere" / "bin" / "gw")) is None


def test_editable_checkout_tolerates_an_unreadable_record(tmp_path: Path) -> None:
    source = _git_checkout(tmp_path)
    binary = _editable_venv(tmp_path, source=source)
    site = tmp_path / "venv" / "lib" / "python3.14" / "site-packages"
    (site / "goblin_watcher-0.1.0.dist-info" / "direct_url.json").write_text("{ not json")
    assert launchd.editable_checkout(str(binary)) is None


# --- gw sync status ---------------------------------------------------------


def _status_with_last_pass(tmp_path: Path, *, age_seconds: float, interval: int = 300) -> str:
    store.save_state(
        SyncState(
            last_pass=PassReport(
                pass_id="p1",
                status="ok",
                tasks=4,
                finished_at=datetime.now(UTC) - timedelta(seconds=age_seconds),
            )
        )
    )
    plist = tmp_path / "job.plist"
    plist.write_text("")
    with (
        patch("goblin_watcher.sync.launchd.is_supported", return_value=True),
        patch("goblin_watcher.sync.launchd.plist_path", return_value=plist),
        patch("goblin_watcher.sync.launchd.is_loaded", return_value=True),
        patch("goblin_watcher.sync.launchd.installed_interval", return_value=interval),
        patch("goblin_watcher.sync.launchd.editable_checkout", return_value=None),
    ):
        # Wide, so Rich's table doesn't ellipsize the detail being asserted on.
        return runner.invoke(app, ["sync", "status"], env={"COLUMNS": "400"}).output


def test_sync_status_flags_a_job_that_has_stopped_completing_passes(
    isolated_xdg: Path, tmp_path: Path
) -> None:
    # Four intervals with nothing recorded: launchd is firing, gw is not
    # finishing. That is what a broken checkout looks like from out here.
    out = _status_with_last_pass(tmp_path, age_seconds=1200, interval=300)
    assert "launchd is firing but gw is not finishing" in out
    assert "sync.launchd.log" in out


def test_sync_status_tolerates_a_pass_that_merely_overran_its_interval(
    isolated_xdg: Path, tmp_path: Path
) -> None:
    # Slower than one interval but inside the shared slack: normal on a slow
    # network, and must not read as a failure.
    out = _status_with_last_pass(tmp_path, age_seconds=400, interval=300)
    assert "launchd is firing but gw is not finishing" not in out


def test_sync_status_names_an_editable_install(isolated_xdg: Path, tmp_path: Path) -> None:
    source = _git_checkout(tmp_path)
    binary = _editable_venv(tmp_path, source=source)
    with (
        patch("goblin_watcher.sync.launchd.is_supported", return_value=True),
        patch("goblin_watcher.sync.launchd.plist_path", return_value=Path("/nonexistent.plist")),
        patch("goblin_watcher.sync.launchd.shutil.which", return_value=str(binary)),
    ):
        res = runner.invoke(app, ["sync", "status"], env={"COLUMNS": "400"})
    assert "editable install" in res.output
    assert str(source) in res.output
    # A warning, not a failure: developing gw on the machine that runs it is a
    # supported setup.
    assert res.exit_code == 0, res.output
