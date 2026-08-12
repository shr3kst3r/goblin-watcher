from __future__ import annotations

from goblin_watcher.agents.antigravity import AntigravityAgent
from goblin_watcher.agents.base import Agent
from goblin_watcher.agents.claude import ClaudeAgent
from goblin_watcher.agents.codex import CodexAgent
from goblin_watcher.agents.gemini import GeminiAgent
from goblin_watcher.agents.managed import ManagedAgent
from goblin_watcher.errors import GoblinError
from goblin_watcher.models import Project

# Static — no plugin system.
registry: dict[str, type[Agent]] = {
    "claude": ClaudeAgent,
    "codex": CodexAgent,
    "gemini": GeminiAgent,
    "antigravity": AntigravityAgent,
    "managed": ManagedAgent,
}

AGENT_NAMES = tuple(registry)


def get_agent(name: str) -> Agent:
    cls = registry.get(name.lower())
    if cls is None:
        raise GoblinError(
            f"Unknown agent {name!r}.",
            hint=f"Pick one of: {', '.join(AGENT_NAMES)}.",
        )
    return cls()


def validate_agent_for_project(agent_name: str, project: Project) -> None:
    """Pre-flight check before spawning `agent_name` against `project`.

    Currently only gates the managed agent on the project having a remote
    (ADR 0002): without one there's nowhere for the sandbox to clone from.
    Local agents have no project-level prerequisites.
    """
    if agent_name.lower() != "managed":
        return
    if project.kind == "scratch":
        raise GoblinError(
            "The managed agent can't run in a scratch space (no git remote to clone).",
            hint="Pick a local agent (claude/codex/gemini/antigravity).",
        )
    if project.repo_url:
        return
    raise GoblinError(
        f"The managed agent requires project {project.name!r} to have a git remote.",
        hint=(
            "Local-only checkouts can't be cloned by the managed sandbox. "
            "Add a remote to the project (or re-register it via `gw project new --repo URL`) "
            "or pick a local agent (claude/codex/gemini/antigravity)."
        ),
    )
