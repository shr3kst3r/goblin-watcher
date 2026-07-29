"""LLM-generated session descriptions.

Two halves:

* `should_refresh` / `schedule_if_stale` — fast in-process gate that decides
  whether a session is due for a refresh and (if so) spawns a detached
  subprocess so the caller never blocks.
* `apply` — synchronous "do the LLM call + write back to disk" path that the
  spawned subprocess executes.

Any failure (binary missing, non-zero exit, timeout, parse error) is logged
and swallowed; the session's existing `summary` snippet remains the display
fallback.
"""

from __future__ import annotations

import contextlib
import logging
import os
import subprocess
import sys
import textwrap
from datetime import UTC, datetime, timedelta

from goblin_watcher import config, paths, state
from goblin_watcher.agents import get_agent
from goblin_watcher.agents.base import TranscriptSummary
from goblin_watcher.models import Project, SessionRecord, Task

_DESCRIBE_TIMEOUT_SECONDS = 30
_MAX_DESCRIPTION_LEN = 320

_log = logging.getLogger(__name__)


def _now() -> datetime:
    return datetime.now(UTC)


def _settings() -> tuple[int, str, str, int]:
    """Return (ttl_seconds, agent, model, max_transcript_chars).

    Falls back to defaults on bad config.
    """
    try:
        cfg = config.load().defaults
        return (
            int(cfg.description_ttl_seconds),
            str(cfg.description_agent),
            str(cfg.description_model),
            int(cfg.description_max_transcript_chars),
        )
    except Exception:
        return (900, "claude", "claude-haiku-4-5", 80_000)


def _transcript_mtime(session: SessionRecord) -> datetime | None:
    p = session.transcript_path
    if p is None:
        return None
    try:
        return datetime.fromtimestamp(p.stat().st_mtime, tz=UTC)
    except OSError:
        return None


def should_refresh(session: SessionRecord, *, now: datetime | None = None) -> bool:
    """Is `session` due for a description refresh?

    True when either:
      * we've never described it; or
      * `ttl` has elapsed AND the transcript has been modified since the last
        description.

    Returns False when description refresh is disabled via config.
    """
    ttl_seconds, agent_name, _, _ = _settings()
    if agent_name == "off":
        return False
    now = now or _now()
    if session.description_updated_at is None:
        return True
    age = now - session.description_updated_at
    if age < timedelta(seconds=ttl_seconds):
        return False
    mtime = _transcript_mtime(session)
    if mtime is None:
        # No transcript on disk yet (e.g. tmux race) — let the next pass try.
        return False
    return mtime > session.description_updated_at


def schedule_if_stale(project: Project, task: Task, session: SessionRecord) -> bool:
    """Fork a detached `gw _describe` subprocess if `session` is due.

    Returns True if a subprocess was launched, False otherwise. Never raises;
    a launch failure is logged and treated as "no refresh this pass".
    """
    if not should_refresh(session):
        return False
    cmd = [
        sys.executable,
        "-m",
        "goblin_watcher",
        "_describe",
        project.name,
        task.id,
        session.session_id,
    ]
    log_fh: object = subprocess.DEVNULL
    log_path = paths.logs_dir() / "describe.log"
    try:
        log_path.parent.mkdir(parents=True, exist_ok=True)
        log_fh = log_path.open("a")
    except OSError:
        pass
    try:
        subprocess.Popen(
            cmd,
            stdin=subprocess.DEVNULL,
            stdout=log_fh,  # type: ignore[arg-type]
            stderr=log_fh,  # type: ignore[arg-type]
            start_new_session=True,
            close_fds=True,
            env={**os.environ, "GW_DESCRIBE_SUBPROCESS": "1"},
        )
        return True
    except OSError as e:
        _log.debug("description: failed to spawn subprocess: %s", e)
        return False
    finally:
        # Popen dup'd the fd; close ours so we don't hold the file open.
        close = getattr(log_fh, "close", None)
        if callable(close):
            with contextlib.suppress(OSError):
                close()


# ---------------------------------------------------------------------------
# Subprocess-side work.

_PROMPT_FULL_TEMPLATE = """\
You are summarizing an AI coding-agent session for a status dashboard.
Output 1-2 short sentences, roughly 20-40 words total, on a single
line with no internal newlines. No surrounding quotes. No trailing
period. Describe what the session is about — what the user is trying
to accomplish overall and where things currently stand — not the
agent's most recent reply. Avoid filler like "the user is" or
"the session involves".

Initial spawn prompt:
{label}

Full transcript (labeled [user] / [assistant]):

{transcript}
"""


