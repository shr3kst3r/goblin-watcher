"""Collect a task's outstanding PR review feedback for `--address-review`.

`gw run --address-review` seeds a session with what a reviewer actually said and
what CI actually printed, so the agent starts from the feedback instead of from
an instruction to go find it (ADR 0008). This module is the gathering half: it
resolves each of the task's repos to a PR, pulls the unresolved threads and the
failing checks' logs, and bounds how much of that reaches the prompt. Rendering
lives in `agents/launcher`, next to the template it fills.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from goblin_watcher import gh
from goblin_watcher.errors import GoblinError
from goblin_watcher.models import Task, TaskRepo

# Bounds on what one seed prompt may carry. A seed prompt competes with the
# agent's own context for the whole session, so CI logs — the one input with no
# natural ceiling — are tail-clipped hard. The tail is what matters: a failing
# job's error is at the end, and the agent can open the run URL for the rest.
MAX_LOG_LINES = 200
MAX_LOG_CHARS = 8000
MAX_LOGGED_CHECKS = 5
MAX_COMMENT_CHARS = 4000
MAX_HUNK_LINES = 20

_TRIMMED_HEAD = "[…earlier output trimmed…]"
_TRIMMED_TAIL = "[…truncated…]"

# `gh run view --log-failed` stamps every line with `<job>\t<step>\t<ISO time> `.
# Repeated on each of a few hundred lines that is more prefix than output, and
# the job and step names are already in the check's header — so it is stripped
# before the tail is taken, and the budget buys log instead of scaffolding.
# The `﻿?` is not defensive padding: `gh` emits a BOM immediately after the
# step tab on the run's first line, and without it that line keeps its stamp.
_LOG_LINE_PREFIX_RE = re.compile(r"^[^\t]*\t[^\t]*\t﻿?\d{4}-\d{2}-\d{2}T[\d:.]+Z ?")

# Test runners colour their output, and CI keeps the escapes because it reports
# a TTY. In a seed prompt they are pure cost — invisible to the reader, and on a
# heavily-coloured line they outweigh the text they wrap.
_ANSI_RE = re.compile(r"\x1b\[[0-9;?]*[ -/]*[@-~]")


@dataclass(frozen=True)
class RepoReview:
    """One repo's PR feedback. `project` names it for a multi-repo task."""

    project: str
    review: gh.PrReview


@dataclass(frozen=True)
class ReviewFeed:
    """Every PR on a task that has something outstanding, primary repo first."""

    repos: tuple[RepoReview, ...]

    @property
    def is_empty(self) -> bool:
        return all(r.review.is_empty for r in self.repos)


def _pr_url(repo: TaskRepo) -> str | None:
    """The repo's PR URL, from the task record or looked up by head branch.

    `pr_url` is only populated by `gw pr open` / `gw pr status` / a sync pass, so
    a PR opened by hand (or by `gw new --pr`) is invisible on the record. Falling
    back to a lookup is what makes `--address-review` work on those.
    """
    if repo.pr_url:
        return repo.pr_url
    found = gh.pr_for_branch(repo.worktree_path, repo.branch)
    if found and found.get("url"):
        return found["url"]
    return None


def clean_log(text: str) -> str:
    """Strip everything from a CI log that isn't the output itself.

    Per-line job/step/timestamp stamps, the BOM `gh` opens with, and ANSI colour
    escapes. Runs before the tail is taken, so the clip budget is spent on log
    rather than on scaffolding.
    """
    stripped = _ANSI_RE.sub("", text)
    return "\n".join(_LOG_LINE_PREFIX_RE.sub("", line).lstrip("﻿") for line in stripped.splitlines())


def _tail(text: str, *, max_lines: int, max_chars: int) -> str:
    """Clip `text` to its last `max_lines` lines and `max_chars` characters."""
    lines = text.splitlines()
    trimmed = len(lines) > max_lines
    out = "\n".join(lines[-max_lines:])
    if len(out) > max_chars:
        out = out[-max_chars:]
        trimmed = True
    return f"{_TRIMMED_HEAD}\n{out}" if trimmed else out


