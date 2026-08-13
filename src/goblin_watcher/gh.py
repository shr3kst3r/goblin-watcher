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
_PR_URL_PARSE_RE = re.compile(
    r"^https://github\.com/(?P<repo>[^/\s]+/[^/\s]+)/pull/(?P<number>\d+)(?:[/?#].*)?$"
)

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
    except json.JSONDecodeError, AttributeError:
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
    except json.JSONDecodeError, AttributeError:
        return None


_FAILING_STATES = frozenset(
    {"FAILURE", "TIMED_OUT", "CANCELLED", "ACTION_REQUIRED", "STARTUP_FAILURE"}
)
_PENDING_STATES = frozenset(
    {"PENDING", "QUEUED", "IN_PROGRESS", "WAITING", "REQUESTED", "EXPECTED"}
)


@dataclass(frozen=True)
class CheckRun:
    """One entry from a PR's status-check rollup.

    `state` is gw's coarse bucket (`passing`/`failing`/`pending`) — the same
    vocabulary `pr_checks` rolls up — and `detail` is GitHub's own word for it
    (`SUCCESS`, `TIMED_OUT`, `IN_PROGRESS`, ...), which keeps the distinctions
    the bucket flattens away. `url` is the check's details page, or None when
    GitHub reports none.
    """

    name: str
    state: str
    detail: str
    url: str | None = None
    workflow: str | None = None

    @property
    def label(self) -> str:
        """`workflow / job` for a workflow's check run, else just the name.

        GitHub Actions names the *job* in `name` and the workflow around it in
        `workflowName`; two workflows can each have a `test` job, so the name
        alone isn't enough to tell which one broke.
        """
        if self.workflow and self.workflow != self.name:
            return f"{self.workflow} / {self.name}"
        return self.name


def _check_state(status: str, conclusion: str, legacy_state: str) -> str:
    """Bucket one rollup entry as `passing`, `failing`, or `pending`.

    Deliberately coarse: any failure/timeout/cancellation is `failing`, anything
    still running or queued is `pending`, and everything else that concluded
    (success, neutral, skipped) is `passing`. An entry we can't read at all
    counts as `pending` rather than `passing` — the safe direction.
    """
    if conclusion in _FAILING_STATES or legacy_state in _FAILING_STATES:
        return "failing"
    still_running = bool(status) and status != "COMPLETED"
    unknown = not status and not conclusion and not legacy_state
    if legacy_state in _PENDING_STATES or still_running or unknown:
        return "pending"
    return "passing"


def pr_check_runs(url: str) -> list[CheckRun] | None:
    """A PR's CI checks, one `CheckRun` per rollup entry, in rollup order.

    The single source of check data: `pr_checks` folds this down to one word.
    Returns None when there was no signal at all — `gh` is missing, the lookup
    failed, or the PR has no checks configured. "No signal" is distinct from
    "passing", and no caller may render a green tick for a repo without CI.
    """
    if shutil.which("gh") is None:
        return None
    res = _run(["pr", "view", url, "--json", "statusCheckRollup"])
    if res.returncode != 0:
        return None
    import json

    try:
        rollup = json.loads(res.stdout).get("statusCheckRollup")
    except json.JSONDecodeError, AttributeError:
        return None
    if not isinstance(rollup, list) or not rollup:
        return None

    runs: list[CheckRun] = []
    for check in rollup:
        if not isinstance(check, dict):
            continue
        # CheckRun reports `status` + `conclusion` and names itself; StatusContext
        # (the legacy commit-status API, still how e.g. Azure Pipelines reports)
        # has a single `state` and calls its name a `context`.
        status = str(check.get("status") or "").upper()
        conclusion = str(check.get("conclusion") or "").upper()
        legacy_state = str(check.get("state") or "").upper()
        name = str(check.get("name") or check.get("context") or "").strip()
        workflow = str(check.get("workflowName") or "").strip()
        target = str(check.get("detailsUrl") or check.get("targetUrl") or "").strip()
        runs.append(
            CheckRun(
                name=name or "(unnamed check)",
                state=_check_state(status, conclusion, legacy_state),
                # GitHub's own word: the outcome once there is one, else whatever
                # the check is currently doing.
                detail=conclusion or legacy_state or status,
                url=target or None,
                workflow=workflow or None,
            )
        )
    return runs


