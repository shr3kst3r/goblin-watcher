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
from goblin_watcher.errors import TaskNotFoundError
from goblin_watcher.models import AgentName, GhIssue, Project, SessionRecord, Task
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


def _persist_record(
    project: Project,
    task: Task,
    record: SessionRecord,
    drop_session_id: str | None = None,
    *,
    create_if_missing: bool = False,
) -> Task:
    """Upsert one session record onto the task under its lock (ADR 0004).

    An agent session runs for minutes to hours, so the `task` this function is
    handed post-run is arbitrarily stale. Writing it back wholesale would revert
    every update any other process made meanwhile — Linear state, PR backfill,
    descriptions. Instead we re-read under the lock and upsert just this record
    (optionally dropping a placeholder id it replaces).

    `create_if_missing` is for the pre-dispatch write only. On the post-run
    write the record being gone means the task was removed while the agent ran
    (`gw task rm`, or a `gw sync` prune) — recreating it there would resurrect a
    task whose worktree and branch are already deleted.
    """

    def _mutate(latest: Task) -> Task:
        out = latest
        if drop_session_id is not None:
            out = out.model_copy(
                update={"sessions": [s for s in out.sessions if s.session_id != drop_session_id]}
            )
        return sessions.upsert(out, record)

    try:
        return state.update_task(project, task.id, _mutate)
    except TaskNotFoundError:
        updated = _mutate(task)
        if create_if_missing:
            state.save_task(project, updated)
        return updated


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

    # Agents that accept a caller-chosen session id (claude's `--session-id`)
    # get one up-front, so the record we save before dispatch already carries
    # the *real* id — windowers like tmux detach before the agent writes its
    # transcript, making post-launch capture impossible there.
    preassigned: str | None = None
    if isinstance(choice, Fresh):
        preassigned = agent.new_session_id()
        cmd = agent.spawn_command(
            prompt=choice.prompt, cwd=cwd, unsafe=unsafe, session_id=preassigned
        )
    else:
        cmd = agent.resume_command(session_id=choice.session_id, cwd=cwd, unsafe=unsafe)

    console.print(f"[muted]$ {' '.join(shlex.quote(arg) for arg in cmd)}  (cwd={cwd})[/]")

    # Save the SessionRecord BEFORE dispatch. Tmux replaces this process via
    # execvp (when attaching) or returns immediately after `send-keys` (when
    # already inside tmux). Either way, anything we'd save after windower.run
    # might never get written. For Fresh sessions we synthesize an id; inline
    # mode reconciles to the agent's real id once the agent has exited.
    is_fresh = isinstance(choice, Fresh)
    if isinstance(choice, Fresh):
        initial_id = preassigned or _new_id()
        label: str | None = _label_from_prompt(choice.prompt)
    else:
        initial_id = choice.session_id
        label = None
    pre_record = SessionRecord(
        agent=cast(AgentName, agent.name),
        session_id=initial_id,
        created_at=_now(),
        last_used_at=_now(),
        label=label,
    )
    task = _persist_record(project, task, pre_record, create_if_missing=True)

    exit_code = windower.run(task=task, cmd=cmd, cwd=cwd, env=extra_env)

    # Tmux hands the agent off to a background pane and returns while the agent
    # is still starting up, so a post-launch `capture_session_id` would race
    # with the agent's first write. Leave the pre-saved record in place — for
    # agents with a preassigned id it already holds the real one.
    if windower.name == "tmux":
        return exit_code, task

    # A preassigned id IS the session's id by construction; capturing would
    # only risk picking up an older transcript when the agent exited before
    # writing its own (e.g. user quit immediately).
    captured = None if preassigned else agent.capture_session_id(cwd)
    drop_id: str | None = None
    if captured and captured != initial_id:
        if is_fresh:
            # Replace the synthetic placeholder with one keyed on the agent's
            # real id.
            drop_id = initial_id
            final_record = pre_record.model_copy(update={"session_id": captured})
        else:
            # Resume that forked into a new transcript: keep the resumed record
            # and add a new one alongside for the forked transcript.
            final_record = pre_record.model_copy(update={"session_id": captured, "label": None})
    else:
        final_record = pre_record
    # Transcript parsing happens before we take the lock — it reads a JSONL file
    # that can be large, and ADR 0004 keeps lock hold times to milliseconds.
    final_record = sessions.refresh_summary(task, final_record)
    task = _persist_record(project, task, final_record, drop_session_id=drop_id)
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
    templates_dir = Path(__file__).parent.parent / "templates"
    addition = prompt_addition.resolve_for_task_project(task.project).strip()
    addition_block = f"{addition}\n\n" if addition else ""
    prompt = (user_prompt or "").strip()
    intro = _PROMPTED_INTRO if prompt else _DEFAULT_INTRO
    trailer = prompt if prompt else _DEFAULT_TRAILER
    if task.kind == "scratch":
        # Scratch spaces have no repo, branch, or PR flow — a dedicated
        # template avoids telling the agent to `gw pr open` a plain directory.
        return (
            (templates_dir / "scratch_prompt.md")
            .read_text()
            .format(
                intro=intro,
                name=task.id,
                directory=task.worktree_path,
                addition_block=addition_block,
                trailer=trailer,
            )
        )
    template = (templates_dir / "spawn_prompt.md").read_text()
    return template.format(
        intro=intro,
        ticket_id=task.ticket_id,
        title=task.ticket_title or task.id,
        repos_block=_format_repos_block(task),
        description=_format_ticket_context(task),
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


def _format_ticket_context(task: Task) -> str:
    """Render the tracking item's description block for the seed prompt.

    Linear tickets contribute their description plus the comment thread; GitHub
    issues contribute their body. A task with neither says so explicitly, so the
    agent knows the thin prompt is the whole brief rather than a truncation.
    """
    if task.linear is not None:
        return _format_linear_context(task.linear)
    if task.github_issue is not None:
        return _format_github_issue_context(task.github_issue)
    return "(no Linear issue or GitHub issue attached — fresh task)"


def _format_github_issue_context(issue: GhIssue) -> str:
    header = f"GitHub issue {issue.reference} ({issue.state.lower()}): {issue.url}"
    if issue.labels:
        header += f"\nLabels: {', '.join(issue.labels)}"
    body = (issue.body or "").strip()
    if not body:
        return f"{header}\n\n(The issue has no description.)"
    return f"{header}\n\n{body}"


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
