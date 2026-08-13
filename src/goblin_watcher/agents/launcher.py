"""Orchestrates agent spawn/resume + session capture + summary refresh."""

from __future__ import annotations

import re
import shlex
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import cast

from goblin_watcher import gh, modes, prompt_addition, sessions, state
from goblin_watcher.agents.base import Agent
from goblin_watcher.console import console
from goblin_watcher.errors import GoblinError, TaskNotFoundError
from goblin_watcher.models import AgentName, GhIssue, Project, SessionRecord, Task
from goblin_watcher.modes import ModeSpec
from goblin_watcher.review_feed import RepoReview, ReviewFeed, clip_body, clip_hunk
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


def _check_headless(choice: SessionChoice, unsafe: bool) -> None:
    """Guard the headless path before anything is persisted or spawned.

    Resume is refused rather than approximated: an agent's print mode is
    "run this prompt to completion", and resuming a conversation with nothing
    to say would either hang on stdin or burn a turn to no effect. Starting a
    fresh headless run with the follow-up as its prompt is the honest
    equivalent.
    """
    if isinstance(choice, Resume):
        raise GoblinError(
            "Headless windowing can only start a fresh session, not resume one.",
            hint="Drop --session (optionally passing --prompt with what you want done), "
            "or pick --windowing inline/tmux to resume interactively.",
        )
    if not unsafe:
        # Not fatal — some agents' print mode is useful read-only — but an
        # unattended run that stops dead at its first permission prompt looks
        # like a hang, so say so up front.
        console.print(
            "[hint]Warning[/]: headless run without --unsafe. The agent will be blocked "
            "on any tool call that needs approval, with nobody there to approve it."
        )


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
    if windower.headless:
        _check_headless(choice, unsafe)
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
        # A headless windower has no terminal to draw a TUI on, so the agent
        # runs its print/exec mode instead (`_check_headless` has already
        # ruled out the resume branch).
        build = agent.headless_command if windower.headless else agent.spawn_command
        cmd = build(prompt=choice.prompt, cwd=cwd, unsafe=unsafe, session_id=preassigned)
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

    # `session_id` is what lets `gw session send` address this run later: tmux
    # stamps it on the pane it opens.
    exit_code = windower.run(
        task=task, cmd=cmd, cwd=cwd, env=extra_env, session_id=pre_record.session_id
    )

    # Detaching windowers (tmux, headless) return while the agent is still
    # starting up, so a post-launch `capture_session_id` would race with its
    # first write. Leave the pre-saved record in place — for agents with a
    # preassigned id it already holds the real one.
    if windower.detaches:
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


def build_seed_prompt(
    task: Task,
    user_prompt: str | None = None,
    *,
    research: bool = False,
    review: ReviewFeed | None = None,
    mode: ModeSpec | None = None,
) -> str:
    """Construct the prompt seeded into a fresh agent session.

    Every brief is rendered from a template carrying the same task-context slots
    (ADR 0006):

    - Default: `spawn_prompt.md`. When `user_prompt` is provided, the trailing
      "wait for my next message" line is replaced with the user's prompt and the
      intro is rephrased so the agent treats it as the task to begin working on.
    - `mode=<spec>`: the named work mode's own template (ADR 0009), rendered
      with the same slots plus a `{focus}` paragraph that `user_prompt` narrows
      the session with instead of becoming the trailer. A *seed* mode never
      reaches here — its literal message bypasses the seed prompt entirely.
    - `research=True`: shorthand for the built-in `research` mode, kept for
      callers that predate `--mode`.
    - `review=<feed>`: `address_review_prompt.md`. The PR's unresolved review
      threads and failing-check output are embedded in the brief (ADR 0008), and
      `user_prompt` narrows the focus as it does for a mode.

    `review` and the mode arguments are mutually exclusive; the command layer
    rejects the combination before it reaches here, and `review` wins if one
    ever slips past.
    """
    templates_dir = Path(__file__).parent.parent / "templates"
    addition = prompt_addition.resolve_for_task_project(task.project).strip()
    addition_block = f"{addition}\n\n" if addition else ""
    prompt = (user_prompt or "").strip()
    if review is not None:
        return (
            (templates_dir / "address_review_prompt.md")
            .read_text()
            .format(
                ticket_id=task.ticket_id,
                title=task.ticket_title or task.id,
                repos_block=_format_repos_block(task),
                description=format_ticket_context(task),
                addition_block=addition_block,
                review_block=format_review_block(review),
                focus=_format_focus("Focus on the following in particular", prompt),
            )
        )
    if mode is None and research:
        mode = modes.BUILTIN_MODES["research"]
    if mode is not None and mode.template is not None:
        # Returns before the intro/trailer machinery below: the mode's template
        # fixes both, and `user_prompt` becomes a focus paragraph instead. There
        # is no scratch variant either — the command layer is the guard,
        # rejecting a `requires_ticket` mode for any task with no tracking item
        # (scratch tasks included).
        return render_mode_prompt(mode, task, user_prompt=prompt, addition_block=addition_block)
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
        description=format_ticket_context(task),
        addition_block=addition_block,
        trailer=trailer,
    )


