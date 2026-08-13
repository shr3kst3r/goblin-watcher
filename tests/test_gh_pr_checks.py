"""`gh.pr_checks` / `gh.pr_check_runs` — gw's CI status. Never calls the real `gh`."""

from __future__ import annotations

import json
import subprocess
from unittest.mock import patch

from goblin_watcher import gh


def _rollup(*checks: dict) -> subprocess.CompletedProcess[str]:
    return subprocess.CompletedProcess(
        args=[],
        returncode=0,
        stdout=json.dumps({"statusCheckRollup": list(checks)}),
        stderr="",
    )


def _run_with(result: subprocess.CompletedProcess[str]):  # type: ignore[no-untyped-def]
    return (
        patch("goblin_watcher.gh.shutil.which", return_value="/usr/bin/gh"),
        patch("goblin_watcher.gh.subprocess.run", return_value=result),
    )


def _check(res: subprocess.CompletedProcess[str]) -> str | None:
    which, run = _run_with(res)
    with which, run:
        return gh.pr_checks("https://gh/pr/1")


def test_all_successful_is_passing() -> None:
    assert (
        _check(
            _rollup(
                {"status": "COMPLETED", "conclusion": "SUCCESS"},
                {"status": "COMPLETED", "conclusion": "SKIPPED"},
            )
        )
        == "passing"
    )


def test_any_failure_is_failing() -> None:
    assert (
        _check(
            _rollup(
                {"status": "COMPLETED", "conclusion": "SUCCESS"},
                {"status": "COMPLETED", "conclusion": "FAILURE"},
            )
        )
        == "failing"
    )


def test_failure_wins_over_pending() -> None:
    assert (
        _check(
            _rollup(
                {"status": "IN_PROGRESS", "conclusion": None},
                {"status": "COMPLETED", "conclusion": "TIMED_OUT"},
            )
        )
        == "failing"
    )


def test_in_progress_is_pending() -> None:
    assert (
        _check(
            _rollup(
                {"status": "COMPLETED", "conclusion": "SUCCESS"},
                {"status": "IN_PROGRESS", "conclusion": None},
            )
        )
        == "pending"
    )


def test_legacy_status_context_state_is_understood() -> None:
    assert _check(_rollup({"state": "FAILURE"})) == "failing"
    assert _check(_rollup({"state": "PENDING"})) == "pending"
    assert _check(_rollup({"state": "SUCCESS"})) == "passing"


def test_no_checks_configured_is_none_not_passing() -> None:
    """A repo without CI must never render as a green tick."""
    assert _check(_rollup()) is None


def test_missing_gh_binary_returns_none() -> None:
    with patch("goblin_watcher.gh.shutil.which", return_value=None):
        assert gh.pr_checks("https://gh/pr/1") is None


def test_gh_failure_returns_none() -> None:
    assert (
        _check(subprocess.CompletedProcess(args=[], returncode=1, stdout="", stderr="boom")) is None
    )


def test_invalid_json_returns_none() -> None:
    assert (
        _check(subprocess.CompletedProcess(args=[], returncode=0, stdout="not json", stderr=""))
        is None
    )


def _runs(res: subprocess.CompletedProcess[str]) -> list[gh.CheckRun] | None:
    which, run = _run_with(res)
    with which, run:
        return gh.pr_check_runs("https://gh/pr/1")


def test_check_runs_keep_name_state_and_url() -> None:
    """The whole point of gh-18: the rollup's detail survives the call."""
    runs = _runs(
        _rollup(
            {
                "__typename": "CheckRun",
                "name": "test",
                "workflowName": "verify",
                "status": "COMPLETED",
                "conclusion": "FAILURE",
                "detailsUrl": "https://github.com/o/r/actions/runs/1",
            },
            {
                "__typename": "StatusContext",
                "context": "ci/azure",
                "state": "SUCCESS",
                "targetUrl": "https://dev.azure.com/build/2",
            },
        )
    )
    assert runs is not None
    assert [(r.label, r.state, r.detail, r.url) for r in runs] == [
        ("verify / test", "failing", "FAILURE", "https://github.com/o/r/actions/runs/1"),
        ("ci/azure", "passing", "SUCCESS", "https://dev.azure.com/build/2"),
    ]


def test_check_run_detail_falls_back_to_status_while_running() -> None:
    """An in-flight check has no conclusion yet — show what it's doing instead."""
    runs = _runs(_rollup({"name": "test", "status": "IN_PROGRESS", "conclusion": None}))
    assert runs is not None
    [run] = runs
    assert (run.state, run.detail, run.url) == ("pending", "IN_PROGRESS", None)


def test_check_run_label_is_bare_name_without_a_workflow() -> None:
    runs = _runs(_rollup({"name": "test", "status": "COMPLETED", "conclusion": "SUCCESS"}))
    assert runs is not None
    assert runs[0].label == "test"


def test_check_runs_and_rollup_agree_on_no_signal() -> None:
    """`pr_check_runs` must return None wherever `pr_checks` does, so callers
    can't render an empty check list as "this PR has no CI"."""
    no_signal = [
        _rollup(),
        subprocess.CompletedProcess(args=[], returncode=1, stdout="", stderr="boom"),
        subprocess.CompletedProcess(args=[], returncode=0, stdout="not json", stderr=""),
    ]
    for res in no_signal:
        assert _check(res) is None
        assert _runs(res) is None
    with patch("goblin_watcher.gh.shutil.which", return_value=None):
        assert gh.pr_check_runs("https://gh/pr/1") is None


def test_unnamed_check_still_gets_a_row() -> None:
    runs = _runs(_rollup({"status": "COMPLETED", "conclusion": "SUCCESS"}))
    assert runs is not None
    assert runs[0].name == "(unnamed check)"
