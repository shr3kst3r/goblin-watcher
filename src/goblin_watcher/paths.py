import os
from pathlib import Path

APP_NAME = "goblin-watcher"


def _xdg(env_var: str, default_subpath: tuple[str, ...]) -> Path:
    """XDG-style resolution: honor the env var if set, otherwise fall back under ~."""
    value = os.environ.get(env_var)
    if value:
        return Path(value)
    return Path.home().joinpath(*default_subpath)


def data_dir() -> Path:
    """`$XDG_DATA_HOME/goblin-watcher`, defaulting to `~/.local/share/goblin-watcher`."""
    return _xdg("XDG_DATA_HOME", (".local", "share")) / APP_NAME


def config_dir() -> Path:
    """`$XDG_CONFIG_HOME/goblin-watcher`, defaulting to `~/.config/goblin-watcher`."""
    return _xdg("XDG_CONFIG_HOME", (".config",)) / APP_NAME


def state_file() -> Path:
    return data_dir() / "state.json"


def config_file() -> Path:
    return config_dir() / "config.toml"


def global_prompt_file() -> Path:
    """User-wide prompt addition appended to every spawn prompt."""
    return config_dir() / "prompt.md"


def logs_dir() -> Path:
    return data_dir() / "logs"


def global_lock_file() -> Path:
    """Advisory-lock sidecar guarding read-modify-write of the global registry.

    A stable path, deliberately not `state.json` itself: atomic writes replace
    that file's inode, so a lock held on it would not be seen by other writers.
    """
    return data_dir() / "state.lock"


def sync_dir() -> Path:
    """Background-sync tier: pass state, the indicator cache, and the pass lock."""
    return data_dir() / "sync"


def sync_state_file() -> Path:
    """Last-pass summary plus the edge-trigger and description-backoff maps."""
    return sync_dir() / "state.json"


def sync_cache_file() -> Path:
    """Derived per-task git/PR indicators. Regenerable; never authoritative."""
    return sync_dir() / "indicators.json"


def sync_lock_file() -> Path:
    """Single-instance guard: one sync pass at a time, machine-wide."""
    return sync_dir() / "pass.lock"


def sync_journal_file() -> Path:
    """Append-only JSONL record of every sync pass, action, and notification."""
    return logs_dir() / "sync.jsonl"


def sync_launchd_log_file() -> Path:
    """stdout/stderr of scheduled passes, as redirected by the launchd plist."""
    return logs_dir() / "sync.launchd.log"


def project_meta_dir(project_root: Path) -> Path:
    """Per-project state lives at <project_root>/.goblin/."""
    return project_root / ".goblin"


def project_tasks_dir(project_root: Path) -> Path:
    return project_meta_dir(project_root) / "tasks"


def project_logs_dir(project_root: Path) -> Path:
    """Per-project agent output logs, written by the headless windower.

    Project-scoped rather than global: an unattended run's output belongs next
    to the task record it came from, and `.goblin/` is already excluded from
    the user's repo via `.git/info/exclude`.
    """
    return project_meta_dir(project_root) / "logs"


def task_lock_file(project_root: Path, task_id: str) -> Path:
    """Advisory-lock sidecar for one task record.

    Dot-prefixed and `.lock`-suffixed so `state.list_tasks`' `*.json` glob
    never sees it.
    """
    return project_tasks_dir(project_root) / f".{task_id}.lock"


def project_prompt_file(project_root: Path) -> Path:
    """Per-project prompt addition; presence overrides the global addition."""
    return project_meta_dir(project_root) / "prompt.md"


def worktree_root(project_root: Path, override: Path | None = None) -> Path:
    """Default worktree root is <project_root>/.worktrees/."""
    return override if override is not None else project_root / ".worktrees"


def projects_root() -> Path:
    """Parent directory for newly cloned projects (~/goblin)."""
    return Path.home() / "goblin"


def scratch_root() -> Path:
    """Root of the reserved scratch project; each scratch space is a subdir."""
    return projects_root() / "scratch"


def workspace_root() -> Path:
    """Parent directory for multi-repo task workspaces.

    Lives in the global data tier so no single project's repo is privileged as
    the filesystem parent of a cross-repo workspace.
    """
    return data_dir() / "workspaces"


def task_workspace(task_id: str) -> Path:
    """Workspace directory for a multi-repo task; each repo's worktree is a subdir."""
    return workspace_root() / task_id
