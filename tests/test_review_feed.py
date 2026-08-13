"""`review_feed.collect` — resolving a task's PRs and bounding what they contribute."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from unittest.mock import patch

import pytest

from goblin_watcher import gh, review_feed
from goblin_watcher.errors import GoblinError
from goblin_watcher.models import Task, TaskRepo


def _task(tmp_path: Path, **over: object) -> Task:
    return Task(
        id="gh-17",
        project="alpha",
        branch="gh-17-address-review",
        worktree_path=tmp_path / "wt",
        base_branch="main",
        created_at=datetime(2026, 8, 13, tzinfo=UTC),
        **over,
    )


def _review(**over: object) -> gh.PrReview:
    defaults: dict[str, object] = {
        "number": 34,
        "title": "Add --address-review",
        "state": "OPEN",
        "url": "https://github.com/org/repo/pull/34",
        "threads": (),
        "summaries": (),
        "failing": (),
    }
    return gh.PrReview(**{**defaults, **over})  # type: ignore[arg-type]


def _thread(body: str = "This leaks a handle.") -> gh.ReviewThread:
    return gh.ReviewThread(
        path="src/gh.py",
        line=10,
        is_outdated=False,
        diff_hunk="@@ -1 +1 @@",
        comments=(gh.ReviewComment(author="alice", body=body, created_at="2026-08-13T10:00:00Z"),),
    )


def _check(name: str = "verify", url: str = "https://github.com/o/r/actions/runs/1/job/2"):  # type: ignore[no-untyped-def]
    return gh.FailingCheck(name=name, conclusion="FAILURE", details_url=url)


def test_scratch_tasks_are_refused(tmp_path: Path) -> None:
    task = _task(tmp_path, kind="scratch")
    with pytest.raises(GoblinError, match="scratch space"):
        review_feed.collect(task)


def test_a_task_with_no_pr_is_refused(tmp_path: Path) -> None:
    with (
        patch("goblin_watcher.gh.pr_for_branch", return_value=None),
        pytest.raises(GoblinError, match="no pull request"),
    ):
        review_feed.collect(_task(tmp_path))


def test_pr_url_is_looked_up_when_the_record_has_none(tmp_path: Path) -> None:
    """`pr_url` is only written by `gw pr open`/`gw pr status`/sync, so a PR
    opened by hand is invisible on the record until something backfills it."""
    with (
        patch(
            "goblin_watcher.gh.pr_for_branch",
            return_value={"url": "https://github.com/org/repo/pull/34", "state": "OPEN"},
        ),
        patch("goblin_watcher.gh.pr_review", return_value=_review(threads=(_thread(),))) as fetch,
        patch("goblin_watcher.gh.check_run_log", return_value=None),
    ):
        feed = review_feed.collect(_task(tmp_path))
    fetch.assert_called_once_with("org/repo", 34)
    assert len(feed.repos) == 1


def test_a_clean_pr_is_refused_rather_than_seeded(tmp_path: Path) -> None:
    """Nothing outstanding means the brief would be about nothing; gw refuses
    loudly instead of spawning a no-op session."""
    task = _task(tmp_path, pr_url="https://github.com/org/repo/pull/34")
    with (
        patch("goblin_watcher.gh.pr_review", return_value=_review()),
        pytest.raises(GoblinError, match="Nothing to address"),
    ):
        review_feed.collect(task)


def test_an_unreadable_pr_is_refused_and_names_what_it_tried(tmp_path: Path) -> None:
    task = _task(tmp_path, pr_url="https://github.com/org/repo/pull/34")
    with (
        patch("goblin_watcher.gh.pr_review", return_value=None),
        pytest.raises(GoblinError, match="Couldn't read review feedback") as excinfo,
    ):
        review_feed.collect(task)
    assert "https://github.com/org/repo/pull/34" in (excinfo.value.hint or "")


def test_a_non_github_pr_url_is_not_batchable(tmp_path: Path) -> None:
    """`parse_pr_url` only matches github.com; an enterprise host reads as
    unreadable rather than being handed to the API as a guess."""
    task = _task(tmp_path, pr_url="https://ghe.corp/org/repo/pull/34")
    with (
        patch("goblin_watcher.gh.pr_review") as fetch,
        pytest.raises(GoblinError, match="Couldn't read review feedback"),
    ):
        review_feed.collect(task)
    fetch.assert_not_called()


def test_failing_check_logs_are_attached_and_tail_clipped(tmp_path: Path) -> None:
    task = _task(tmp_path, pr_url="https://github.com/org/repo/pull/34")
    long_log = "\n".join(f"line {i}" for i in range(review_feed.MAX_LOG_LINES + 50))
    with (
        patch("goblin_watcher.gh.pr_review", return_value=_review(failing=(_check(),))),
        patch("goblin_watcher.gh.check_run_log", return_value=long_log),
    ):
        feed = review_feed.collect(task)
    [entry] = feed.repos
    log = entry.review.failing[0].log
    assert log.startswith("[…earlier output trimmed…]")
    # The tail is what matters: a job's error is at the end of its output.
    assert log.strip().endswith(f"line {review_feed.MAX_LOG_LINES + 49}")
    assert "line 0" not in log


def test_a_check_with_no_fetchable_log_keeps_its_url(tmp_path: Path) -> None:
    task = _task(tmp_path, pr_url="https://github.com/org/repo/pull/34")
    with (
        patch("goblin_watcher.gh.pr_review", return_value=_review(failing=(_check(),))),
        patch("goblin_watcher.gh.check_run_log", return_value=None),
    ):
        feed = review_feed.collect(task)
    check = feed.repos[0].review.failing[0]
    assert check.log == ""
    assert check.details_url.endswith("/job/2")


def test_log_fetching_is_capped_but_the_checks_are_not(tmp_path: Path) -> None:
    """A wide failing matrix would otherwise cost a round-trip per shard before
    the agent even starts. Checks past the cap lose the log, not the entry."""
    task = _task(tmp_path, pr_url="https://github.com/org/repo/pull/34")
    checks = tuple(_check(name=f"verify-{i}") for i in range(review_feed.MAX_LOGGED_CHECKS + 3))
    with (
        patch("goblin_watcher.gh.pr_review", return_value=_review(failing=checks)),
        patch("goblin_watcher.gh.check_run_log", return_value="boom") as fetch,
    ):
        feed = review_feed.collect(task)
    assert fetch.call_count == review_feed.MAX_LOGGED_CHECKS
    failing = feed.repos[0].review.failing
    assert len(failing) == len(checks)
    assert failing[-1].log == ""


def test_multi_repo_tasks_collect_every_pr(tmp_path: Path) -> None:
    task = _task(
        tmp_path,
        pr_url="https://github.com/org/repo/pull/34",
        secondary_repos=[
            TaskRepo(
                project="beta",
                branch="gh-17-address-review",
                worktree_path=tmp_path / "beta",
                base_branch="main",
                pr_url="https://github.com/org/other/pull/7",
            )
        ],
        workspace_path=tmp_path / "ws",
    )
    with (
        patch("goblin_watcher.gh.pr_review", return_value=_review(threads=(_thread(),))),
        patch("goblin_watcher.gh.check_run_log", return_value=None),
    ):
        feed = review_feed.collect(task)
    assert [entry.project for entry in feed.repos] == ["alpha", "beta"]


def test_one_readable_pr_is_enough_for_a_multi_repo_task(tmp_path: Path) -> None:
    """A sibling repo without a PR yet shouldn't block addressing review on the
    one that has it."""
    task = _task(
        tmp_path,
        pr_url="https://github.com/org/repo/pull/34",
        secondary_repos=[
            TaskRepo(
                project="beta",
                branch="gh-17-address-review",
                worktree_path=tmp_path / "beta",
                base_branch="main",
            )
        ],
        workspace_path=tmp_path / "ws",
    )
    with (
        patch("goblin_watcher.gh.pr_for_branch", return_value=None),
        patch("goblin_watcher.gh.pr_review", return_value=_review(threads=(_thread(),))),
        patch("goblin_watcher.gh.check_run_log", return_value=None),
    ):
        feed = review_feed.collect(task)
    assert [entry.project for entry in feed.repos] == ["alpha"]


def test_clip_body_keeps_the_head() -> None:
    """A reviewer's point leads; the tail is elaboration."""
    body = "P" + "x" * (review_feed.MAX_COMMENT_CHARS * 2)
    clipped = review_feed.clip_body(body)
    assert clipped.startswith("Pxxx")
    assert clipped.endswith("[…truncated…]")
    assert len(clipped) < len(body)
    # Short bodies pass through byte-identical.
    assert review_feed.clip_body("short") == "short"


