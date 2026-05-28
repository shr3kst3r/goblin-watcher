"""Thin wrapper around the `gh` CLI for PR operations."""

from __future__ import annotations

import re
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path

from goblin_watcher.errors import GoblinError, MissingDependencyError


def _ensure_gh() -> None:
    if shutil.which("gh") is None:
        raise MissingDependencyError(
            "`gh` is not on PATH.",
            hint="Install the GitHub CLI from https://cli.github.com/.",
        )


def _run(args: list[str], cwd: Path | None = None) -> subprocess.CompletedProcess[str]:
    _ensure_gh()
    return subprocess.run(
        ["gh", *args],
        cwd=str(cwd) if cwd else None,
        capture_output=True,
        text=True,
        check=False,
    )


_PR_URL_RE = re.compile(r"https://github\.com/[^\s]+/pull/\d+")


@dataclass(frozen=True)
class PrInfo:
    """A PR's identity as reported by `gh pr view --json`."""

    number: int
    head_ref: str
    base_ref: str
    url: str
    title: str
    state: str
    is_cross_repository: bool


def pr_view(pr: str, *, cwd: Path) -> PrInfo:
    """Look up a PR by number or URL via `gh pr view`.

    `pr` may be a bare number (resolved against the repo at `cwd`) or a full
    PR URL (which `gh` resolves regardless of `cwd`).
    """
    res = _run(
        [
            "pr",
            "view",
            pr,
            "--json",
            "number,headRefName,baseRefName,url,title,state,isCrossRepository",
        ],
        cwd=cwd,
    )
    if res.returncode != 0:
        raise GoblinError(
            f"No PR {pr!r} found.",
            hint=(res.stderr or res.stdout).strip() or None,
        )
    import json

    try:
        data = json.loads(res.stdout)
    except json.JSONDecodeError as e:
        raise GoblinError(
            f"`gh pr view {pr}` returned output that wasn't valid JSON.",
            hint=res.stdout.strip() or None,
        ) from e
    return PrInfo(
        number=int(data.get("number", 0)),
        head_ref=data.get("headRefName", ""),
        base_ref=data.get("baseRefName", ""),
        url=data.get("url", ""),
        title=data.get("title", ""),
        state=data.get("state", ""),
        is_cross_repository=bool(data.get("isCrossRepository", False)),
    )


def create_pr(
    *,
    cwd: Path,
    title: str,
    body: str,
    base: str,
    head: str | None = None,
    draft: bool = False,
) -> str:
    args = ["pr", "create", "--title", title, "--body", body, "--base", base]
    if head:
        args.extend(["--head", head])
    if draft:
        args.append("--draft")
    res = _run(args, cwd=cwd)
    if res.returncode != 0:
        raise GoblinError(
            "`gh pr create` failed.",
            hint=(res.stderr or res.stdout).strip() or None,
        )
    m = _PR_URL_RE.search(res.stdout)
    if not m:
        raise GoblinError(
            "`gh pr create` succeeded but no PR URL was found in its output.",
            hint=res.stdout.strip() or None,
        )
    return m.group(0)


def list_repo_prs(cwd: Path) -> list[dict[str, str]]:
    """Return PRs newest-first as `[{headRefName, url, state, number}, ...]`.

    Single `gh pr list` call (up to 200 PRs, any state). Returns `[]` silently
    if `gh` is missing or the call fails, so callers can use this as a
    best-effort backfill without crashing.
    """
    if shutil.which("gh") is None:
        return []
    res = subprocess.run(
        [
            "gh",
            "pr",
            "list",
            "--state",
            "all",
            "--json",
            "headRefName,url,state,number",
            "--limit",
            "200",
        ],
        cwd=str(cwd),
        capture_output=True,
        text=True,
        check=False,
    )
    if res.returncode != 0:
        return []
    import json

    try:
        items = json.loads(res.stdout)
    except json.JSONDecodeError:
        return []
    return [
        {
            "headRefName": item.get("headRefName", ""),
            "url": item.get("url", ""),
            "state": item.get("state", ""),
            "number": str(item.get("number", "")),
        }
        for item in items
        if item.get("headRefName")
    ]


def pr_state(url: str) -> str | None:
    """Return the PR state (`OPEN`, `CLOSED`, `MERGED`) or None if `gh` can't read it."""
    if shutil.which("gh") is None:
        return None
    res = _run(["pr", "view", url, "--json", "state"])
    if res.returncode != 0:
        return None
    import json

    try:
        return json.loads(res.stdout).get("state")
    except (json.JSONDecodeError, AttributeError):
        return None


def pr_status(*, cwd: Path) -> dict[str, str]:
    """Return key/value details for the PR associated with the current branch."""
    res = _run(["pr", "view", "--json", "url,state,number,title"], cwd=cwd)
    if res.returncode != 0:
        raise GoblinError(
            "No PR found for the current branch.",
            hint=(res.stderr or res.stdout).strip() or None,
        )
    import json

    data = json.loads(res.stdout)
    return {
        "url": data.get("url", ""),
        "state": data.get("state", ""),
        "number": str(data.get("number", "")),
        "title": data.get("title", ""),
    }