_PROMPT_FALLBACK_TEMPLATE = """\
You are summarizing an AI coding-agent session for a status dashboard.
Output 1-2 short sentences, roughly 20-40 words total, on a single
line with no internal newlines. No surrounding quotes. No trailing
period. Describe what the session is about — what the user is trying
to accomplish overall and where things currently stand — not the
agent's most recent reply. Avoid filler like "the user is" or
"the session involves".

Initial spawn prompt:
{label}

First user message in transcript:
{first_user}

Most recent user messages (oldest first, newest last):
{recent_users}

Most recent agent messages (oldest first, newest last):
{recent_assistants}

Turn count: {turns}
"""


def _format_block(snippets: list[str], fallback: str = "(none)") -> str:
    if not snippets:
        return fallback
    return "\n\n".join(f"- {s}" for s in snippets)


def _clamp_transcript(text: str, max_chars: int) -> str:
    """Trim `text` to fit within `max_chars`, keeping the head and tail.

    The middle is replaced with a `[…]` marker so Claude knows there's a
    gap. Goal: characterize the session — the start has intent, the tail
    has current state, the middle is usually the least informative
    per-character.
    """
    if max_chars <= 0 or len(text) <= max_chars:
        return text
    marker = "\n\n[… transcript truncated for length …]\n\n"
    keep = max_chars - len(marker)
    if keep <= 0:
        return text[:max_chars]
    head_len = keep * 2 // 3
    tail_len = keep - head_len
    return text[:head_len] + marker + text[-tail_len:]


def _build_full_prompt(session: SessionRecord, transcript: str, max_chars: int) -> str:
    clamped = _clamp_transcript(transcript, max_chars)
    return _PROMPT_FULL_TEMPLATE.format(
        label=session.label or "(none)",
        transcript=clamped,
    )


def _build_prompt(session: SessionRecord, parsed: TranscriptSummary | None) -> str:
    """Snippet-based fallback prompt for agents without a full-transcript renderer."""
    first_user = (parsed.first_user_snippet if parsed else None) or session.label or "(none)"
    recent_users = parsed.recent_user_snippets if parsed else []
    recent_assistants = parsed.recent_assistant_snippets if parsed else []
    if not recent_users and session.summary:
        recent_users = [session.summary]
    turn_count = (parsed.turn_count if parsed else 0) or session.turn_count
    return _PROMPT_FALLBACK_TEMPLATE.format(
        label=session.label or "(none)",
        first_user=first_user,
        recent_users=_format_block(recent_users),
        recent_assistants=_format_block(recent_assistants),
        turns=turn_count,
    )


def _run_claude(prompt: str, model: str) -> str | None:
    """Invoke `claude -p --model <model>` and return its stdout, or None on failure."""
    try:
        proc = subprocess.run(
            ["claude", "-p", "--model", model, prompt],
            capture_output=True,
            text=True,
            timeout=_DESCRIBE_TIMEOUT_SECONDS,
            check=False,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired, OSError) as e:
        _log.debug("description: claude invocation failed: %s", e)
        return None
    if proc.returncode != 0:
        _log.debug("description: claude exited %s: %s", proc.returncode, proc.stderr[:200])
        return None
    return proc.stdout


def _run_codex(prompt: str, model: str) -> str | None:
    """Invoke `codex exec --model <model>` and return its stdout, or None on failure."""
    try:
        proc = subprocess.run(
            ["codex", "exec", "--model", model, prompt],
            capture_output=True,
            text=True,
            timeout=_DESCRIBE_TIMEOUT_SECONDS,
            check=False,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired, OSError) as e:
        _log.debug("description: codex invocation failed: %s", e)
        return None
    if proc.returncode != 0:
        _log.debug("description: codex exited %s: %s", proc.returncode, proc.stderr[:200])
        return None
    return proc.stdout


def _clean(raw: str | None) -> str | None:
    """Sanitize the raw LLM output into a single-line description.

    Strategy:
      * Split on blank lines (`\\n\\n`) and keep the last non-empty paragraph
        — agent CLIs occasionally precede the answer with banner / progress
        output, which lives in its own block.
      * Collapse internal whitespace so multi-line answers render on one
        line (the display layer wraps for us).
      * Strip surrounding quotes and a trailing period.
      * Cap length at `_MAX_DESCRIPTION_LEN`.
    """
    if raw is None:
        return None
    text = raw.strip()
    if not text:
        return None
    paragraphs = [p.strip() for p in text.split("\n\n") if p.strip()]
    if not paragraphs:
        return None
    candidate = " ".join(paragraphs[-1].split())
    candidate = candidate.strip('"').strip("'")
    if candidate.endswith("."):
        candidate = candidate[:-1]
    if len(candidate) > _MAX_DESCRIPTION_LEN:
        candidate = candidate[: _MAX_DESCRIPTION_LEN - 1].rstrip() + "…"
    return candidate or None


