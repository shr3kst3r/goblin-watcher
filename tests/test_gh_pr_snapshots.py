"""`gh.parse_pr_url` and `gh.pr_snapshots` — the batched PR lookup.

One aliased GraphQL query answers state + CI for a whole repo's worth of PRs at
a single rate-limit point, which is what keeps a sync pass' API cost from
scaling with task count. Never calls the real `gh`.
"""

from __future__ import annotations

import json
import subprocess
from unittest.mock import patch

import pytest

from goblin_watcher import gh


@pytest.mark.parametrize(
    ("url", "expected"),
    [
        ("https://github.com/shr3kst3r/goblin-watcher/pull/2", ("shr3kst3r/goblin-watcher", 2)),
        ("https://github.com/Owner/Repo/pull/17", ("owner/repo", 17)),
        ("https://github.com/o/r/pull/3/files", ("o/r", 3)),
        ("https://github.com/o/r/pull/3#issuecomment-1", ("o/r", 3)),
        ("  https://github.com/o/r/pull/4  ", ("o/r", 4)),
        # Not batchable: another host, an issue, a truncated path.
        ("https://ghe.corp.example/o/r/pull/5", None),
        ("https://github.com/o/r/issues/5", None),
        ("https://github.com/o/r", None),
        ("https://gh/pr/1", None),
        ("nonsense", None),
    ],
)
def test_parse_pr_url(url: str, expected: tuple[str, int] | None) -> None:
    assert gh.parse_pr_url(url) == expected


def _graphql(payload: dict, returncode: int = 0) -> subprocess.CompletedProcess[str]:
    return subprocess.CompletedProcess(
        args=[], returncode=returncode, stdout=json.dumps(payload), stderr=""
    )


def _snapshots(
    result: subprocess.CompletedProcess[str], numbers: list[int]
) -> dict[int, gh.PrSnapshot]:
    with (
        patch("goblin_watcher.gh.shutil.which", return_value="/usr/bin/gh"),
        patch("goblin_watcher.gh.subprocess.run", return_value=result),
    ):
        return gh.pr_snapshots("o/r", numbers)


def test_rollup_states_map_onto_gws_vocabulary() -> None:
    got = _snapshots(
        _graphql(
            {
                "data": {
                    "repository": {
                        "p1": {"state": "OPEN", "statusCheckRollup": {"state": "SUCCESS"}},
                        "p2": {"state": "OPEN", "statusCheckRollup": {"state": "FAILURE"}},
                        "p3": {"state": "OPEN", "statusCheckRollup": {"state": "ERROR"}},
                        "p4": {"state": "OPEN", "statusCheckRollup": {"state": "PENDING"}},
                        "p5": {"state": "MERGED", "statusCheckRollup": {"state": "EXPECTED"}},
                    }
                }
            }
        ),
        [1, 2, 3, 4, 5],
    )
    assert got[1] == gh.PrSnapshot(state="OPEN", checks="passing")
    assert got[2] == gh.PrSnapshot(state="OPEN", checks="failing")
    assert got[3] == gh.PrSnapshot(state="OPEN", checks="failing")
    assert got[4] == gh.PrSnapshot(state="OPEN", checks="pending")
    assert got[5] == gh.PrSnapshot(state="MERGED", checks="pending")


def test_no_checks_configured_is_none_not_passing() -> None:
    """Same contract as `pr_checks`: a repo without CI never renders a green tick."""
    got = _snapshots(
        _graphql({"data": {"repository": {"p1": {"state": "OPEN", "statusCheckRollup": None}}}}),
        [1],
    )
    assert got[1] == gh.PrSnapshot(state="OPEN", checks=None)


def test_partial_errors_keep_the_surviving_aliases() -> None:
    """A deleted PR makes `gh` exit non-zero while still returning the rest."""
    got = _snapshots(
        _graphql(
            {
                "data": {
                    "repository": {
                        "p1": {"state": "OPEN", "statusCheckRollup": {"state": "SUCCESS"}},
                        "p2": None,
                    }
                },
                "errors": [{"type": "NOT_FOUND", "path": ["repository", "p2"]}],
            },
            returncode=1,
        ),
        [1, 2],
    )
    assert got[1].state == "OPEN"
    assert 2 not in got, "an unresolvable PR must be absent, not an empty snapshot"


def test_missing_gh_binary_returns_empty() -> None:
    with patch("goblin_watcher.gh.shutil.which", return_value=None):
        assert gh.pr_snapshots("o/r", [1]) == {}


def test_no_numbers_makes_no_call() -> None:
    with patch("goblin_watcher.gh.subprocess.run") as run:
        assert gh.pr_snapshots("o/r", []) == {}
    run.assert_not_called()


def test_malformed_repo_makes_no_call() -> None:
    with (
        patch("goblin_watcher.gh.shutil.which", return_value="/usr/bin/gh"),
        patch("goblin_watcher.gh.subprocess.run") as run,
    ):
        assert gh.pr_snapshots("not-a-repo", [1]) == {}
    run.assert_not_called()


def test_invalid_json_returns_empty() -> None:
    assert (
        _snapshots(
            subprocess.CompletedProcess(args=[], returncode=0, stdout="not json", stderr=""), [1]
        )
        == {}
    )


def test_one_query_covers_every_pr() -> None:
    """The whole point: N PRs cost one call, not 2N."""
    result = _graphql(
        {
            "data": {
                "repository": {
                    f"p{n}": {"state": "OPEN", "statusCheckRollup": {"state": "SUCCESS"}}
                    for n in range(1, 26)
                }
            }
        }
    )
    with (
        patch("goblin_watcher.gh.shutil.which", return_value="/usr/bin/gh"),
        patch("goblin_watcher.gh.subprocess.run", return_value=result) as run,
    ):
        got = gh.pr_snapshots("o/r", list(range(1, 26)))
    assert len(got) == 25
    assert run.call_count == 1
    # owner/name travel as GraphQL variables so they are never interpolated.
    argv = run.call_args.args[0]
    assert "owner=o" in argv and "name=r" in argv
    query = next(a for a in argv if a.startswith("query="))
    assert "p25: pullRequest(number: 25)" in query


def test_large_repos_are_chunked_not_truncated() -> None:
    with (
        patch("goblin_watcher.gh.shutil.which", return_value="/usr/bin/gh"),
        patch(
            "goblin_watcher.gh.subprocess.run", return_value=_graphql({"data": {"repository": {}}})
        ) as run,
    ):
        gh.pr_snapshots("o/r", list(range(1, 251)))
    assert run.call_count == 3, "250 PRs should split into 3 queries of at most 100"
