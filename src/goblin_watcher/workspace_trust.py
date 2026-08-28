"""Pre-accept an agent's first-run workspace-trust prompt (ADR 0014).

gw's whole shape is "one task, one directory the agent has never seen": a
worktree under `<project>/.worktrees/`, a multi-repo workspace, a scratch
space. An agent that gates the first run in a directory behind a trust dialog
therefore gates *every* gw task behind one, and a headless fleet run meets it
with nobody at the terminal to answer.

The gate is `defaults.trust_workspaces`. Recording trust is the agent's job
(`Agent.pretrust_workspace`), because the file and its format belong to the
agent; this module is only the config check and the fail-open wrapper.
"""

from __future__ import annotations

import logging
from pathlib import Path

from goblin_watcher import config
from goblin_watcher.agents.base import Agent
from goblin_watcher.console import console

_log = logging.getLogger(__name__)


def apply(agent: Agent, cwd: Path, *, cfg: config.Config | None = None) -> bool:
    """Record `cwd` as trusted for `agent`. Returns True when it wrote.

    Never raises. The trust prompt is answerable by hand in two keystrokes, so
    a bookkeeping write that failed — an unreadable config, a lock someone else
    is holding, a format that moved — does not get to veto the session the user
    actually asked for. Same posture as `linear_transitions.apply`.
    """
    try:
        if not (cfg or config.load()).defaults.trust_workspaces:
            return False
        return agent.pretrust_workspace(cwd)
    except Exception as e:
        # Deliberately broad: this runs between the user and the agent launch.
        _log.debug("could not pre-trust %s for %s: %s", cwd, agent.name, e)
        console.print(f"[muted]Could not pre-accept {agent.name}'s trust prompt for {cwd}: {e}[/]")
        return False
