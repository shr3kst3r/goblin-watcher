"""User-configured text appended to the seed prompt sent to fresh agents.

Two scopes:
- Global at `$XDG_CONFIG_HOME/goblin-watcher/prompt.md` — applies everywhere.
- Project at `<project_root>/.goblin/prompt.md` — overrides the global for one
  project. File *presence* is the override signal, not content: writing an
  empty project file suppresses the global addition for that project.
"""

from __future__ import annotations

from pathlib import Path

from goblin_watcher import paths, state
from goblin_watcher.errors import ProjectNotFoundError
from goblin_watcher.models import Project


def global_file() -> Path:
    return paths.global_prompt_file()


def project_file(project: Project) -> Path:
    return paths.project_prompt_file(project.root)


def load_global() -> str:
    f = global_file()
    return f.read_text() if f.exists() else ""


def load_project(project: Project) -> str:
    f = project_file(project)
    return f.read_text() if f.exists() else ""


def has_project_override(project: Project) -> bool:
    return project_file(project).exists()


def resolve(project: Project | None) -> str:
    """Return the addition that should be appended to the seed prompt.

    Project-level file wins if it exists (even when empty), so users can
    suppress the global addition for a single project by saving an empty file.
    """
    if project is not None and has_project_override(project):
        return load_project(project)
    return load_global()


def resolve_for_task_project(project_name: str) -> str:
    """Lookup-by-name variant for use at seed-prompt build time."""
    try:
        proj = state.get_project(project_name)
    except ProjectNotFoundError:
        return resolve(None)
    return resolve(proj)


def save_global(text: str) -> None:
    f = global_file()
    f.parent.mkdir(parents=True, exist_ok=True)
    f.write_text(text)


def save_project(project: Project, text: str) -> None:
    f = project_file(project)
    f.parent.mkdir(parents=True, exist_ok=True)
    f.write_text(text)


def clear_global() -> bool:
    """Delete the global addition file. Returns True if a file was removed."""
    f = global_file()
    if not f.exists():
        return False
    f.unlink()
    return True


def clear_project(project: Project) -> bool:
    """Delete the project addition file. Returns True if a file was removed."""
    f = project_file(project)
    if not f.exists():
        return False
    f.unlink()
    return True
