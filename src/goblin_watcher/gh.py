"""Thin wrapper around the `gh` CLI for PR and issue operations."""

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

_HTTPS_REMOTE_RE = re.compile(r"https://github\.com/(?P<repo>[^/\s]+/[^/\s]+?)(?:\.git)?/?$")
_SSH_REMOTE_RE = re.compile(r"git@github\.com:(?P<repo>.+)")
_ISSUE_URL_RE = re.compile(
    r"https://github\.com/(?P<repo>[^/\s]+/[^/\s]+)/issues/(?P<number>\d+)/?$"
)
# `owner/repo#42` — the cross-repo shorthand `gh` itself accepts.
_ISSUE_QUALIFIED_RE = re.compile(r"^(?P<repo>[^/\s#]+/[^/\s#]+)#(?P<number>\d+)$")
_ISSUE_NUMBER_RE = re.compile(r"^#?(?P<number>\d+)$")


def normalize_repo(url: str | None) -> str | None:
    """Reduce a git remote URL to its lowercased `owner/repo`, else None.

    Handles `https://github.com/owner/repo(.git)` and
    `git@github.com:owner/repo.git`.
    """
    if not url:
        return None
    url = url.strip()
    https = _HTTPS_REMOTE_RE.match(url)
    if https is not None:
        return https.group("repo").lower()
    ssh = _SSH_REMOTE_RE.match(url)
    if ssh is not None:
        return ssh.group("repo").removesuffix(".git").lower()
    return None


@dataclass(frozen=True)
class IssueRef:
    """A parsed `--issue` argument.

    `repo` is None for the bare-number form (`42`), which only resolves against
    a repository the caller supplies; it is `owner/repo` for the URL and
    `owner/repo#42` forms, which name their repository explicitly.
    """

    repo: str | None
    number: int


def parse_issue_ref(ref: str) -> IssueRef:
    """Parse `42`, `#42`, `owner/repo#42`, or a full issue URL into an `IssueRef`."""
    text = ref.strip()
    url = _ISSUE_URL_RE.match(text)
    if url is not None:
        return IssueRef(
            repo=url.group("repo").removesuffix(".git").lower(), number=int(url["number"])
        )
    qualified = _ISSUE_QUALIFIED_RE.match(text)
    if qualified is not None:
        return IssueRef(repo=qualified.group("repo").lower(), number=int(qualified["number"]))
    bare = _ISSUE_NUMBER_RE.match(text)
    if bare is not None:
        return IssueRef(repo=None, number=int(bare["number"]))
    raise GoblinError(
        f"{ref!r} is not a GitHub issue reference.",
        hint="Pass a number (42), owner/repo#42, or an issue URL.",
    )


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


@dataclass(frozen=True)
class IssueInfo:
    """A GitHub issue as reported by `gh issue view --json`."""

    number: int
    repo: str
    title: str
    body: str
    url: str
    state: str
    labels: tuple[str, ...]
    assignees: tuple[str, ...]


def issue_view(ref: IssueRef, *, cwd: Path) -> IssueInfo:
    """Look up a GitHub issue via `gh issue view`.

    `ref.repo` is passed as `--repo` when set, so the cross-repo forms resolve
    regardless of `cwd`; a bare number resolves against the repo at `cwd`.
    The returned `repo` is always the issue's own `owner/repo`, read back off
    the URL `gh` reports rather than assumed from the input.
    """
    args = [
        "issue",
        "view",
        str(ref.number),
        "--json",
        "number,title,body,url,state,labels,assignees",
    ]
    if ref.repo is not None:
        args.extend(["--repo", ref.repo])
    res = _run(args, cwd=cwd)
    if res.returncode != 0:
        label = f"{ref.repo}#{ref.number}" if ref.repo else f"#{ref.number}"
        raise GoblinError(
            f"No GitHub issue {label} found.",
            hint=(res.stderr or res.stdout).strip() or None,
        )
    import json

    try:
        data = json.loads(res.stdout)
    except json.JSONDecodeError as e:
        raise GoblinError(
            f"`gh issue view {ref.number}` returned output that wasn't valid JSON.",
            hint=res.stdout.strip() or None,
        ) from e
    url = data.get("url", "")
    from_url = _ISSUE_URL_RE.match(url)
    repo = from_url.group("repo").lower() if from_url else (ref.repo or "")
    return IssueInfo(
        number=int(data.get("number", ref.number)),
        repo=repo,
        title=data.get("title", ""),
        body=data.get("body") or "",
        url=url,
        state=str(data.get("state", "")),
        labels=tuple(
            str(item.get("name", "")) for item in data.get("labels") or [] if item.get("name")
        ),
        assignees=tuple(
            str(item.get("login", "")) for item in data.get("assignees") or [] if item.get("login")
        ),
    )


