"""User config at $XDG_CONFIG_HOME/goblin-watcher/config.toml.

Schema is intentionally small. Unknown keys are tolerated.
"""

from __future__ import annotations

import tomllib
from pathlib import Path
from typing import Any, Literal

import tomli_w
from pydantic import BaseModel, ConfigDict, Field

from goblin_watcher import paths

Windowing = str  # "inline" | "tmux" | "headless" — validated where used, not here.

# Orientation of additional panes opened by `tmux split-window` for second+
# sessions on the same task. "vertical" stacks panes top-over-bottom (tmux
# `-v`); "horizontal" places them side-by-side (tmux `-h`).
SplitDirection = Literal["vertical", "horizontal"]


class LinearConfig(BaseModel):
    api_key: str | None = None  # literal key or "op://..." reference


class TmuxConfig(BaseModel):
    session_name: str = "goblin"
    attach_on_spawn: bool = True
    split: SplitDirection = "vertical"
    # When true, set tmux `monitor-silence` on each goblin task window so it
    # gets a `~` marker in the status bar when its pane sees no output for N
    # seconds. Detects "agent done / waiting for input" without stealing focus.
    mark_idle: bool = False
    mark_idle_seconds: int = 5


class DefaultsConfig(BaseModel):
    agent: str | None = None  # "claude" | "codex" | "gemini" | "antigravity"
    windowing: Windowing = "inline"
    summary_ttl_seconds: int = 30
    unsafe: bool = True  # Default to bypass-permission mode; set `unsafe = false` to opt out.
    # LLM-generated session descriptions (lazy, background-refreshed).
    description_ttl_seconds: int = 900  # 15 minutes
    description_agent: str = "claude"  # "claude" | "codex" | "off"
    description_model: str = "claude-haiku-4-5"
    # Whole-transcript char cap for the description prompt. Claude is asked to
    # characterize the entire session; very long transcripts are truncated
    # head + tail with a marker in between. ~80k chars ≈ 20k input tokens ≈
    # $0.02 per Haiku refresh.
    description_max_transcript_chars: int = 80_000
    # How long a task's cached Linear workflow state stays fresh before
    # `gw status` re-fetches it. One API round-trip per Linear-backed task is
    # slow with many tasks; the cache keeps status snappy. 0 = always fetch.
    linear_state_ttl_seconds: int = 300
    # Same, for a task's cached GitHub issue state (`gh issue view`). Cheaper
    # than the Linear round-trip but still a subprocess per issue-backed task.
    github_issue_state_ttl_seconds: int = 300
    # A session whose transcript was modified within this window shows as
    # `● active` in `gw status`; older activity shows as `idle <age>`.
    activity_active_seconds: int = 120


# Which notification transport `gw sync` uses. "auto" resolves to "macos" on
# darwin and "off" elsewhere.
NotifyTransport = Literal["auto", "macos", "command", "off"]

# Events a sync pass can notify on. Edge-triggered: each fires once, when the
# underlying state actually changes (ADR 0005).
SyncEvent = Literal[
    "agent-idle",
    "pr-merged",
    "checks-failed",
    "checks-passed",
    "prunable",
]

_DEFAULT_SYNC_EVENTS: tuple[SyncEvent, ...] = (
    "agent-idle",
    "pr-merged",
    "checks-failed",
    "checks-passed",
    "prunable",
)


class SyncConfig(BaseModel):
    """Background-sync settings (ADR 0005). Sync only runs if scheduled."""

    # How often the launchd/cron job fires. Also the worst-case staleness of
    # anything `gw status` reads from the sync cache.
    interval_seconds: int = 300
    # Auto-prune tasks that are merged AND have a clean worktree. Never forces:
    # dirty or ambiguous tasks are reported, never deleted.
    prune: bool = True
    # Prune scratch spaces idle more than N days. 0 disables (scratch spaces
    # have no merge signal, so idleness is the only criterion).
    scratch_prune_days: int = 0
    notify: NotifyTransport = "auto"
    # argv for the "command" transport; title and body are appended as the
    # final two arguments. Never shell-interpolated.
    notify_command: list[str] = Field(default_factory=list)
    notify_events: list[SyncEvent] = Field(default_factory=lambda: list(_DEFAULT_SYNC_EVENTS))


# One `setup.run` step: either a shell command line, executed via `sh -c` so
# `&&`, pipes, and `$VARS` behave as typed, or an argv list exec'd directly with
# no shell in between.
SetupCommand = str | list[str]


class SetupConfig(BaseModel):
    """Bootstrap applied to a freshly materialized worktree.

    A worktree is a bare checkout: everything gitignored (`.env`, `.venv`,
    `node_modules`) is missing, so the first thing an agent does is rediscover
    the project's bootstrap. These three lists close that gap.

    `copy` and `link` entries are paths *relative to the project root*, and are
    reproduced at the same relative path inside the worktree. They are refused
    if they resolve outside the root (see `worktree_setup.resolve_inside`).

    The Python names carry a `_paths` suffix because `copy` alone shadows
    `BaseModel.copy`; the TOML keys are the plain `copy` / `link` aliases.
    """

    model_config = ConfigDict(populate_by_name=True)

    copy_paths: list[str] = Field(default_factory=list, alias="copy", serialization_alias="copy")
    link_paths: list[str] = Field(default_factory=list, alias="link", serialization_alias="link")
    run: list[SetupCommand] = Field(default_factory=list)
    # Per-`run`-step wall-clock cap. A hung bootstrap otherwise blocks the spawn
    # forever with no output.
    timeout_seconds: int = 600

    @property
    def is_empty(self) -> bool:
        return not (self.copy_paths or self.link_paths or self.run)


class Config(BaseModel):
    defaults: DefaultsConfig = Field(default_factory=DefaultsConfig)
    linear: LinearConfig = Field(default_factory=LinearConfig)
    tmux: TmuxConfig = Field(default_factory=TmuxConfig)
    sync: SyncConfig = Field(default_factory=SyncConfig)
    setup: SetupConfig = Field(default_factory=SetupConfig)


def load() -> Config:
    f = paths.config_file()
    if not f.exists():
        return Config()
    raw: dict[str, Any] = tomllib.loads(f.read_text())
    return Config.model_validate(raw)


def dump_toml_dict(cfg: Config) -> dict[str, Any]:
    """The config as TOML-shaped data: aliases applied, `None`s dropped.

    `by_alias` is what turns `setup.copy_paths` back into the `copy` key users
    actually write; every other field aliases to itself.
    """
    return cfg.model_dump(exclude_none=True, by_alias=True)


def save(cfg: Config) -> None:
    f = paths.config_file()
    f.parent.mkdir(parents=True, exist_ok=True)
    # TOML doesn't accept None; emit only the keys the user has set.
    f.write_bytes(tomli_w.dumps(dump_toml_dict(cfg)).encode())


def config_path() -> Path:
    return paths.config_file()
