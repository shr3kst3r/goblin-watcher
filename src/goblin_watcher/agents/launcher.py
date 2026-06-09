"""Orchestrates agent spawn/resume + session capture + summary refresh."""

from __future__ import annotations

import shlex
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import cast

from goblin_watcher import prompt_addition, sessions, state
from goblin_watcher.agents.base import Agent
from goblin_watcher.console import console
from goblin_watcher.models import AgentName, Project, SessionRecord, Task
from goblin_watcher.windowing.base import Windower


@dataclass
class Resume:
    session_id: str


@dataclass
class Fresh:
    prompt: str


SessionChoice = Resume | Fresh


def _now() -> datetime:
    return datetime.now(UTC)


def _new_id() -> str:
    # Synthesized id for agents that don't expose a stable one. ULID-ish but
    # sticking with uuid4 to avoid an extra dep.
    return uuid.uuid4().hex[:24]


def _label_from_prompt(prompt: str, max_len: int = 80) -> str:
    text = " ".join(prompt.split())
    return text[:max_len] + ("…" if len(text) > max_len else "")


def launch(
    *,
    project: Project,
    task: Task,
    agent: Agent,
    choice: SessionChoice,
    windower: Windower,
    unsafe: bool = False,
) -> tuple[int, Task]:
    """Run the agent for `task`. Returns (exit_code, updated_task)."""
    # A multi-repo task launches in its workspace (each repo is a subdir);
    # a single-repo task launches directly in its worktree.
    cwd = task.agent_cwd
    # Windowers receive only the agent's *extra* vars; inline merges them into
    # os.environ itself, tmux injects them into the pane command (the pane's
    # shell can't inherit this process's environment).
    extra_env = agent.env()

    if isinstance(choice, Fresh):
        cmd = agent.spawn_command(prompt=choice.prompt, cwd=cwd, unsafe=unsafe)
    else:
        cmd = agent.resume_command(session_id=choice.session_id, cwd=cwd, unsafe=unsafe)

    console.print(f"[muted]$ {' '.join(shlex.quote(arg) for arg in cmd)}  (cwd={cwd})[/]")

    # Save the SessionRecord BEFORE dispatch. Tmux replaces this process via
    # execvp (when attaching) or returns immediately after `send-keys` (when
    # already inside tmux). Either way, anything we'd save after windower.run
    # might never get written. For Fresh sessions we synthesize an id; inline
    # mode reconciles to the agent's real id once the agent has exited.
    is_fresh = isinstance(choice, Fresh)
    initial_id = _new_id() if is_fresh else choice.session_id
    pre_record = SessionRecord(
        agent=cast(AgentName, agent.name),
        session_id=initial_id,
        created_at=_now(),
        last_used_at=_now(),
        label=_label_from_prompt(choice.prompt) if isinstance(choice, Fresh) else None,
    )
    task = sessions.upsert(task, pre_record)
    state.save_task(project, task)

    exit_code = windower.run(task=task, cmd=cmd, cwd=cwd, env=extra_env)

    # Tmux hands the agent off to a background pane and returns while the agent
    # is still starting up, so a post-launch `capture_session_id` would race
    # with the agent's first write. Leave the pre-saved record in place.
    if windower.name == "tmux":
        return exit_code, task

    captured = agent.capture_session_id(cwd)
    if captured and captured != initial_id:
        if is_fresh:
            # Replace the synthetic placeholder with one keyed on the agent's
            # real id.
            task = task.model_copy(
                update={"sessions": [s for s in task.sessions if s.session_id != initial_id]}
            )
            real_record = pre_record.model_copy(update={"session_id": captured})
        else:
            # Resume that forked into a new transcript: keep the resumed record
            # and add a new one alongside for the forked transcript.
            real_record = pre_record.model_copy(update={"session_id": captured, "label": None})
        real_record = sessions.refresh_summary(task, real_record)
        task = sessions.upsert(task, real_record)
    else:
        refreshed = sessions.refresh_summary(task, pre_record)
        task = sessions.upsert(task, refreshed)
    state.save_task(project, task)
    return exit_code, task


_DEFAULT_INTRO = "(Context only — do not begin working until I give you a direct instruction.)"
_DEFAULT_TRAILER = "Wait for my next message before taking any action."
_PROMPTED_INTRO = (
    "(Context for your task. Your instructions are at the bottom — begin work on those.)"
)


def build_seed_prompt(task: Task, user_prompt: str | None = None) -> str:
    """Construct the prompt seeded into a fresh agent session.

    When `user_prompt` is provided, the trailing "wait for my next message"
    line is replaced with the user's prompt and the intro is rephrased so the
    agent treats it as the task to begin working on.
    """
    template_path = Path(__file__).parent.parent / "templates" / "spawn_prompt.md"
    template = template_path.read_text()
    linear = task.linear
    addition = prompt_addition.resolve_for_task_project(task.project).strip()
    addition_block = f"{addition}\n\n" if addition else ""
    prompt = (user_prompt or "").strip()
    intro = _PROMPTED_INTRO if prompt else _DEFAULT_INTRO
    trailer = prompt if prompt else _DEFAULT_TRAILER
    return template.format(
        intro=intro,
        linear_id=linear.identifier if linear else task.id.upper(),
        title=linear.title if linear else task.id,
        repos_block=_format_repos_block(task),
        description=_format_linear_context(linear),
        addition_block=addition_block,
        trailer=trailer,
    )


def _format_repos_block(task: Task) -> str:
    """Render the branch/worktree section of the seed prompt.

    Single-repo output is byte-identical to the original two-line block. A
    multi-repo task lists every repo and points the agent at the shared
    workspace it was launched in.
    """
    if not task.is_multi_repo:
        return f"Branch: {task.branch} (off {task.base_branch})\nWorktree: {task.worktree_path}"
    lines = [
        f"This task spans {len(task.all_repos())} repositories. You are running in a "
        "workspace that holds each repo as a subdirectory:",
        f"Workspace: {task.workspace_path}",
        "",
    ]
    for repo in task.all_repos():
        lines.append(
            f"- {repo.project}: {repo.worktree_path}  (branch {repo.branch} off {repo.base_branch})"
        )
    return "\n".join(lines)


def _format_linear_context(linear: object) -> str:
    """Render the Linear description + comments block for the seed prompt."""
    from goblin_watcher.models import LinearIssue

    if not isinstance(linear, LinearIssue):
        return "(no Linear issue attached — fresh task)"

    parts: list[str] = []
    if linear.description:
        parts.append(f"Linear issue:\n{linear.description}")
    if linear.comments:
        rendered = "\n\n".join(
            f"[{c.created_at.strftime('%Y-%m-%d %H:%M UTC')} · {c.author or 'unknown'}]\n{c.body}"
            for c in linear.comments
        )
        parts.append(f"Linear comments (oldest first):\n\n{rendered}")
    if not parts:
        return "(Linear ticket has no description or comments yet.)"
    return "\n\n".join(parts)
