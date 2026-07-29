"""launchd plist generation and install/uninstall. Never calls real launchctl."""

from __future__ import annotations

import plistlib
import subprocess
from pathlib import Path
from unittest.mock import patch

import pytest

from goblin_watcher.errors import GoblinError
from goblin_watcher.sync import launchd


def _ok() -> subprocess.CompletedProcess[str]:
    return subprocess.CompletedProcess(args=[], returncode=0, stdout="", stderr="")


def _fail() -> subprocess.CompletedProcess[str]:
    return subprocess.CompletedProcess(args=[], returncode=1, stdout="", stderr="nope")


def test_is_supported_only_on_darwin() -> None:
    assert launchd.is_supported("darwin") is True
    assert launchd.is_supported("linux") is False


def test_build_plist_shape(isolated_xdg: Path) -> None:
    payload = launchd.build_plist(300, program="/opt/bin/gw")
    assert payload["Label"] == launchd.LABEL
    assert payload["ProgramArguments"] == ["/opt/bin/gw", "sync", "run"]
    assert payload["StartInterval"] == 300
    # A load-time firing would burst work on every login; the interval is enough.
    assert payload["RunAtLoad"] is False
    assert "sync.launchd.log" in str(payload["StandardOutPath"])


def test_build_plist_uses_absolute_binary(isolated_xdg: Path) -> None:
    """launchd has a minimal PATH, so a bare `gw` would never resolve."""
    with patch("goblin_watcher.sync.launchd.shutil.which", return_value="/usr/local/bin/gw"):
        payload = launchd.build_plist(600)
    assert payload["ProgramArguments"][0] == "/usr/local/bin/gw"


def test_resolve_gw_binary_raises_when_missing() -> None:
    with (
        patch("goblin_watcher.sync.launchd.shutil.which", return_value=None),
        pytest.raises(GoblinError),
    ):
        launchd.resolve_gw_binary()


def test_crontab_line_for_non_darwin() -> None:
    line = launchd.crontab_line(300, program="/opt/bin/gw")
    assert line == "*/5 * * * * /opt/bin/gw sync run"
    assert launchd.crontab_line(60, program="gw").startswith("* * * * *")


def test_install_writes_plist_and_bootstraps(isolated_xdg: Path, tmp_path: Path) -> None:
    calls: list[list[str]] = []

    def fake_run(argv, **kwargs):  # type: ignore[no-untyped-def]
        calls.append(list(argv))
        return _ok()

    with (
        patch("goblin_watcher.sync.launchd.shutil.which", return_value="/usr/bin/launchctl"),
        patch("goblin_watcher.sync.launchd.subprocess.run", side_effect=fake_run),
        patch.object(Path, "home", return_value=tmp_path),
    ):
        path = launchd.install(300, program="/opt/bin/gw")

    assert path.exists()
    with path.open("rb") as f:
        payload = plistlib.load(f)
    assert payload["ProgramArguments"] == ["/opt/bin/gw", "sync", "run"]

    verbs = [c[1] for c in calls]
    # Reinstall must be idempotent: drop any prior registration, then bootstrap.
    assert verbs[0] == "bootout"
    assert "bootstrap" in verbs


def test_install_falls_back_to_legacy_load(isolated_xdg: Path, tmp_path: Path) -> None:
    def fake_run(argv, **kwargs):  # type: ignore[no-untyped-def]
        return _ok() if argv[1] == "load" else _fail()

    with (
        patch("goblin_watcher.sync.launchd.shutil.which", return_value="/usr/bin/launchctl"),
        patch("goblin_watcher.sync.launchd.subprocess.run", side_effect=fake_run),
        patch.object(Path, "home", return_value=tmp_path),
    ):
        path = launchd.install(300, program="/opt/bin/gw")
    assert path.exists()


def test_install_raises_when_both_load_paths_fail(isolated_xdg: Path, tmp_path: Path) -> None:
    with (
        patch("goblin_watcher.sync.launchd.shutil.which", return_value="/usr/bin/launchctl"),
        patch("goblin_watcher.sync.launchd.subprocess.run", return_value=_fail()),
        patch.object(Path, "home", return_value=tmp_path),
        pytest.raises(GoblinError),
    ):
        launchd.install(300, program="/opt/bin/gw")


def test_uninstall_removes_plist(isolated_xdg: Path, tmp_path: Path) -> None:
    with (
        patch("goblin_watcher.sync.launchd.shutil.which", return_value="/usr/bin/launchctl"),
        patch("goblin_watcher.sync.launchd.subprocess.run", return_value=_ok()),
        patch.object(Path, "home", return_value=tmp_path),
    ):
        launchd.install(300, program="/opt/bin/gw")
        assert launchd.plist_path().exists()
        removed = launchd.uninstall()
        assert removed is True
        assert not launchd.plist_path().exists()
        # Second uninstall is a no-op, not an error.
        assert launchd.uninstall() is False


def test_launchctl_missing_raises() -> None:
    with (
        patch("goblin_watcher.sync.launchd.shutil.which", return_value=None),
        pytest.raises(GoblinError),
    ):
        launchd.is_loaded()


def test_build_plist_bakes_in_a_usable_path(isolated_xdg: Path, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    """launchd's default PATH has no Homebrew, so `gh` and `op` would vanish.

    Without this, every scheduled pass silently loses PR state and CI checks
    while looking perfectly healthy.
    """
    monkeypatch.setenv("PATH", "/opt/homebrew/bin:/usr/bin")
    payload = launchd.build_plist(300, program="/opt/bin/gw")
    env = payload["EnvironmentVariables"]
    assert isinstance(env, dict)
    path = env["PATH"].split(":")
    assert "/opt/homebrew/bin" in path
    # The system defaults are still appended, and nothing is duplicated.
    assert "/sbin" in path
    assert len(path) == len(set(path))


def test_installed_interval_reads_the_plist_not_the_config(
    isolated_xdg: Path, tmp_path: Path
) -> None:
    """`--interval N` writes the plist without touching config; status must not lie."""
    with (
        patch("goblin_watcher.sync.launchd.shutil.which", return_value="/usr/bin/launchctl"),
        patch("goblin_watcher.sync.launchd.subprocess.run", return_value=_ok()),
        patch.object(Path, "home", return_value=tmp_path),
    ):
        launchd.install(900, program="/opt/bin/gw")
        assert launchd.installed_interval() == 900


def test_installed_interval_is_none_without_a_plist(isolated_xdg: Path, tmp_path: Path) -> None:
    with patch.object(Path, "home", return_value=tmp_path):
        assert launchd.installed_interval() is None


def test_installed_interval_tolerates_a_corrupt_plist(isolated_xdg: Path, tmp_path: Path) -> None:
    with patch.object(Path, "home", return_value=tmp_path):
        target = launchd.plist_path()
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text("not a plist")
        assert launchd.installed_interval() is None
