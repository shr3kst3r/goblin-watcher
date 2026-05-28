from pathlib import Path
from unittest.mock import patch

import pytest

from goblin_watcher import gh
from goblin_watcher.errors import GoblinError, MissingDependencyError


def test_create_pr_returns_url_on_success(tmp_path: Path) -> None:
    class _FakeRes:
        returncode = 0
        stdout = "https://github.com/org/repo/pull/42\n"
        stderr = ""

    with (
        patch("goblin_watcher.gh.shutil.which", return_value="/usr/bin/gh"),
        patch("goblin_watcher.gh.subprocess.run", return_value=_FakeRes()),
    ):
        url = gh.create_pr(
            cwd=tmp_path,
            title="t",
            body="b",
            base="main",
            head="feat/x",
            draft=False,
        )
    assert url == "https://github.com/org/repo/pull/42"


def test_create_pr_missing_gh_errors(tmp_path: Path) -> None:
    with (
        patch("goblin_watcher.gh.shutil.which", return_value=None),
        pytest.raises(MissingDependencyError),
    ):
        gh.create_pr(cwd=tmp_path, title="t", body="b", base="main")


def test_create_pr_nonzero_raises(tmp_path: Path) -> None:
    class _FakeRes:
        returncode = 1
        stdout = ""
        stderr = "no permission"

    with (
        patch("goblin_watcher.gh.shutil.which", return_value="/usr/bin/gh"),
        patch("goblin_watcher.gh.subprocess.run", return_value=_FakeRes()),
        pytest.raises(GoblinError, match="gh pr create"),
    ):
        gh.create_pr(cwd=tmp_path, title="t", body="b", base="main")