def clip_body(text: str) -> str:
    """Clip a comment body from the end — a reviewer's point leads, it doesn't trail."""
    if len(text) <= MAX_COMMENT_CHARS:
        return text
    return f"{text[:MAX_COMMENT_CHARS].rstrip()}\n{_TRIMMED_TAIL}"


def clip_hunk(hunk: str) -> str:
    """Clip a diff hunk to its last lines, keeping the `@@` header.

    GitHub's `diffHunk` ends at the line the comment anchors to, so the tail is
    the relevant part — but dropping the header would lose the line numbers that
    say *where* in the file this is.
    """
    lines = hunk.splitlines()
    if len(lines) <= MAX_HUNK_LINES:
        return hunk
    return "\n".join([lines[0], "…", *lines[-(MAX_HUNK_LINES - 2) :]])


def _with_logs(review: gh.PrReview, repo_slug: str) -> gh.PrReview:
    """Attach each failing check's log tail, for the first `MAX_LOGGED_CHECKS`.

    Every log is a separate `gh run view` call, so a PR with a wide failing
    matrix would otherwise spend a round-trip per shard before the agent even
    starts. Checks past the cap keep their URL and lose only the inline log.
    """
    enriched: list[gh.FailingCheck] = []
    for index, check in enumerate(review.failing):
        if index >= MAX_LOGGED_CHECKS:
            enriched.append(check)
            continue
        log = gh.check_run_log(repo_slug, check.run.url or "")
        clipped = (
            _tail(clean_log(log), max_lines=MAX_LOG_LINES, max_chars=MAX_LOG_CHARS) if log else ""
        )
        enriched.append(gh.FailingCheck(run=check.run, log=clipped))
    return gh.PrReview(
        number=review.number,
        title=review.title,
        state=review.state,
        url=review.url,
        threads=review.threads,
        summaries=review.summaries,
        failing=tuple(enriched),
    )


def collect(task: Task) -> ReviewFeed:
    """Gather outstanding review feedback across every repo on `task`.

    Raises `GoblinError` when there is nothing to address — no PR, no readable
    PR, or a PR with every thread resolved and every check green. Seeding a
    session in any of those cases would hand the agent a brief about nothing,
    and gw's house style is to refuse loudly rather than spawn a no-op.
    """
    if task.kind == "scratch":
        raise GoblinError(
            f"Task {task.id!r} is a scratch space — there's no PR to address review on.",
        )

    repos = task.all_repos()
    urls: list[tuple[TaskRepo, str]] = [
        (repo, url) for repo in repos if (url := _pr_url(repo)) is not None
    ]
    if not urls:
        raise GoblinError(
            f"Task {task.id!r} has no pull request to address review on.",
            hint="Open one with `gw pr open` first.",
        )

    collected: list[RepoReview] = []
    unreadable: list[str] = []
    for repo, url in urls:
        parsed = gh.parse_pr_url(url)
        if parsed is None:
            unreadable.append(url)
            continue
        repo_slug, number = parsed
        review = gh.pr_review(repo_slug, number)
        if review is None:
            unreadable.append(url)
            continue
        collected.append(RepoReview(project=repo.project, review=_with_logs(review, repo_slug)))

    if not collected:
        raise GoblinError(
            f"Couldn't read review feedback for task {task.id!r}.",
            hint=(
                "Checked: " + ", ".join(unreadable) + ". Is `gh` authenticated (`gh auth status`)?"
            ),
        )

    feed = ReviewFeed(repos=tuple(collected))
    if feed.is_empty:
        raise GoblinError(
            f"Nothing to address on task {task.id!r}: no unresolved review "
            "comments and no failing checks.",
            hint="Checks that are still running don't count as failing — "
            "try again once they finish.",
        )
    return feed
