"""`gh.pr_review` + `gh.check_run_log` — the fetch half of `--address-review`.

Never calls the real `gh`: every test feeds a canned GraphQL payload through a
patched `subprocess.run`.
"""

from __future__ import annotations

import json
import subprocess
from unittest.mock import patch

from goblin_watcher import gh


def _pr_payload(**pr: object) -> subprocess.CompletedProcess[str]:
    """Wrap PR fields in the `data.repository.pullRequest` envelope gh returns."""
    body = {
        "number": 34,
        "title": "Add gw session send",
        "state": "OPEN",
        "url": "https://github.com/org/repo/pull/34",
        "reviewThreads": {"nodes": []},
        "reviews": {"nodes": []},
        "commits": {"nodes": []},
        **pr,
    }
    return subprocess.CompletedProcess(
        args=[],
        returncode=0,
        stdout=json.dumps({"data": {"repository": {"pullRequest": body}}}),
        stderr="",
    )


def _fetch(result: subprocess.CompletedProcess[str]) -> gh.PrReview | None:
    with (
        patch("goblin_watcher.gh.shutil.which", return_value="/usr/bin/gh"),
        patch("goblin_watcher.gh.subprocess.run", return_value=result),
    ):
        return gh.pr_review("org/repo", 34)


def _thread(*, resolved: bool = False, **over: object) -> dict[str, object]:
    return {
        "isResolved": resolved,
        "isOutdated": False,
        "path": "src/goblin_watcher/gh.py",
        "line": 120,
        "comments": {
            "nodes": [
                {
                    "author": {"login": "alice"},
                    "body": "This leaks a file handle.",
                    "createdAt": "2026-08-13T10:00:00Z",
                    "diffHunk": "@@ -1,3 +1,3 @@\n-old\n+new",
                }
            ]
        },
        **over,
    }


def _rollup(*contexts: dict[str, object]) -> dict[str, object]:
    return {
        "nodes": [
            {"commit": {"statusCheckRollup": {"contexts": {"nodes": list(contexts)}}}},
        ]
    }


def test_resolved_threads_are_dropped() -> None:
    review = _fetch(
        _pr_payload(reviewThreads={"nodes": [_thread(), _thread(resolved=True)]}),
    )
    assert review is not None
    assert len(review.threads) == 1
    assert review.threads[0].path == "src/goblin_watcher/gh.py"
    assert review.threads[0].line == 120
    assert review.threads[0].comments[0].author == "alice"
    assert review.threads[0].comments[0].body == "This leaks a file handle."
    assert review.threads[0].diff_hunk.startswith("@@")


def test_thread_with_no_comment_bodies_is_dropped() -> None:
    """An unresolved thread whose comments came back empty carries nothing to
    act on — seeding it would be a blank bullet in the brief."""
    empty = _thread(comments={"nodes": [{"author": {"login": "bot"}, "body": "  "}]})
    review = _fetch(_pr_payload(reviewThreads={"nodes": [empty]}))
    assert review is not None
    assert review.threads == ()


def test_thread_survives_a_null_author_and_missing_anchor() -> None:
    """GitHub nulls the author for a deleted account and the path for a file
    that left the diff; neither may drop the feedback on the floor."""
    orphan = _thread(
        path=None,
        line=None,
        author=None,
        comments={"nodes": [{"author": None, "body": "Still wrong.", "diffHunk": ""}]},
    )
    review = _fetch(_pr_payload(reviewThreads={"nodes": [orphan]}))
    assert review is not None
    assert review.threads[0].path is None
    assert review.threads[0].line is None
    assert review.threads[0].comments[0].author == "unknown"


def test_only_actionable_review_bodies_are_kept() -> None:
    """An APPROVED body is congratulation and a DISMISSED one was overruled —
    neither is feedback to work through."""
    review = _fetch(
        _pr_payload(
            reviews={
                "nodes": [
                    {"author": {"login": "a"}, "state": "CHANGES_REQUESTED", "body": "Two things."},
                    {"author": {"login": "b"}, "state": "COMMENTED", "body": "One nit."},
                    {"author": {"login": "c"}, "state": "APPROVED", "body": "Nice."},
                    {"author": {"login": "d"}, "state": "DISMISSED", "body": "Ignore me."},
                    {"author": {"login": "e"}, "state": "COMMENTED", "body": "   "},
                ]
            }
        )
    )
    assert review is not None
    assert [s.author for s in review.summaries] == ["a", "b"]