def render_mode_prompt(
    mode: ModeSpec, task: Task, *, user_prompt: str = "", addition_block: str = ""
) -> str:
    """Render a template mode's brief with the shared task-context slots.

    A user-authored template is as likely to be wrong as any other config, so an
    unknown `{slot}` becomes a `GoblinError` naming what it may reference rather
    than a `KeyError` traceback out of `str.format`.
    """
    path = modes.template_path(mode)
    slots = {
        "ticket_id": task.ticket_id,
        "title": task.ticket_title or task.id,
        "repos_block": _format_repos_block(task),
        "description": format_ticket_context(task),
        "addition_block": addition_block,
        "focus": _format_focus(mode.focus_lead, user_prompt.strip()),
    }
    try:
        return path.read_text().format(**slots)
    except (KeyError, IndexError) as exc:
        raise GoblinError(
            f"Mode {mode.name!r} template {path} references a slot gw doesn't fill: {exc}",
            hint=f"Templates may use {{{'}}, {{'.join(modes.TEMPLATE_SLOTS)}}}. "
            "Double any literal brace you meant to keep ({{ and }}).",
        ) from exc


def _fenced(text: str, lang: str = "") -> str:
    """Wrap `text` in a code fence long enough to survive backticks inside it.

    A diff of a markdown file — or a CI log echoing one — routinely contains a
    ``` run of its own. A fixed three-backtick fence would close there and the
    remainder of the hunk would read as instructions rather than as evidence.
    """
    longest = max((len(run) for run in re.findall(r"`+", text)), default=0)
    fence = "`" * max(3, longest + 1)
    return f"{fence}{lang}\n{text}\n{fence}"


def _format_thread(index: int, thread: gh.ReviewThread) -> str:
    """One unresolved review thread: where it points, the hunk, then the replies."""
    where = thread.path or "(file no longer in the diff)"
    if thread.line is not None:
        where = f"{where}:{thread.line}"
    header = f"[{index}] {where}"
    if thread.is_outdated:
        header += "  (outdated — the diff has moved since this was written)"
    parts = [header]
    if thread.diff_hunk.strip():
        parts.append(_fenced(clip_hunk(thread.diff_hunk), "diff"))
    for comment in thread.comments:
        if not comment.body:
            continue
        stamp = f" · {comment.created_at}" if comment.created_at else ""
        parts.append(f"{comment.author}{stamp}:\n{clip_body(comment.body)}")
    return "\n\n".join(parts)


def _format_check(check: gh.FailingCheck) -> str:
    """One failing check: its name and URL, plus the log tail when gw has one.

    `CheckRun.label` qualifies the job with its workflow — two workflows can each
    have a `test` job, and the bare name wouldn't say which one broke.
    """
    header = f"{check.run.label} — {check.run.detail.lower()}"
    if check.run.url:
        header += f"\n{check.run.url}"
    if not check.log:
        return f"{header}\n(no log available — open the URL above to read it)"
    return f"{header}\n{_fenced(check.log)}"


def _format_repo_review(entry: RepoReview, *, multi_repo: bool) -> str:
    """One PR's outstanding feedback: identity, threads, review bodies, checks."""
    review = entry.review
    prefix = f"[{entry.project}] " if multi_repo else ""
    parts = [f"{prefix}PR #{review.number} ({review.state}): {review.url}\n{review.title}"]

    if review.threads:
        threads = "\n\n".join(_format_thread(i, t) for i, t in enumerate(review.threads, start=1))
        parts.append(f"Unresolved review threads ({len(review.threads)}):\n\n{threads}")

    if review.summaries:
        rendered = "\n\n".join(
            f"{s.author} · {s.state}{f' · {s.submitted_at}' if s.submitted_at else ''}:\n"
            f"{clip_body(s.body)}"
            for s in review.summaries
        )
        parts.append(f"Review summaries:\n\n{rendered}")

    if review.failing:
        checks = "\n\n".join(_format_check(c) for c in review.failing)
        parts.append(f"Failing checks ({len(review.failing)}):\n\n{checks}")

    if review.is_empty:
        parts.append("(Nothing outstanding on this PR.)")
    return "\n\n".join(parts)


def format_review_block(feed: ReviewFeed) -> str:
    """Render the whole review feed as the `{review_block}` slot of the brief.

    A multi-repo task labels each PR with its project, since the same review
    comment text can otherwise look like it belongs to the wrong checkout.
    """
    multi_repo = len(feed.repos) > 1
    return "\n\n".join(_format_repo_review(entry, multi_repo=multi_repo) for entry in feed.repos)


def _format_focus(lead: str, prompt: str) -> str:
    """Render the optional focus paragraph shared by the non-default briefs.

    In research and address-review mode `--prompt` *narrows* the session rather
    than replacing its trailer, so an absent (or whitespace-only) prompt renders
    nothing at all. `lead` is the sentence introducing it, minus the colon.
    """
    if not prompt:
        return ""
    return f"\n{lead}:\n\n{prompt}"


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


def format_ticket_context(task: Task) -> str:
    """Render the tracking item's description block for the seed prompt.

    Linear tickets contribute their description plus the comment thread; GitHub
    issues contribute their body. A task with neither says so explicitly, so the
    agent knows the thin prompt is the whole brief rather than a truncation.

    Public because `classify` reads the ticket through it too: the classifier
    must see exactly what the agent will be handed, or its advice is about a
    different document.
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
