"""Pre-accepting an agent's first-run workspace-trust prompt (ADR 0014)."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from goblin_watcher import config, workspace_trust
from goblin_watcher.agents import AGENT_NAMES, get_agent
from goblin_watcher.agents.claude import ClaudeAgent


@pytest.fixture
def claude_config(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A `.claude.json` in a `CLAUDE_CONFIG_DIR` of its own."""
    root = tmp_path / "claude-config"
    root.mkdir()
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(root))
    path = root / ".claude.json"
    path.write_text(json.dumps({"numStartups": 3, "projects": {}}))
    return path


def _projects(path: Path) -> dict:
    return json.loads(path.read_text())["projects"]


def test_every_agent_answers_the_capability(tmp_path: Path) -> None:
    for name in AGENT_NAMES:
        assert isinstance(get_agent(name).pretrust_workspace(tmp_path), bool), name


def test_claude_records_trust_for_the_cwd(claude_config: Path, tmp_path: Path) -> None:
    workdir = tmp_path / "worktree"
    workdir.mkdir()

    assert ClaudeAgent().pretrust_workspace(workdir) is True

    assert _projects(claude_config)[str(workdir.resolve())]["hasTrustDialogAccepted"] is True


def test_claude_preserves_the_rest_of_the_config(claude_config: Path, tmp_path: Path) -> None:
    # The whole document is rewritten, so everything else in it — auth state
    # included — has to survive the round trip untouched.
    claude_config.write_text(
        json.dumps(
            {
                "oauthAccount": {"accountUuid": "abc"},
                "projects": {"/somewhere/else": {"hasTrustDialogAccepted": True, "lastCost": 1.5}},
            }
        )
    )

    ClaudeAgent().pretrust_workspace(tmp_path)

    raw = json.loads(claude_config.read_text())
    assert raw["oauthAccount"] == {"accountUuid": "abc"}
    assert raw["projects"]["/somewhere/else"] == {"hasTrustDialogAccepted": True, "lastCost": 1.5}


def test_claude_keeps_an_existing_entrys_other_keys(claude_config: Path, tmp_path: Path) -> None:
    key = str(tmp_path.resolve())
    claude_config.write_text(json.dumps({"projects": {key: {"allowedTools": ["Bash"]}}}))

    assert ClaudeAgent().pretrust_workspace(tmp_path) is True

    entry = _projects(claude_config)[key]
    assert entry == {"allowedTools": ["Bash"], "hasTrustDialogAccepted": True}


def test_claude_skips_an_already_trusted_dir(claude_config: Path, tmp_path: Path) -> None:
    ClaudeAgent().pretrust_workspace(tmp_path)
    before = claude_config.stat().st_mtime_ns

    # The second launch in the same directory is a pure read: no write, and the
    # False says so, which is what keeps the claude-write race down to first
    # touch of a directory.
    assert ClaudeAgent().pretrust_workspace(tmp_path) is False
    assert claude_config.stat().st_mtime_ns == before


def test_claude_never_creates_the_config(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    root = tmp_path / "empty-config"
    root.mkdir()
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(root))

    # `.claude.json` is where claude's onboarding and auth live. No file means
    # claude has never run here, and inventing one is not gw's business.
    assert ClaudeAgent().pretrust_workspace(tmp_path) is False
    assert not (root / ".claude.json").exists()


def test_apply_is_gated_on_config(claude_config: Path, tmp_path: Path) -> None:
    cfg = config.Config()
    cfg.defaults.trust_workspaces = False

    assert workspace_trust.apply(ClaudeAgent(), tmp_path, cfg=cfg) is False
    assert _projects(claude_config) == {}


def test_apply_is_on_by_default(claude_config: Path, tmp_path: Path) -> None:
    assert workspace_trust.apply(ClaudeAgent(), tmp_path, cfg=config.Config()) is True


def test_apply_swallows_a_broken_config(
    claude_config: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    claude_config.write_text("{ not json")

    # A bookkeeping write that failed must not take the launch down with it.
    assert workspace_trust.apply(ClaudeAgent(), tmp_path, cfg=config.Config()) is False
    assert "trust prompt" in capsys.readouterr().out


def test_apply_swallows_an_agent_that_raises(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    class Exploding(ClaudeAgent):
        def pretrust_workspace(self, cwd: Path) -> bool:
            del cwd
            raise RuntimeError("boom")

    assert workspace_trust.apply(Exploding(), tmp_path, cfg=config.Config()) is False
    assert "boom" in capsys.readouterr().out
