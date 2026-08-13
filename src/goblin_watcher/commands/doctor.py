from __future__ import annotations

import os
import re
import shutil
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING

import typer
from rich.table import Table

from goblin_watcher import config, drift, secrets
from goblin_watcher.agents import AGENT_NAMES, get_agent
from goblin_watcher.console import console, print_success
from goblin_watcher.errors import GoblinError
from goblin_watcher.windowing import WINDOWING_MODES

if TYPE_CHECKING:
    from goblin_watcher.sync.models import PassReport


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


# What the user loses when gw can't parse an agent's transcripts. Kept in one
# place so every agent's row says the same thing about the consequence and
# differs only in the reason.
_TRANSCRIPT_CONSEQUENCE = (
    "session summaries, descriptions, turn counts and idle notifications will be blank"
)


def _transcript_checks() -> list[Check]:
    """One advisory row per agent whose transcripts gw can't parse.

    Reports `ok` — a stubbed transcript reader is a known limitation of the
    agent's session store, not a broken install, and doctor exits non-zero on
    any failed check. The point is that the degradation stops being silent.
    """
    checks: list[Check] = []
    for name in AGENT_NAMES:
        capability = get_agent(name).transcripts
        if capability.parseable:
            continue
        checks.append(
            Check(
                name=f"{name} transcripts",
                ok=True,
                detail=f"not parseable ({capability.reason}) — {_TRANSCRIPT_CONSEQUENCE}",
            )
        )
    return checks


def _sync_check() -> Check:
    """Whether background sync is scheduled, loaded, and actually firing (ADR 0005).

    "Not installed" stays advisory — sync is opt-in. An *installed* job that
    launchd never loaded, or one whose last pass is many intervals old, is a
    genuine failure: nothing else tells you your indicators quietly went stale.
    """
    from goblin_watcher.sync import launchd, store

    name = "background sync"
    if not launchd.is_supported():
        return Check(
            name=name,
            ok=True,
            detail="launchd scheduling is macOS-only — see `gw sync install` for a cron line",
        )
    plist = launchd.plist_path()
    if not plist.exists():
        return Check(
            name=name,
            ok=True,
            detail="not scheduled — run `gw sync install` to enable",
        )
    try:
        loaded = launchd.is_loaded()
    except GoblinError as e:
        return Check(name=name, ok=False, detail=f"could not query launchd: {e.message}")
    if not loaded:
        return Check(
            name=name,
            ok=False,
            detail=(
                f"{plist} is installed but launchd has not loaded it — no pass will ever fire. "
                "Re-run `gw sync install`."
            ),
        )

    interval = launchd.installed_interval()
    last = store.load_state().last_pass
    when = "never run" if last is None else f"last pass {last.status}"
    stale = _sync_staleness(plist, last_finished=_last_pass_at(last), interval=interval)
    if stale is not None:
        return Check(name=name, ok=False, detail=f"scheduled ({when}) but {stale}")
    return Check(name=name, ok=True, detail=f"loaded · {when}")


def _last_pass_at(last: PassReport | None) -> datetime | None:
    """When the most recent pass reported in, tolerating naive stored timestamps."""
    if last is None:
        return None
    when = last.finished_at or last.started_at
    return when if when.tzinfo is not None else when.replace(tzinfo=UTC)


def _sync_staleness(
    plist: Path, *, last_finished: datetime | None, interval: int | None
) -> str | None:
    """Why sync looks like it isn't firing, or None when it's on schedule.

    The plist's mtime stands in for "installed at" when no pass has ever run,
    which is what distinguishes "installed 20 seconds ago" (fine — `RunAtLoad`
    is off, so the first pass waits a full interval) from "installed last week
    and never ran" (broken).
    """
    if interval is None:
        return "the plist's StartInterval is unreadable, so gw can't tell when the next pass is due"
    try:
        installed_at = datetime.fromtimestamp(plist.stat().st_mtime, UTC)
    except OSError:  # pragma: no cover - the caller just stat'd it via exists()
        return None
    since = last_finished or installed_at
    age = int((datetime.now(UTC) - since).total_seconds())
    # Three intervals of slack: a single missed firing is normal (laptop asleep,
    # a pass that overran), three in a row is not.
    if age <= max(interval * 3, 60):
        return None
    what = "no pass has run" if last_finished is None else "the last pass finished"
    return f"{what} {age}s ago with a {interval}s interval — check `gw sync status` and the log"


