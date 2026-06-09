"""Tests for `gw config` (show/get/set/unset/path)."""

from pathlib import Path

from typer.testing import CliRunner

from goblin_watcher import config, paths
from goblin_watcher.cli import app

runner = CliRunner()


def test_config_path_prints_file(isolated_xdg: Path) -> None:
    res = runner.invoke(app, ["config", "path"])
    assert res.exit_code == 0, res.output
    assert res.stdout.strip() == str(paths.config_file())


def test_config_show_renders_defaults_without_file(isolated_xdg: Path) -> None:
    res = runner.invoke(app, ["config", "show"])
    assert res.exit_code == 0, res.output
    assert "(not present; showing defaults)" in res.output
    assert "windowing" in res.output


def test_config_set_then_get_round_trips(isolated_xdg: Path) -> None:
    res = runner.invoke(app, ["config", "set", "defaults.agent", "codex"])
    assert res.exit_code == 0, res.output
    assert paths.config_file().exists()

    res = runner.invoke(app, ["config", "get", "defaults.agent"])
    assert res.exit_code == 0, res.output
    assert res.stdout.strip() == "codex"

    # The file only carries the key the user set; everything else stays default.
    assert config.load().defaults.windowing == "inline"


def test_config_set_parses_toml_literals(isolated_xdg: Path) -> None:
    res = runner.invoke(app, ["config", "set", "defaults.unsafe", "false"])
    assert res.exit_code == 0, res.output
    assert config.load().defaults.unsafe is False

    res = runner.invoke(app, ["config", "set", "defaults.summary_ttl_seconds", "60"])
    assert res.exit_code == 0, res.output
    assert config.load().defaults.summary_ttl_seconds == 60


def test_config_set_invalid_value_rejected(isolated_xdg: Path) -> None:
    res = runner.invoke(app, ["config", "set", "defaults.summary_ttl_seconds", "soon"])
    assert res.exit_code != 0
    assert res.exception is not None
    assert "Invalid config value" in str(res.exception)
    # Nothing was written.
    assert not paths.config_file().exists()


def test_config_set_unknown_key_rejected(isolated_xdg: Path) -> None:
    res = runner.invoke(app, ["config", "set", "defaults.does_not_exist", "1"])
    assert res.exit_code != 0
    assert res.exception is not None
    assert "Unknown config key" in str(res.exception)


def test_config_get_unknown_key_errors(isolated_xdg: Path) -> None:
    res = runner.invoke(app, ["config", "get", "defaults.nope"])
    assert res.exit_code != 0
    assert res.exception is not None
    assert "No config key" in str(res.exception)


def test_config_unset_removes_key_and_prunes_empty_tables(isolated_xdg: Path) -> None:
    runner.invoke(app, ["config", "set", "defaults.agent", "codex"])
    res = runner.invoke(app, ["config", "unset", "defaults.agent"])
    assert res.exit_code == 0, res.output
    assert config.load().defaults.agent is None
    # The now-empty [defaults] table is dropped from the file.
    assert "defaults" not in paths.config_file().read_text()


def test_config_unset_missing_key_errors(isolated_xdg: Path) -> None:
    res = runner.invoke(app, ["config", "unset", "defaults.agent"])
    assert res.exit_code != 0
    assert res.exception is not None
