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


def project_meta_dir(project_root: Path) -> Path:
    """Per-project state lives at <project_root>/.goblin/."""
    return project_root / ".goblin"


def project_tasks_dir(project_root: Path) -> Path:
    return project_meta_dir(project_root) / "tasks"


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