def issue_state(repo: str, number: int) -> str | None:
    """Return an issue's state (`OPEN`, `CLOSED`) or None if `gh` can't read it.

    Best-effort, like `pr_state`: a missing `gh` or a failed lookup reads as "no
    signal" so callers keep whatever they had cached.
    """
    if shutil.which("gh") is None:
        return None
    res = _run(["issue", "view", str(number), "--repo", repo, "--json", "state"])
    if res.returncode != 0:
        return None
    import json

    try:
        value = json.loads(res.stdout).get("state")
    except (json.JSONDecodeError, AttributeError):
        return None
    return str(value) if value else None


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


def pr_checks(url: str) -> str | None:
    """Roll up a PR's CI checks to `passing`, `failing`, or `pending`.

    Returns None when `gh` is missing, the lookup fails, or the PR has no checks
    configured at all — "no signal" is distinct from "passing", and callers must
    not report a green tick for a repo without CI.

    The rollup is deliberately coarse: any failure/timeout/cancellation is
    `failing`, any still-running or queued check is `pending`, and everything
    else having concluded successfully (or neutral/skipped) is `passing`.
    """
    if shutil.which("gh") is None:
        return None
    res = _run(["pr", "view", url, "--json", "statusCheckRollup"])
    if res.returncode != 0:
        return None
    import json

    try:
        rollup = json.loads(res.stdout).get("statusCheckRollup")
    except (json.JSONDecodeError, AttributeError):
        return None
    if not rollup:
        return None

    failing = {"FAILURE", "TIMED_OUT", "CANCELLED", "ACTION_REQUIRED", "STARTUP_FAILURE"}
    pending = {"PENDING", "QUEUED", "IN_PROGRESS", "WAITING", "REQUESTED", "EXPECTED"}
    saw_pending = False
    for check in rollup:
        if not isinstance(check, dict):
            continue
        # CheckRun reports `status` + `conclusion`; StatusContext reports `state`.
        status = str(check.get("status") or "").upper()
        conclusion = str(check.get("conclusion") or "").upper()
        legacy_state = str(check.get("state") or "").upper()
        if conclusion in failing or legacy_state in failing:
            return "failing"
        still_running = status and status != "COMPLETED"
        unknown = not status and not conclusion and not legacy_state
        if legacy_state in pending or still_running or unknown:
            saw_pending = True
    return "pending" if saw_pending else "passing"


def pr_status(*, cwd: Path) -> dict[str, str]:
    """Return key/value details for the PR associated with the current branch."""
    res = _run(["pr", "view", "--json", "url,state,number,title"], cwd=cwd)
    if res.returncode != 0:
        raise GoblinError(
            "No PR found for the current branch.",
            hint=(res.stderr or res.stdout).strip() or None,
        )
    import json

    try:
        data = json.loads(res.stdout)
    except json.JSONDecodeError as e:
        raise GoblinError(
            "`gh pr view` returned output that wasn't valid JSON.",
            hint=res.stdout.strip() or None,
        ) from e
    return {
        "url": data.get("url", ""),
        "state": data.get("state", ""),
        "number": str(data.get("number", "")),
        "title": data.get("title", ""),
    }


def pr_for_branch(cwd: Path, branch: str) -> dict[str, str] | None:
    """The most recent PR whose head is `branch`, or None when there isn't one.

    Returns `{url, state, number}`. Best-effort: missing `gh` or any lookup
    failure reads as "no PR", so callers can use it as an idempotency probe
    before `pr create`.
    """
    if shutil.which("gh") is None:
        return None
    res = _run(
        ["pr", "list", "--state", "all", "--head", branch, "--json", "url,state,number"],
        cwd=cwd,
    )
    if res.returncode != 0:
        return None
    import json

    try:
        items = json.loads(res.stdout)
    except json.JSONDecodeError:
        return None
    if not isinstance(items, list) or not items:
        return None
    item = items[0]
    return {
        "url": item.get("url", ""),
        "state": item.get("state", ""),
        "number": str(item.get("number", "")),
    }
