"""Tests for the GitHub-issue side of the `gh` wrapper."""

from pathlib import Path
from unittest.mock import patch

import pytest

from goblin_watcher import gh
from goblin_watcher.errors import GoblinError

_ISSUE_JSON = (
    '{"number": 42, "title": "Add rate limit", "body": "We need a token bucket.", '
    '"url": "https://github.com/org/repo/issues/42", "state": "OPEN", '
    '"labels": [{"name": "bug"}, {"name": "p1"}], "assignees": [{"login": "alice"}]}'
)


class _Res:
    def __init__(self, stdout: str = "", returncode: int = 0, stderr: str = "") -> None:
        self.stdout = stdout
        self.returncode = returncode
        self.stderr = stderr


@pytest.mark.parametrize(
    ("ref", "expected"),
    [
        ("42", (None, 42)),
        ("#42", (None, 42)),
        ("  42 ", (None, 42)),
        ("org/repo#42", ("org/repo", 42)),
        ("Org/Repo#42", ("org/repo", 42)),
        ("https://github.com/org/repo/issues/42", ("org/repo", 42)),
        ("https://github.com/org/repo/issues/42/", ("org/repo", 42)),
    ],
)
def test_parse_issue_ref_accepts_every_form(ref: str, expected: tuple[str | None, int]) -> None:
    parsed = gh.parse_issue_ref(ref)
    assert (parsed.repo, parsed.number) == expected


@pytest.mark.parametrize(
    "ref",
    [
        "",
        "not-a-ref",
        "org/repo",
        "ENG-123",
        "https://github.com/org/repo/pull/42",
    ],
)
def test_parse_issue_ref_rejects_non_issues(ref: str) -> None:
    with pytest.raises(GoblinError, match="not a GitHub issue reference"):
        gh.parse_issue_ref(ref)


@pytest.mark.parametrize(
    ("url", "expected"),
    [
        ("https://github.com/Org/Repo.git", "org/repo"),
        ("https://github.com/org/repo", "org/repo"),
        ("https://github.com/org/repo/", "org/repo"),
        ("git@github.com:org/repo.git", "org/repo"),
        ("/local/path/to/repo", None),
        (None, None),
    ],
)
def test_normalize_repo(url: str | None, expected: str | None) -> None:
    assert gh.normalize_repo(url) == expected


def test_issue_view_parses_json(tmp_path: Path) -> None:
    with (
        patch("goblin_watcher.gh.shutil.which", return_value="/usr/bin/gh"),
        patch("goblin_watcher.gh.subprocess.run", return_value=_Res(_ISSUE_JSON)) as run,
    ):
        info = gh.issue_view(gh.IssueRef(repo=None, number=42), cwd=tmp_path)
    assert info.number == 42
    # The repo comes back off the URL `gh` reported, not from the input.
    assert info.repo == "org/repo"
    assert info.title == "Add rate limit"
    assert info.body == "We need a token bucket."
    assert info.state == "OPEN"
    assert info.labels == ("bug", "p1")
    assert info.assignees == ("alice",)
    # A bare number resolves against `cwd`, so no --repo is passed.
    assert "--repo" not in run.call_args.args[0]


def test_issue_view_qualified_ref_passes_repo_flag(tmp_path: Path) -> None:
    with (
        patch("goblin_watcher.gh.shutil.which", return_value="/usr/bin/gh"),
        patch("goblin_watcher.gh.subprocess.run", return_value=_Res(_ISSUE_JSON)) as run,
    ):
        gh.issue_view(gh.IssueRef(repo="org/repo", number=42), cwd=tmp_path)
    argv = run.call_args.args[0]
    assert argv[:3] == ["gh", "issue", "view"]
    assert "--repo" in argv and argv[argv.index("--repo") + 1] == "org/repo"


def test_issue_view_missing_body_becomes_empty_string(tmp_path: Path) -> None:
    payload = (
        '{"number": 7, "title": "T", "body": null, '
        '"url": "https://github.com/o/r/issues/7", "state": "CLOSED"}'
    )
    with (
        patch("goblin_watcher.gh.shutil.which", return_value="/usr/bin/gh"),
        patch("goblin_watcher.gh.subprocess.run", return_value=_Res(payload)),
    ):
        info = gh.issue_view(gh.IssueRef(repo=None, number=7), cwd=tmp_path)
    assert info.body == ""
    assert info.labels == ()
    assert info.assignees == ()
    assert info.state == "CLOSED"


def test_issue_view_nonzero_raises(tmp_path: Path) -> None:
    with (
        patch("goblin_watcher.gh.shutil.which", return_value="/usr/bin/gh"),
        patch(
            "goblin_watcher.gh.subprocess.run",
            return_value=_Res(returncode=1, stderr="could not resolve to an Issue"),
        ),
        pytest.raises(GoblinError, match="No GitHub issue org/repo#9 found"),
    ):
        gh.issue_view(gh.IssueRef(repo="org/repo", number=9), cwd=tmp_path)


def test_issue_view_invalid_json_raises(tmp_path: Path) -> None:
    with (
        patch("goblin_watcher.gh.shutil.which", return_value="/usr/bin/gh"),
        patch("goblin_watcher.gh.subprocess.run", return_value=_Res("not json")),
        pytest.raises(GoblinError, match="valid JSON"),
    ):
        gh.issue_view(gh.IssueRef(repo=None, number=1), cwd=tmp_path)


def test_issue_state_returns_state() -> None:
    with (
        patch("goblin_watcher.gh.shutil.which", return_value="/usr/bin/gh"),
        patch("goblin_watcher.gh.subprocess.run", return_value=_Res('{"state": "CLOSED"}')),
    ):
        assert gh.issue_state("org/repo", 42) == "CLOSED"


def test_issue_state_none_on_failure_or_missing_gh() -> None:
    with (
        patch("goblin_watcher.gh.shutil.which", return_value="/usr/bin/gh"),
        patch("goblin_watcher.gh.subprocess.run", return_value=_Res(returncode=1)),
    ):
        assert gh.issue_state("org/repo", 42) is None
    with patch("goblin_watcher.gh.shutil.which", return_value=None):
        assert gh.issue_state("org/repo", 42) is None