def pr_checks(url: str) -> str | None:
    """Roll up a PR's CI checks to `passing`, `failing`, or `pending`.

    Returns None when there was no signal at all (see `pr_check_runs`). Failing
    wins over pending, which wins over passing — one broken check makes the whole
    PR read as broken. Reach for `pr_check_runs` when you need to know *which*
    check that was.
    """
    runs = pr_check_runs(url)
    if runs is None:
        return None
    states = {r.state for r in runs}
    if "failing" in states:
        return "failing"
    if "pending" in states:
        return "pending"
    return "passing"


def parse_pr_url(url: str) -> tuple[str, int] | None:
    """Split a github.com PR URL into `(owner/repo, number)`, else None.

    Only github.com matches. Anything else — a GitHub Enterprise host, a URL
    shape `gh` accepts but we don't recognise — reads as "not batchable", and
    callers fall back to the per-PR lookups.
    """
    m = _PR_URL_PARSE_RE.match(url.strip())
    if m is None:
        return None
    return m.group("repo").removesuffix(".git").lower(), int(m.group("number"))


@dataclass(frozen=True)
class PrSnapshot:
    """One PR's state and CI rollup, as gw's coarse vocabulary.

    `state` is `OPEN`/`CLOSED`/`MERGED` (as `pr_state` returns) and `checks` is
    `passing`/`failing`/`pending` (as `pr_checks` returns). Either is None when
    there was no signal, which callers treat as "keep what you had".
    """

    state: str | None = None
    checks: str | None = None


# GitHub's own `StatusCheckRollup.state`, mapped onto the same three buckets
# `pr_checks` derives by hand from the individual check runs. Letting GitHub do
# the aggregation is what makes the batched query cheap: we ask for one enum per
# PR instead of every check run's status and conclusion.
_ROLLUP_STATES: dict[str, str] = {
    "SUCCESS": "passing",
    "FAILURE": "failing",
    "ERROR": "failing",
    "PENDING": "pending",
    "EXPECTED": "pending",
}

# Aliases per GraphQL query. Each query costs one rate-limit point regardless of
# how many PRs it names, so this only bounds the request body — big enough that
# realistic repos take a single round-trip.
_PR_BATCH_SIZE = 100


def pr_snapshots(repo: str, numbers: list[int]) -> dict[int, PrSnapshot]:
    """State + CI rollup for many PRs in one repo, in one API call per 100.

    A single aliased GraphQL query costs one rate-limit point no matter how many
    PRs it names, where `pr_state` + `pr_checks` cost two points *per PR*.

    Best-effort throughout, matching `pr_state`: a missing `gh`, an unreachable
    API, or a deleted PR all read as "no signal". Numbers that resolve to
    nothing are simply absent from the returned mapping rather than mapped to an
    empty snapshot, so a caller can tell "we asked and got nothing" from "we
    never asked".
    """
    if shutil.which("gh") is None or not numbers:
        return {}
    owner, _, name = repo.partition("/")
    if not owner or not name:
        return {}

    import json

    out: dict[int, PrSnapshot] = {}
    unique = sorted(set(numbers))
    for start in range(0, len(unique), _PR_BATCH_SIZE):
        chunk = unique[start : start + _PR_BATCH_SIZE]
        # `owner`/`name` go through GraphQL variables; the numbers are already
        # ints, so the aliases are the only interpolation and they cannot carry
        # anything but digits.
        fields = "\n".join(
            f"    p{n}: pullRequest(number: {n}) {{ state statusCheckRollup {{ state }} }}"
            for n in chunk
        )
        query = (
            "query($owner: String!, $name: String!) {\n"
            "  repository(owner: $owner, name: $name) {\n"
            f"{fields}\n"
            "  }\n"
            "}"
        )
        res = _run(
            ["api", "graphql", "-f", f"owner={owner}", "-f", f"name={name}", "-f", f"query={query}"]
        )
        # A PR that no longer exists makes `gh` exit non-zero *and* still return
        # the data for every other alias, so the return code is not a usable
        # signal here — parse stdout either way and let missing aliases fall out
        # as "no snapshot".
        try:
            payload = json.loads(res.stdout)
        except json.JSONDecodeError:
            continue
        repo_data = (payload.get("data") or {}).get("repository")
        if not isinstance(repo_data, dict):
            continue
        for n in chunk:
            pr = repo_data.get(f"p{n}")
            if not isinstance(pr, dict):
                continue
            rollup = pr.get("statusCheckRollup")
            rollup_state = rollup.get("state") if isinstance(rollup, dict) else None
            out[n] = PrSnapshot(
                state=str(pr["state"]) if pr.get("state") else None,
                checks=_ROLLUP_STATES.get(str(rollup_state or "").upper()),
            )
    return out


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
