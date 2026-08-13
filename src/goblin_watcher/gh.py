"""Thin wrapper around the `gh` CLI for PR and issue operations."""

from __future__ import annotations

import re
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import cast

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


# `ERROR` is a StatusContext-only state (the legacy commit-status API's word for
# a failed check). `_ROLLUP_STATES` below already maps it to `failing`, so
# leaving it out here made the batched and per-PR paths disagree.
_FAILING_STATES = frozenset(
    {"FAILURE", "TIMED_OUT", "CANCELLED", "ACTION_REQUIRED", "STARTUP_FAILURE", "ERROR"}
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


@dataclass(frozen=True)
class ReviewComment:
    """One comment inside a review thread."""

    author: str
    body: str
    created_at: str


@dataclass(frozen=True)
class ReviewThread:
    """An unresolved inline review thread, in file order.

    `path`/`line` are None for a thread whose anchor GitHub no longer reports
    (a file deleted since the comment landed). `is_outdated` means the diff has
    moved on underneath it — worth showing, but the agent should re-read the
    code rather than trust the hunk.
    """

    path: str | None
    line: int | None
    is_outdated: bool
    diff_hunk: str
    comments: tuple[ReviewComment, ...]


@dataclass(frozen=True)
class ReviewSummary:
    """A review's top-level body — the prose a reviewer writes above the inline
    comments, which carries no resolved/unresolved state of its own."""

    author: str
    state: str
    body: str
    submitted_at: str


@dataclass(frozen=True)
class FailingCheck:
    """A failed `CheckRun` paired with the tail of its log.

    `log` is the failing steps' output when gw could fetch it (GitHub Actions
    only) and "" otherwise — a non-Actions status context contributes its URL
    and nothing more.
    """

    run: CheckRun
    log: str = ""


@dataclass(frozen=True)
class PrReview:
    """A PR's outstanding review feedback: unresolved threads + failing checks."""

    number: int
    title: str
    state: str
    url: str
    threads: tuple[ReviewThread, ...]
    summaries: tuple[ReviewSummary, ...]
    failing: tuple[FailingCheck, ...]

    @property
    def is_empty(self) -> bool:
        return not (self.threads or self.summaries or self.failing)


# One round-trip for everything `--address-review` seeds. Splitting this into
# `gh pr view` calls would cost a request per facet and still not reach
# `reviewThreads`, which the REST-backed `gh pr view --json` does not expose.
_PR_REVIEW_QUERY = """
query($owner: String!, $name: String!, $number: Int!) {
  repository(owner: $owner, name: $name) {
    pullRequest(number: $number) {
      number
      title
      state
      url
      reviewThreads(first: 100) {
        nodes {
          isResolved
          isOutdated
          path
          line
          comments(first: 50) {
            nodes { author { login } body createdAt diffHunk }
          }
        }
      }
      reviews(last: 30) {
        nodes { author { login } state body submittedAt }
      }
      commits(last: 1) {
        nodes {
          commit {
            statusCheckRollup {
              contexts(first: 100) {
                nodes {
                  __typename
                  ... on CheckRun {
                    name
                    status
                    conclusion
                    detailsUrl
                    # `gh pr view --json` flattens this to `workflowName`; GraphQL
                    # makes you walk to it, and there is no shortcut field.
                    checkSuite { workflowRun { workflow { name } } }
                  }
                  ... on StatusContext { context state targetUrl }
                }
              }
            }
          }
        }
      }
    }
  }
}
"""

# Review states whose body is feedback to act on. An APPROVED review's body is
# congratulation, and a DISMISSED one has been explicitly overruled.
_ACTIONABLE_REVIEW_STATES = frozenset({"CHANGES_REQUESTED", "COMMENTED"})

# `https://github.com/owner/repo/actions/runs/<run>/job/<job>` — the shape a
# GitHub Actions check run reports as its details URL, and the only one whose
# logs gw knows how to fetch.
_ACTIONS_JOB_URL_RE = re.compile(r"/actions/runs/\d+/job/(?P<job>\d+)")


def _obj(value: object) -> dict[str, object]:
    """A decoded JSON object, or `{}` for anything else.

    GraphQL nulls out a whole field — a connection, an author, a rollup — when it
    can't resolve it, so every level of the response has to survive being None.
    Funnelling that through one helper keeps the walk below free of `isinstance`
    ladders.
    """
    return cast(dict[str, object], value) if isinstance(value, dict) else {}


def _nodes(value: object) -> list[dict[str, object]]:
    """`{"nodes": [...]}` → the dict entries, or `[]` for anything else."""
    nodes = _obj(value).get("nodes")
    if not isinstance(nodes, list):
        return []
    return [_obj(n) for n in nodes if isinstance(n, dict)]


def _login(value: object) -> str:
    """`{"login": "alice"}` → `alice`. Author is null for a deleted account."""
    return str(_obj(value).get("login") or "unknown")


def _rollup_contexts(pr: dict[str, object]) -> list[dict[str, object]]:
    """The check runs on the PR's head commit, or `[]` when there are none."""
    commits = _nodes(pr.get("commits"))
    if not commits:
        return []
    rollup = _obj(_obj(commits[-1].get("commit")).get("statusCheckRollup"))
    return _nodes(rollup.get("contexts"))


def _failing_checks(pr: dict[str, object]) -> tuple[FailingCheck, ...]:
    """Select the failed checks from the head commit's rollup.

    Buckets through `_check_state`, the same function `pr_check_runs` uses, so a
    check `gw pr checks` calls failing is exactly one `--address-review` seeds.
    Handles both node types the rollup mixes: `CheckRun` (status + conclusion,
    named by `name`) and the legacy `StatusContext` (`state` + `context`).
    """
    out: list[FailingCheck] = []
    for ctx in _rollup_contexts(pr):
        status = str(ctx.get("status") or "").upper()
        conclusion = str(ctx.get("conclusion") or "").upper()
        legacy_state = str(ctx.get("state") or "").upper()
        if _check_state(status, conclusion, legacy_state) != "failing":
            continue
        name = str(ctx.get("name") or ctx.get("context") or "").strip()
        workflow = str(
            _obj(_obj(_obj(ctx.get("checkSuite")).get("workflowRun")).get("workflow")).get("name")
            or ""
        ).strip()
        url = str(ctx.get("detailsUrl") or ctx.get("targetUrl") or "")
        out.append(
            FailingCheck(
                run=CheckRun(
                    name=name or "(unnamed check)",
                    state="failing",
                    detail=conclusion or legacy_state,
                    url=url or None,
                    workflow=workflow or None,
                )
            )
        )
    return tuple(out)


def pr_review(repo: str, number: int) -> PrReview | None:
    """A PR's unresolved review threads, actionable review bodies, and failed checks.

    Best-effort like the rest of this module's read paths: a missing `gh`, an
    unparseable repo, or a failed API call all read as "no signal" (None) so the
    caller can decide whether that is fatal. An *empty* `PrReview` is a different
    answer — it means gw asked and the PR genuinely has nothing outstanding.
    """
    if shutil.which("gh") is None:
        return None
    owner, _, name = repo.partition("/")
    if not owner or not name:
        return None
    res = _run(
        [
            "api",
            "graphql",
            "-f",
            f"owner={owner}",
            "-f",
            f"name={name}",
            "-F",
            f"number={number}",
            "-f",
            f"query={_PR_REVIEW_QUERY}",
        ]
    )
    import json

    try:
        payload = json.loads(res.stdout)
    except json.JSONDecodeError:
        return None
    # `gh` exits non-zero on a GraphQL error but still prints a body, so stdout
    # is the signal, not the return code. A null anywhere down the chain (bad
    # credentials, deleted PR, repo gone) collapses to `{}` and reads as
    # "no signal" — a real PR always comes back with fields.
    repo_data = _obj(_obj(payload).get("data")).get("repository")
    pr = _obj(_obj(repo_data).get("pullRequest"))
    if not pr:
        return None

    threads: list[ReviewThread] = []
    for node in _nodes(pr.get("reviewThreads")):
        if node.get("isResolved"):
            continue
        raw_comments = _nodes(node.get("comments"))
        comments = tuple(
            ReviewComment(
                author=_login(c.get("author")),
                body=str(c.get("body") or "").strip(),
                created_at=str(c.get("createdAt") or ""),
            )
            for c in raw_comments
        )
        # A thread whose comments all came back empty carries nothing to act on.
        if not any(c.body for c in comments):
            continue
        # The hunk is a property of the thread, but GitHub hangs it off each
        # comment; the first one is the anchor the thread was opened against.
        first = raw_comments[0]
        line = node.get("line")
        threads.append(
            ReviewThread(
                path=str(node["path"]) if node.get("path") else None,
                line=int(line) if isinstance(line, int) else None,
                is_outdated=bool(node.get("isOutdated")),
                diff_hunk=str(first.get("diffHunk") or ""),
                comments=comments,
            )
        )

    summaries = tuple(
        ReviewSummary(
            author=_login(node.get("author")),
            state=str(node.get("state") or ""),
            body=str(node.get("body") or "").strip(),
            submitted_at=str(node.get("submittedAt") or ""),
        )
        for node in _nodes(pr.get("reviews"))
        if str(node.get("state") or "").upper() in _ACTIONABLE_REVIEW_STATES
        and str(node.get("body") or "").strip()
    )

    reported_number = pr.get("number")
    return PrReview(
        number=reported_number if isinstance(reported_number, int) else number,
        title=str(pr.get("title") or ""),
        state=str(pr.get("state") or ""),
        url=str(pr.get("url") or ""),
        threads=tuple(threads),
        summaries=summaries,
        failing=_failing_checks(pr),
    )


def check_run_log(repo: str, details_url: str) -> str | None:
    """The failing steps' log for a GitHub Actions check run, or None.

    Returns None for any check gw can't fetch logs for — a non-Actions status
    context, a run whose logs have expired, a missing `gh`. Callers show the
    check's URL in that case rather than pretending there was no failure.
    """
    if shutil.which("gh") is None:
        return None
    m = _ACTIONS_JOB_URL_RE.search(details_url)
    if m is None:
        return None
    res = _run(["run", "view", "--repo", repo, "--job", m.group("job"), "--log-failed"])
    if res.returncode != 0:
        return None
    return res.stdout.strip() or None