def _invoke_llm(session: SessionRecord, task: Task) -> str | None:
    """Build the description prompt (full-transcript when available) and call the LLM."""
    _, agent_name, model, max_chars = _settings()
    if agent_name == "off":
        return None
    full = _render_full_transcript(session, task)
    if full:
        prompt = _build_full_prompt(session, full, max_chars)
    else:
        parsed = _read_transcript(session, task)
        prompt = _build_prompt(session, parsed)
    if agent_name == "codex":
        return _clean(_run_codex(prompt, model))
    return _clean(_run_claude(prompt, model))


def _read_transcript(session: SessionRecord, task: Task) -> TranscriptSummary | None:
    """Best-effort transcript read. Returns None when the agent can't parse it."""
    try:
        agent = get_agent(session.agent)
    except Exception as e:
        _log.debug("description: unknown agent %r: %s", session.agent, e)
        return None
    try:
        return agent.read_transcript(session.session_id, task.agent_cwd)
    except Exception as e:
        _log.debug("description: read_transcript failed for %s: %s", session.session_id, e)
        return None


def _render_full_transcript(session: SessionRecord, task: Task) -> str | None:
    """Best-effort full-transcript render. Returns None when unavailable."""
    try:
        agent = get_agent(session.agent)
    except Exception as e:
        _log.debug("description: unknown agent %r: %s", session.agent, e)
        return None
    try:
        return agent.render_transcript(session.session_id, task.agent_cwd)
    except Exception as e:
        _log.debug("description: render_transcript failed for %s: %s", session.session_id, e)
        return None


def apply(project_name: str, task_id: str, session_id: str) -> int:
    """Subprocess entry point: generate + persist a description.

    Returns a process exit code (0 on success or graceful skip, 1 on
    unrecoverable bookkeeping errors). Never raises GoblinError up to the
    caller.
    """
    try:
        project = state.get_project(project_name)
    except Exception as e:
        _log.debug("description: project %r not found: %s", project_name, e)
        return 1
    try:
        task = state.load_task(project, task_id)
    except Exception as e:
        _log.debug("description: task %r/%r not found: %s", project_name, task_id, e)
        return 1
    target = next((s for s in task.sessions if s.session_id == session_id), None)
    if target is None:
        _log.debug("description: session %r not on task %r", session_id, task_id)
        return 0
    # Re-check freshness now that we're actually running: a peer subprocess may
    # have updated `target` between schedule and exec.
    if not should_refresh(target):
        return 0

    new_description = _invoke_llm(target, task)
    if new_description is None:
        # Graceful fail-back: leave the existing snippet `summary` untouched.
        return 0

    # Persist under the task lock, re-reading inside it and patching *only* the
    # description fields on the matching session (ADR 0004). The LLM call above
    # deliberately happens outside the lock — it takes seconds.
    from goblin_watcher import sessions as sessions_mod

    def _patch(latest: Task) -> Task:
        if not sessions_mod.has_session(latest, target.agent, session_id):
            # Session was deleted while we ran; no-op write.
            return latest
        return sessions_mod.patch_session(
            latest,
            target.agent,
            session_id,
            {"description": new_description, "description_updated_at": _now()},
        )

    try:
        state.update_task(project, task_id, _patch)
    except Exception as e:
        _log.debug("description: persist failed for %r: %s", task_id, e)
        return 1
    return 0


def display_text(session: SessionRecord) -> str:
    """Pick the best human-readable line for a session row.

    Priority: LLM description → cheap snippet summary → label → placeholder.
    Internal whitespace is collapsed so single-line consumers (picker rows,
    table cells) don't end up with embedded newlines.
    """
    raw = session.description or session.summary or session.label or "(no summary yet)"
    return " ".join(raw.split())


def wrap_for_tree(text: str, indent_cols: int = 8, width: int = 72) -> str:
    """Wrap `text` with a hanging indent suitable for a Rich Tree node.

    The first wrapped line is un-indented so the caller can prepend an agent
    badge (`claude  …`); subsequent lines are indented by `indent_cols` so
    they align under the description body, not at the tree gutter. Embedded
    `\\n` already in the input is treated as a paragraph break — each
    paragraph wraps independently.
    """
    if not text:
        return ""
    pad = " " * indent_cols
    paragraphs = text.split("\n") if "\n" in text else [text]
    wrapped: list[str] = []
    for i, para in enumerate(paragraphs):
        if not para.strip():
            wrapped.append("")
            continue
        # Only the very first paragraph's first line is un-indented; every
        # subsequent paragraph starts with the hanging indent too.
        initial = "" if i == 0 else pad
        wrapped.append(
            textwrap.fill(
                para,
                width=width,
                initial_indent=initial,
                subsequent_indent=pad,
                break_long_words=False,
                break_on_hyphens=False,
            )
        )
    return "\n".join(wrapped)


def transcript_mtime(session: SessionRecord) -> datetime | None:
    """Exposed for tests."""
    return _transcript_mtime(session)


__all__ = [
    "apply",
    "display_text",
    "schedule_if_stale",
    "should_refresh",
    "transcript_mtime",
    "wrap_for_tree",
]
