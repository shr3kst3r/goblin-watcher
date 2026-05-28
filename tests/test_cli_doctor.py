from pathlib import Path

import pytest
from typer.testing import CliRunner

from goblin_watcher import paths
from goblin_watcher.cli import app


def _write_tmux_config() -> None:
    f = paths.config_file()
    f.parent.mkdir(parents=True, exist_ok=True)
    f.write_text('[defaults]\nwindowing = "tmux"\n')


def test_doctor_runs_and_reports(isolated_xdg: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("LINEAR_API_KEY", raising=False)
    runner = CliRunner()
    res = runner.invoke(app, ["doctor"])
    # Linear key isn't configured → at least one failed check → non-zero exit.
    assert res.exit_code != 0, res.output
    assert "git" in res.output
    assert "linear api key" in res.output


def test_doctor_green_when_key_present(isolated_xdg: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("LINEAR_API_KEY", "lin_api_test")
    runner = CliRunner()
    res = runner.invoke(app, ["doctor"])
    # `git` is on PATH in the test env; with the env key present, all required
    # checks should pass.
    assert "linear api key" in res.output
    assert "resolved" in res.output


def test_doctor_reports_managed_agent_scaffold(
    isolated_xdg: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("LINEAR_API_KEY", "lin_api_test")
    runner = CliRunner()
    res = runner.invoke(app, ["doctor"])
    assert "managed agent" in res.output
    assert "scaffold only" in res.output


def test_omz_check_na_for_inline_windowing(
    isolated_xdg: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("LINEAR_API_KEY", "lin_api_test")
    monkeypatch.setenv("ZSH", str(isolated_xdg / "home" / ".oh-my-zsh"))
    (isolated_xdg / "home" / ".zshrc").write_text("# no omz update mode set\n")
    runner = CliRunner()
    res = runner.invoke(app, ["doctor"])
    assert "omz update prompt" in res.output
    assert "n/a (inline windowing)" in res.output


def test_omz_check_warns_when_tmux_and_default_prompt(
    isolated_xdg: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("LINEAR_API_KEY", "lin_api_test")
    monkeypatch.setenv("ZSH", str(isolated_xdg / "home" / ".oh-my-zsh"))
    _write_tmux_config()
    (isolated_xdg / "home" / ".zshrc").write_text("# no omz update mode set\n")
    runner = CliRunner()
    res = runner.invoke(app, ["doctor"])
    # Rich wraps the detail cell across multiple rows. Collapse whitespace,
    # then assert on substrings short enough not to span a wrap boundary.
    flat = " ".join(res.output.split())
    assert "omz update prompt" in flat
    assert "can eat the" in flat
    assert "mode reminder" in flat


def test_omz_check_quiet_when_safe_zstyle_set(
    isolated_xdg: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("LINEAR_API_KEY", "lin_api_test")
    monkeypatch.setenv("ZSH", str(isolated_xdg / "home" / ".oh-my-zsh"))
    _write_tmux_config()
    (isolated_xdg / "home" / ".zshrc").write_text("zstyle ':omz:update' mode reminder\n")
    runner = CliRunner()
    res = runner.invoke(app, ["doctor"])
    flat = " ".join(res.output.split())
    assert "update prompt suppressed" in flat


def test_omz_check_quiet_when_legacy_disable_set(
    isolated_xdg: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("LINEAR_API_KEY", "lin_api_test")
    monkeypatch.setenv("ZSH", str(isolated_xdg / "home" / ".oh-my-zsh"))
    _write_tmux_config()
    (isolated_xdg / "home" / ".zshrc").write_text('DISABLE_AUTO_UPDATE="true"\n')
    runner = CliRunner()
    res = runner.invoke(app, ["doctor"])
    flat = " ".join(res.output.split())
    assert "update prompt suppressed" in flat


def test_omz_check_quiet_when_omz_not_detected(
    isolated_xdg: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("LINEAR_API_KEY", "lin_api_test")
    monkeypatch.delenv("ZSH", raising=False)
    _write_tmux_config()
    # No ~/.oh-my-zsh dir, no $ZSH — should report "not detected".
    runner = CliRunner()
    res = runner.invoke(app, ["doctor"])
    flat = " ".join(res.output.split())
    assert "oh-my-zsh not detected" in flat