def test_clip_hunk_keeps_the_header_and_the_tail() -> None:
    """The `@@` header carries the line numbers; the tail is where the comment
    anchors. Dropping either loses the thread's location."""
    hunk = "\n".join(["@@ -1,80 +1,80 @@", *[f"  line {i}" for i in range(80)]])
    clipped = review_feed.clip_hunk(hunk)
    assert clipped.startswith("@@ -1,80 +1,80 @@")
    assert clipped.splitlines()[1] == "…"
    assert clipped.strip().endswith("line 79")
    assert len(clipped.splitlines()) <= review_feed.MAX_HUNK_LINES


def test_clean_log_strips_stamps_bom_and_colour() -> None:
    """`gh run view --log-failed` stamps job/step/timestamp on every line, opens
    with a BOM, and keeps the runner's ANSI colour. None of it is output, and at
    200 lines it outweighs what is."""
    raw = (
        "cargo test (linux)\tRun tests\t﻿2026-08-13T14:29:42.3890991Z ##[group]Run cargo\n"
        "cargo test\tRun tests\t2026-08-13T14:31:22.4922245Z     \x1b[31;1mFAILED\x1b[0m x\n"
        "not a stamped line\n"
    )
    assert review_feed.clean_log(raw) == ("##[group]Run cargo\n    FAILED x\nnot a stamped line")


def test_stripping_happens_before_the_tail_is_taken(tmp_path: Path) -> None:
    """Otherwise the budget is spent on scaffolding and the clip drops real
    output that would have fitted."""
    task = _task(tmp_path, pr_url="https://github.com/org/repo/pull/34")
    stamped = "\n".join(
        f"verify\tRun tests\t2026-08-13T14:29:{i:02d}.000000Z line {i}"
        for i in range(review_feed.MAX_LOG_LINES + 10)
    )
    with (
        patch("goblin_watcher.gh.pr_review", return_value=_review(failing=(_check(),))),
        patch("goblin_watcher.gh.check_run_log", return_value=stamped),
    ):
        feed = review_feed.collect(task)
    log = feed.repos[0].review.failing[0].log
    assert "\tRun tests\t" not in log
    assert log.strip().endswith(f"line {review_feed.MAX_LOG_LINES + 9}")
