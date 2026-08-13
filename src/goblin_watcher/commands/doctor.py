from __future__ import annotations

import os
import re
import shutil
from dataclasses import dataclass
from pathlib import Path

import typer
from rich.table import Table

from goblin_watcher import config, secrets
from goblin_watcher.console import console
from goblin_watcher.errors import GoblinError
from goblin_watcher.windowing import WINDOWING_MODES


@dataclass
class Check:
    name: str
    ok: bool
    detail: str


def _binary_check(name: str, required: bool) -> Check:
    found = shutil.which(name)
    if found:
        return Check(name=name, ok=True, detail=found)
    return Check(
        name=name,
        ok=not required,
        detail="(not on PATH)" if required else "(not on PATH — optional)",
    )


def _linear_key_check() -> Check:
    try:
        # We deliberately don't print the key — only confirm it resolved.
        secrets.get_linear_api_key()
        return Check(name="linear api key", ok=True, detail="resolved")
    except GoblinError as e:
        return Check(name="linear api key", ok=False, detail=e.message)


def _windowing_check(cfg: config.Config) -> Check:
    mode = cfg.defaults.windowing
    if mode not in WINDOWING_MODES:
        # Config isn't validated on load, so a typo here would otherwise only
        # surface as a failed spawn.
        return Check(
            name="windowing",
            ok=False,
            detail=f"unknown mode {mode!r} (use: {', '.join(WINDOWING_MODES)})",
        )
    if mode == "tmux":
        return _binary_check("tmux", required=True)
    return Check(name="windowing", ok=True, detail=f"mode={mode}")


_OMZ_UPDATE_MODE_RE = re.compile(
    r"""^\s*zstyle\s+['"]:omz:update['"]\s+mode\s+(\w+)""",
    re.MULTILINE,
)
_OMZ_LEGACY_DISABLE_RE = re.compile(
    r"""^\s*(?:export\s+)?DISABLE_(?:AUTO_UPDATE|UPDATE_PROMPT)\s*=\s*['"]?true""",
    re.MULTILINE,
)
_OMZ_SAFE_MODES = {"auto", "reminder", "disabled"}


def _omz_update_prompt_check(cfg: config.Config) -> Check:
    # Oh-my-zsh's interactive update prompt reads a single char at shell init.
    # In tmux mode we `send-keys` the agent command into a fresh pane, and that
    # first byte (e.g. the 'c' of `claude`) can be eaten by the prompt. Inline
    # and headless modes bypass the interactive shell entirely.
    name = "omz update prompt"
    mode = cfg.defaults.windowing
    if mode != "tmux":
        return Check(name=name, ok=True, detail=f"n/a ({mode} windowing)")

    using_omz = bool(os.environ.get("ZSH")) or (Path.home() / ".oh-my-zsh").is_dir()
    if not using_omz:
        return Check(name=name, ok=True, detail="oh-my-zsh not detected")

    zshrc = Path.home() / ".zshrc"
    if not zshrc.is_file():
        return Check(name=name, ok=True, detail="no ~/.zshrc to inspect")

    text = zshrc.read_text(errors="replace")
    modes = _OMZ_UPDATE_MODE_RE.findall(text)
    has_safe_zstyle = any(m in _OMZ_SAFE_MODES for m in modes)
    has_legacy_disable = bool(_OMZ_LEGACY_DISABLE_RE.search(text))
    if has_safe_zstyle or has_legacy_disable:
        return Check(name=name, ok=True, detail="update prompt suppressed")

    return Check(
        name=name,
        ok=True,
        detail=(
            "tmux + omz default prompt can eat the first keystroke in a new pane. "
            "Add to ~/.zshrc: `zstyle ':omz:update' mode reminder` "
            "(or `auto` / `disabled`)."
        ),
    )


def _managed_agent_check() -> Check:
    """The managed agent is registered as scaffolding only (ADR 0002).

    Reports `ok` because doctor isn't meant to fail on an unwired feature, but
    the detail makes clear nothing actually runs end-to-end yet. Real backend
    wiring will replace this with checks that exercise the client.
    """
    return Check(
        name="managed agent",
        ok=True,
        detail="scaffold only — no backend wired (see ADR 0002)",
    )


def _sync_check() -> Check:
    """Whether background sync is scheduled, and when it last ran (ADR 0005).

    Advisory: sync is opt-in, so "not installed" is a normal state and must not
    fail doctor.
    """
    from goblin_watcher.sync import launchd, store

    if not launchd.is_supported():
        return Check(
            name="background sync",
            ok=True,
            detail="launchd scheduling is macOS-only — see `gw sync install` for a cron line",
        )
    if not launchd.plist_path().exists():
        return Check(
            name="background sync",
            ok=True,
            detail="not scheduled — run `gw sync install` to enable",
        )
    last = store.load_state().last_pass
    when = "never run" if last is None else f"last pass {last.status}"
    return Check(name="background sync", ok=True, detail=f"scheduled · {when}")


def _render(checks: list[Check]) -> None:
    table = Table(title="gw doctor", show_header=True, header_style="bold")
    table.add_column("Check")
    table.add_column("Status", justify="center")
    table.add_column("Detail")
    for c in checks:
        status = "[bold green]ok[/]" if c.ok else "[bold red]fail[/]"
        table.add_row(c.name, status, c.detail)
    console.print(table)


def doctor() -> None:
    cfg = config.load()
    checks = [
        _binary_check("git", required=True),
        _binary_check("gh", required=False),
        _binary_check("op", required=False),
        _binary_check("claude", required=False),
        _binary_check("codex", required=False),
        _binary_check("gemini", required=False),
        # Google Antigravity's CLI installs as `agy`, not `antigravity`.
        _binary_check("agy", required=False),
        _managed_agent_check(),
        _windowing_check(cfg),
        _omz_update_prompt_check(cfg),
        _linear_key_check(),
        _sync_check(),
    ]
    _render(checks)
    if any(not c.ok for c in checks):
        raise typer.Exit(code=1)