# Human-facing labels for each drift kind, in the order the drift table sorts
# them: the ones that can lose work if ignored come first.
_DRIFT_LABELS: dict[drift.DriftKind, str] = {
    "orphan-worktree": "untracked worktree",
    "missing-worktree": "worktree gone",
    "missing-branch": "branch gone",
    "orphan-record": "dead task record",
    "missing-exclude": "exclude entries",
    "stale-indicator": "stale indicator cache",
}


def _drift_check(findings: list[drift.Finding]) -> Check:
    """One summary row for state drift; the specifics go in their own table.

    Drift fails doctor. Unlike a missing optional binary, every one of these
    means a `gw` command will misbehave — `gw status` listing a task whose
    worktree is gone, `git status` noisy with `.worktrees/`, a stale indicator
    row shown as if it were current.
    """
    if not findings:
        return Check(name="state drift", ok=True, detail="no drift detected")
    fixable = sum(1 for f in findings if f.repairable)
    counts = ", ".join(
        label if n == 1 else f"{label} x{n}"
        for kind, label in _DRIFT_LABELS.items()
        if (n := sum(1 for f in findings if f.kind == kind))
    )
    suffix = (
        f"{fixable} safely fixable with `gw doctor --repair`"
        if fixable
        else "none safely auto-fixable"
    )
    return Check(name="state drift", ok=False, detail=f"{counts} · {suffix}")


def _render_drift(findings: list[drift.Finding]) -> None:
    order = list(_DRIFT_LABELS)
    table = Table(title="state drift", show_header=True, header_style="bold")
    table.add_column("Kind")
    table.add_column("Where")
    table.add_column("Detail")
    table.add_column("Fix")
    for f in sorted(findings, key=lambda f: (order.index(f.kind), f.where)):
        table.add_row(
            _DRIFT_LABELS[f.kind],
            f.where,
            f.detail,
            "[success]--repair[/]" if f.repairable else "[hint]by hand[/]",
        )
    console.print(table)


def _render_repairs(outcomes: list[drift.RepairOutcome]) -> None:
    if not outcomes:
        console.print("[muted]Nothing to repair.[/]")
        return
    fixed = [o for o in outcomes if o.fixed]
    for o in outcomes:
        label = _DRIFT_LABELS[o.finding.kind]
        if o.fixed:
            console.print(f"  [success]fixed[/] {label} ({o.finding.where}): {o.detail}")
        else:
            console.print(f"  [error]failed[/] {label} ({o.finding.where}): {o.detail}")
    print_success(f"Repaired {len(fixed)} of {len(outcomes)} finding(s).")


def _render(checks: list[Check]) -> None:
    table = Table(title="gw doctor", show_header=True, header_style="bold")
    table.add_column("Check")
    table.add_column("Status", justify="center")
    table.add_column("Detail")
    for c in checks:
        status = "[bold green]ok[/]" if c.ok else "[bold red]fail[/]"
        table.add_row(c.name, status, c.detail)
    console.print(table)


def doctor(
    repair: bool = typer.Option(
        False,
        "--repair",
        help="Apply the safely-fixable state repairs, then re-check.",
    ),
) -> None:
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
        *_transcript_checks(),
        _windowing_check(cfg),
        _omz_update_prompt_check(cfg),
        _linear_key_check(),
        _sync_check(),
    ]

    findings = drift.detect()
    outcomes: list[drift.RepairOutcome] = []
    if repair:
        outcomes = drift.repair(findings)
        # Re-detect so the table reports post-repair truth, including anything a
        # failed fix left behind.
        findings = drift.detect()
    checks.append(_drift_check(findings))

    _render(checks)
    if repair:
        _render_repairs(outcomes)
    if findings:
        _render_drift(findings)
        if not repair and any(f.repairable for f in findings):
            console.print("[hint]Run `gw doctor --repair` to fix the safe subset.[/]")

    if any(not c.ok for c in checks):
        raise typer.Exit(code=1)