def test_failing_checks_cover_both_rollup_node_types() -> None:
    """The rollup mixes CheckRun (name + conclusion) with the legacy
    StatusContext (context + state); a failure in either must land."""
    review = _fetch(
        _pr_payload(
            commits=_rollup(
                {"name": "verify", "conclusion": "SUCCESS", "detailsUrl": "https://x/1"},
                {"name": "build", "conclusion": "FAILURE", "detailsUrl": "https://x/2"},
                {"context": "legacy/ci", "state": "ERROR", "targetUrl": "https://x/3"},
                {"name": "slow", "conclusion": None, "detailsUrl": "https://x/4"},
            )
        )
    )
    assert review is not None
    assert [(c.name, c.details_url) for c in review.failing] == [
        ("build", "https://x/2"),
        ("legacy/ci", "https://x/3"),
    ]


def test_empty_pr_is_not_the_same_as_no_signal() -> None:
    """A PR with nothing outstanding reports an empty `PrReview`; only an
    unreadable one reports None. `--address-review` tells them apart."""
    review = _fetch(_pr_payload())
    assert review is not None
    assert review.is_empty


def test_graphql_error_reads_as_no_signal() -> None:
    """`gh` exits non-zero on a GraphQL error but still prints a body with
    `data: null` — stdout is the signal, not the return code."""
    res = subprocess.CompletedProcess(
        args=[],
        returncode=1,
        stdout=json.dumps({"data": None, "errors": [{"message": "Could not resolve"}]}),
        stderr="",
    )
    assert _fetch(res) is None


def test_unparseable_output_reads_as_no_signal() -> None:
    res = subprocess.CompletedProcess(args=[], returncode=0, stdout="not json", stderr="")
    assert _fetch(res) is None


def test_missing_gh_reads_as_no_signal() -> None:
    with patch("goblin_watcher.gh.shutil.which", return_value=None):
        assert gh.pr_review("org/repo", 34) is None


def test_malformed_repo_slug_is_refused_before_the_call() -> None:
    with (
        patch("goblin_watcher.gh.shutil.which", return_value="/usr/bin/gh"),
        patch("goblin_watcher.gh.subprocess.run") as run,
    ):
        assert gh.pr_review("not-a-slug", 34) is None
    run.assert_not_called()


def test_pr_number_is_passed_as_a_typed_graphql_variable() -> None:
    """`-f` would send the number as a String and the query declares Int!, so
    the whole call would fail server-side."""
    with (
        patch("goblin_watcher.gh.shutil.which", return_value="/usr/bin/gh"),
        patch("goblin_watcher.gh.subprocess.run", return_value=_pr_payload()) as run,
    ):
        gh.pr_review("org/repo", 34)
    args = run.call_args.args[0]
    assert "-F" in args
    assert args[args.index("-F") + 1] == "number=34"


def test_check_run_log_reads_only_the_failed_steps() -> None:
    res = subprocess.CompletedProcess(args=[], returncode=0, stdout="boom\n", stderr="")
    with (
        patch("goblin_watcher.gh.shutil.which", return_value="/usr/bin/gh"),
        patch("goblin_watcher.gh.subprocess.run", return_value=res) as run,
    ):
        log = gh.check_run_log(
            "org/repo", "https://github.com/org/repo/actions/runs/9/job/77?check_suite_focus=true"
        )
    assert log == "boom"
    args = run.call_args.args[0]
    assert args[1:] == ["run", "view", "--repo", "org/repo", "--job", "77", "--log-failed"]


def test_check_run_log_skips_non_actions_urls() -> None:
    """A third-party status context has no Actions job behind it; gw shows the
    URL instead of inventing a log."""
    with (
        patch("goblin_watcher.gh.shutil.which", return_value="/usr/bin/gh"),
        patch("goblin_watcher.gh.subprocess.run") as run,
    ):
        assert gh.check_run_log("org/repo", "https://circleci.com/build/12") is None
        assert gh.check_run_log("org/repo", "") is None
    run.assert_not_called()


def test_check_run_log_swallows_a_failed_lookup() -> None:
    """Expired logs make `gh run view` exit non-zero; that's a missing log, not
    a reason to abandon the whole seed prompt."""
    res = subprocess.CompletedProcess(args=[], returncode=1, stdout="", stderr="expired")
    with (
        patch("goblin_watcher.gh.shutil.which", return_value="/usr/bin/gh"),
        patch("goblin_watcher.gh.subprocess.run", return_value=res),
    ):
        assert gh.check_run_log("org/repo", "https://github.com/o/r/actions/runs/9/job/7") is None
