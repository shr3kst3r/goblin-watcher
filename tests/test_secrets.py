from pathlib import Path
from unittest.mock import patch

import pytest

from goblin_watcher import config, paths, secrets
from goblin_watcher.errors import LinearAuthError, MissingDependencyError


def test_env_key_takes_precedence(isolated_xdg: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("LINEAR_API_KEY", "lin_api_from_env")
    assert secrets.get_linear_api_key() == "lin_api_from_env"


def test_config_literal_used_when_env_absent(
    isolated_xdg: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("LINEAR_API_KEY", raising=False)
    cfg = config.Config()
    cfg.linear.api_key = "lin_api_from_config"
    config.save(cfg)
    assert secrets.get_linear_api_key() == "lin_api_from_config"


def test_missing_key_raises(isolated_xdg: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("LINEAR_API_KEY", raising=False)
    with pytest.raises(LinearAuthError):
        secrets.get_linear_api_key()


def test_op_reference_calls_op_read(isolated_xdg: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("LINEAR_API_KEY", raising=False)
    cfg = config.Config()
    cfg.linear.api_key = "op://Personal/Linear/api_key"
    config.save(cfg)

    fake_run = type("FakeRes", (), {"returncode": 0, "stdout": "resolved_key\n", "stderr": ""})()
    with (
        patch("goblin_watcher.secrets.shutil.which", return_value="/usr/bin/op"),
        patch("goblin_watcher.secrets.subprocess.run", return_value=fake_run),
    ):
        assert secrets.get_linear_api_key() == "resolved_key"


def test_op_reference_without_op_cli_errors(
    isolated_xdg: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("LINEAR_API_KEY", raising=False)
    cfg = config.Config()
    cfg.linear.api_key = "op://Personal/Linear/api_key"
    config.save(cfg)
    with (
        patch("goblin_watcher.secrets.shutil.which", return_value=None),
        pytest.raises(MissingDependencyError),
    ):
        secrets.get_linear_api_key()


def test_config_round_trip(isolated_xdg: Path) -> None:
    cfg = config.Config()
    cfg.linear.api_key = "x"
    cfg.defaults.agent = "claude"
    cfg.defaults.windowing = "tmux"
    config.save(cfg)

    f = paths.config_file()
    assert f.exists()

    loaded = config.load()
    assert loaded.linear.api_key == "x"
    assert loaded.defaults.agent == "claude"
    assert loaded.defaults.windowing == "tmux"
