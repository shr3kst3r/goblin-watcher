import os
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from goblin_watcher.errors import GitCommandError


def _run(args: list[str], cwd: Path | None = None) -> str:
    env = os.environ.copy()
    env["GIT_TERMINAL_PROMPT"] = "0"
    res = subprocess.run(
        ["git", *args],
        cwd=str(cwd) if cwd else None,
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )
    if res.returncode != 0:
        raise GitCommandError(
            f"git {' '.join(args)} failed (exit {res.returncode})",
            hint=(res.stderr or res.stdout).strip() or None,
        )
    return res.stdout


def ensure_git_available() -> None:
    if shutil.which("git") is None:
        raise GitCommandError(
            "`git` is not on PATH.",
            hint="Install git and ensure it's in your PATH.",
        )


def is_git_repo(path: Path) -> bool:
    if not path.exists():
        return False
    res = subprocess.run(
        ["git", "-C", str(path), "rev-parse", "--git-dir"],
        capture_output=True,
        text=True,
        check=False,
    )
    return res.returncode == 0


def repo_root(path: Path) -> Path:
    """Resolve `path` to the top-level working tree directory of its git repo."""
    out = _run(["-C", str(path), "rev-parse", "--show-toplevel"]).strip()
    return Path(out)


def clone(url: str, dest: Path) -> Path:
    """Clone `url` into `dest`. Returns the absolute path to the working tree."""
    ensure_git_available()
    dest.parent.mkdir(parents=True, exist_ok=True)
    _run(["clone", url, str(dest)])
    return dest.resolve()


def adopt(path: Path) -> Path:
    """Validate that `path` is a git working tree and return its root."""
    ensure_git_available()
    if not is_git_repo(path):
        raise GitCommandError(
            f"{path} is not a git working tree.",
            hint="Pass a directory that contains a .git directory, or use --repo to clone.",
        )
    return repo_root(path)


def current_branch(path: Path) -> str:
    try:
        return _run(["-C", str(path), "rev-parse", "--abbrev-ref", "HEAD"]).strip()
    except GitCommandError:
        # A repo with no commits yet (unborn HEAD) can't rev-parse HEAD, but
        # .git/HEAD still names the branch it points at.
        return _run(["-C", str(path), "symbolic-ref", "--short", "HEAD"]).strip()


def default_branch(path: Path) -> str:
    """Best-effort: read `origin/HEAD`, fall back to current branch."""
    try:
        ref = _run(["-C", str(path), "symbolic-ref", "refs/remotes/origin/HEAD"]).strip()
        return ref.rsplit("/", 1)[-1]
    except GitCommandError:
        return current_branch(path)


def common_git_dir(path: Path) -> Path:
    """Resolve the main repository's `.git` directory. For worktrees this is the
    main repo's .git, not the worktree's `.git` file."""
    out = _run(["-C", str(path), "rev-parse", "--git-common-dir"]).strip()
    common = Path(out)
    if not common.is_absolute():
        common = (path / common).resolve()
    return common.resolve()


def main_repo_root(path: Path) -> Path:
    """Resolve `path` to the working tree of the *main* repo it belongs to.

    For a regular checkout this matches `repo_root(path)`. For a worktree, this
    returns the main checkout's root (parent of the common .git directory).
    """
    common = common_git_dir(path)
    return common.parent.resolve()


def origin_url(path: Path) -> str | None:
    try:
        return _run(["-C", str(path), "remote", "get-url", "origin"]).strip() or None
    except GitCommandError:
        return None


def has_remote(path: Path) -> bool:
    """True if the checkout at `path` has at least one configured git remote.

    Distinct from `project.repo_url`: this is a live probe of the working tree's
    git config. Both checks exist because `--dir`-adopted projects may have
    drifted (remote added or removed after adoption).
    """
    try:
        out = _run(["-C", str(path), "remote"])
    except GitCommandError:
        return False
    return bool(out.strip())


def head_sha(path: Path) -> str:
    return _run(["-C", str(path), "rev-parse", "HEAD"]).strip()


def last_commit_title(repo: Path, ref: str) -> str | None:
    """Subject line of the most recent commit on `ref`, or None if it can't be resolved."""
    try:
        return _run(["-C", str(repo), "log", "-1", "--format=%s", ref, "--"]).strip()
    except GitCommandError:
        return None


def branch_exists(repo: Path, branch: str) -> bool:
    res = subprocess.run(
        ["git", "-C", str(repo), "rev-parse", "--verify", "--quiet", f"refs/heads/{branch}"],
        capture_output=True,
        text=True,
        check=False,
    )
    return res.returncode == 0


def commit_exists(repo: Path, ref: str) -> bool:
    """True if `ref` resolves to a commit in `repo`. False for missing refs and
    for any ref in a repo with no commits yet (unborn HEAD)."""
    res = subprocess.run(
        ["git", "-C", str(repo), "rev-parse", "--verify", "--quiet", f"{ref}^{{commit}}"],
        capture_output=True,
        text=True,
        check=False,
    )
    return res.returncode == 0


def remote_branch_exists(repo: Path, branch: str, remote: str = "origin") -> bool:
    res = subprocess.run(
        [
            "git",
            "-C",
            str(repo),
            "rev-parse",
            "--verify",
            "--quiet",
            f"refs/remotes/{remote}/{branch}",
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    return res.returncode == 0


def fetch(repo: Path, remote: str = "origin") -> None:
    _run(["-C", str(repo), "fetch", remote, "--quiet"])


def create_branch_from_remote(repo: Path, branch: str, remote: str = "origin") -> None:
    """Create a local branch tracking `<remote>/<branch>`."""
    _run(["-C", str(repo), "branch", branch, f"{remote}/{branch}"])


def fetch_pr_head(repo: Path, pr_number: int, branch: str, remote: str = "origin") -> None:
    """Fetch a PR's head into local `branch` via the `pull/<N>/head` ref.

    This is how a fork (cross-repository) PR is checked out without adding the
    fork as a remote — GitHub exposes every PR's head under `refs/pull/`.
    `+` forces an update when the local branch already exists from an earlier
    fetch, so re-running picks up new commits on the PR.
    """
    _run(
        [
            "-C",
            str(repo),
            "fetch",
            remote,
            f"+refs/pull/{pr_number}/head:refs/heads/{branch}",
        ]
    )


PullBaseOutcome = Literal[
    "no_remote",
    "fetch_failed",
    "no_remote_branch",
    "created",
    "up_to_date",
    "updated",
    "diverged",
    "dirty",
]


@dataclass(frozen=True)
class PullBaseResult:
    """Outcome of `pull_base_from_remote`.

    `outcome` is the structured status; `detail` is a one-line human summary
    suitable for surfacing in the CLI.
    """

    outcome: PullBaseOutcome
    detail: str


def _worktree_for_branch(repo: Path, branch: str) -> Path | None:
    target = f"refs/heads/{branch}"
    for entry in worktree_list(repo):
        if entry.get("branch") == target:
            wt = entry.get("worktree")
            if wt:
                return Path(wt)
    return None


def pull_base_from_remote(repo: Path, base: str, remote: str = "origin") -> PullBaseResult:
    """Fetch `remote` and fast-forward the local `base` branch to match it.

    Never mutates a branch that has diverged from its remote or that's
    checked out in a worktree with uncommitted changes — both surface as
    structured outcomes so the caller can warn and continue.
    """
    if not has_remote(repo):
        return PullBaseResult("no_remote", f"{repo} has no configured remote.")
    try:
        fetch(repo, remote)
    except GitCommandError as e:
        return PullBaseResult(
            "fetch_failed",
            f"git fetch {remote} failed: {e.hint or e.message}",
        )
    if not remote_branch_exists(repo, base, remote):
        return PullBaseResult(
            "no_remote_branch",
            f"{remote} has no branch {base!r}; nothing to fast-forward.",
        )
    if not branch_exists(repo, base):
        create_branch_from_remote(repo, base, remote)
        return PullBaseResult("created", f"created local {base!r} tracking {remote}/{base}.")
    local_sha = _run(["-C", str(repo), "rev-parse", base]).strip()
    remote_sha = _run(["-C", str(repo), "rev-parse", f"{remote}/{base}"]).strip()
    if local_sha == remote_sha:
        return PullBaseResult("up_to_date", f"{base} already at {remote}/{base}.")
    ancestor = subprocess.run(
        ["git", "-C", str(repo), "merge-base", "--is-ancestor", base, f"{remote}/{base}"],
        capture_output=True,
        text=True,
        check=False,
    )
    if ancestor.returncode != 0:
        return PullBaseResult(
            "diverged",
            f"local {base!r} has commits not on {remote}/{base}; left alone.",
        )
    checkout = _worktree_for_branch(repo, base)
    if checkout is not None:
        if has_uncommitted_changes(checkout):
            return PullBaseResult(
                "dirty",
                f"{base!r} is checked out at {checkout} with uncommitted changes; left alone.",
            )
        _run(["-C", str(checkout), "merge", "--ff-only", "--quiet", f"{remote}/{base}"])
    else:
        _run(["-C", str(repo), "branch", "-f", base, f"{remote}/{base}"])
    return PullBaseResult(
        "updated", f"fast-forwarded {base!r} to {remote}/{base} ({remote_sha[:12]})."
    )


def delete_branch(repo: Path, branch: str, force: bool = True) -> None:
    flag = "-D" if force else "-d"
    _run(["-C", str(repo), "branch", flag, branch])


def push(repo: Path, branch: str, set_upstream: bool = True, remote: str = "origin") -> None:
    args = ["-C", str(repo), "push"]
    if set_upstream:
        args.append("-u")
    args.extend([remote, branch])
    _run(args)


def worktree_add(repo: Path, dest: Path, branch: str, base: str | None = None) -> None:
    """Create a worktree at `dest`. If `base` is given and `branch` doesn't exist,
    create the branch off `base`. Otherwise check out the existing `branch`."""
    dest.parent.mkdir(parents=True, exist_ok=True)
    if branch_exists(repo, branch):
        _run(["-C", str(repo), "worktree", "add", str(dest), branch])
    else:
        if base and not commit_exists(repo, base):
            raise GitCommandError(
                f"Cannot create branch {branch!r}: base {base!r} does not resolve to a commit.",
                hint=(
                    "If the repo is brand new (no commits yet), push an initial commit "
                    f"to {base!r} first, then re-run."
                ),
            )
        args = ["-C", str(repo), "worktree", "add", "-b", branch, str(dest)]
        if base:
            args.append(base)
        _run(args)


def worktree_move(repo: Path, src: Path, dest: Path) -> None:
    """Move an existing worktree from `src` to `dest`, updating git's metadata.

    `git worktree move` rewrites the gitdir pointer so the relocated tree stays
    linked to `repo`. The destination's parent must exist.
    """
    dest.parent.mkdir(parents=True, exist_ok=True)
    _run(["-C", str(repo), "worktree", "move", str(src), str(dest)])


def worktree_remove(repo: Path, dest: Path, force: bool = False) -> None:
    args = ["-C", str(repo), "worktree", "remove", str(dest)]
    if force:
        args.append("--force")
    _run(args)


def worktree_prune(repo: Path) -> None:
    """Forget git's metadata for worktrees whose directory is no longer there.

    `git worktree add` refuses a path git still has registered, even when
    nothing is on disk — which is exactly the state left behind when a checkout
    was deleted outside git (the `shutil.rmtree` fallback both `gw task rm` and
    `gw task archive` fall back to). Pruning first is what lets `gw run`
    rematerialize an archived task's worktree at its original path.
    """
    _run(["-C", str(repo), "worktree", "prune"])


def worktree_list(repo: Path) -> list[dict[str, str]]:
    """Parse `git worktree list --porcelain` into [{path, branch, head, bare?}, ...]."""
    out = _run(["-C", str(repo), "worktree", "list", "--porcelain"])
    worktrees: list[dict[str, str]] = []
    current: dict[str, str] = {}
    for line in out.splitlines():
        if not line.strip():
            if current:
                worktrees.append(current)
                current = {}
            continue
        if " " in line:
            key, _, value = line.partition(" ")
        else:
            key, value = line, ""
        current[key] = value
    if current:
        worktrees.append(current)
    return worktrees


def has_uncommitted_changes(path: Path) -> bool:
    """True if the working tree has staged, unstaged, or untracked changes.

    Untracked files count: `gw task rm` falls back to `shutil.rmtree` if
    `git worktree remove` refuses, so an untracked-but-precious file could be
    silently lost. Catching it here is the gate that keeps that from happening.
    """
    out = _run(["-C", str(path), "status", "--porcelain"])
    return bool(out.strip())


def rev_list_count(repo: Path, range_spec: str) -> int:
    """Run `git rev-list --count <range_spec>`; return 0 if the range can't be resolved.

    Tolerant of missing refs (e.g. `origin/<branch>` for a never-pushed branch)
    so callers can use it as a probe without pre-validating both endpoints.
    """
    try:
        out = _run(["-C", str(repo), "rev-list", "--count", range_spec])
    except GitCommandError:
        return 0
    try:
        return int(out.strip())
    except ValueError:
        return 0


def commits_between(repo: Path, base: str, head: str) -> list[tuple[str, str, str]]:
    """Return commits on `head` not reachable from `base`, oldest first.

    Each entry is `(sha, subject, body)`. Bodies may be empty.
    """
    fmt = "%H%x1f%s%x1f%b%x1e"
    try:
        out = _run(
            ["-C", str(repo), "log", "--reverse", f"{base}..{head}", f"--pretty=format:{fmt}"]
        )
    except GitCommandError:
        return []
    commits: list[tuple[str, str, str]] = []
    for chunk in out.split("\x1e"):
        # Trim the inter-record newline only; default str.strip() eats \x1f too,
        # which would drop the trailing field separator on empty-body commits.
        chunk = chunk.lstrip("\r\n").rstrip("\r\n")
        if not chunk:
            continue
        parts = chunk.split("\x1f")
        if len(parts) < 2:
            continue
        sha = parts[0]
        subject = parts[1].strip()
        body = parts[2].strip() if len(parts) > 2 else ""
        commits.append((sha, subject, body))
    return commits


def diffstat(repo: Path, base: str, head: str) -> str:
    """Return `git diff --stat <base>..<head>` output, or '' if it errors."""
    try:
        return _run(["-C", str(repo), "diff", "--stat", f"{base}..{head}"]).strip()
    except GitCommandError:
        return ""


def is_branch_merged(repo: Path, branch: str, base: str, remote: str = "origin") -> bool:
    """True if `branch` is an ancestor of `<remote>/<base>` (or local `<base>` as fallback)
    AND has diverged from it at some point.

    A branch sitting at the same commit as its base hasn't been "merged" — it's just
    never diverged, so we don't flag it. Misses squash- and rebase-merged branches;
    callers should pair this with a PR-state check for full coverage. Run `fetch()`
    first for fresh results.
    """
    try:
        branch_sha = _run(["-C", str(repo), "rev-parse", branch]).strip()
    except GitCommandError:
        return False

    for target in (f"{remote}/{base}", base):
        res = subprocess.run(
            ["git", "-C", str(repo), "merge-base", "--is-ancestor", branch, target],
            capture_output=True,
            text=True,
            check=False,
        )
        if res.returncode == 128:  # target ref doesn't exist; try fallback
            continue
        if res.returncode != 0:
            return False
        try:
            target_sha = _run(["-C", str(repo), "rev-parse", target]).strip()
        except GitCommandError:
            return True
        return branch_sha != target_sha
    return False


PatchApplyOutcome = Literal["applied", "refused_dirty", "refused_diverged", "refused_conflict"]


@dataclass(frozen=True)
class PatchApplyResult:
    """Outcome of `apply_patch_safely`.

    `outcome` is the structured status; `detail` is a single human-readable
    line suitable for surfacing in the CLI.
    """

    outcome: PatchApplyOutcome
    detail: str


def apply_patch_safely(
    *,
    worktree: Path,
    patch: Path,
    base_sha: str,
) -> PatchApplyResult:
    """Apply `patch` to `worktree`, refusing if the tree is dirty or HEAD has moved.

    Designed for managed-agent patch-return (ADR 0002). The base_sha is the
    commit the patch was generated against; if HEAD has moved since then we
    refuse rather than risk applying onto unintended context. `git apply
    --3way` is used so applicable hunks land cleanly; conflicts surface as
    a structured `refused_conflict` so the caller can point the user at the
    patch file.

    Does NOT commit the result — the caller decides whether to wrap the
    application in a commit, leave it staged, or surface a hunk-by-hunk
    review.
    """
    if has_uncommitted_changes(worktree):
        return PatchApplyResult(
            outcome="refused_dirty",
            detail=f"worktree {worktree} has uncommitted changes; refusing to apply.",
        )
    try:
        current = head_sha(worktree)
    except GitCommandError as e:
        return PatchApplyResult(
            outcome="refused_diverged",
            detail=f"could not read HEAD of {worktree}: {e.message}",
        )
    if current != base_sha:
        return PatchApplyResult(
            outcome="refused_diverged",
            detail=(
                f"worktree HEAD ({current[:12]}) has moved from patch base "
                f"({base_sha[:12]}); refusing to apply."
            ),
        )
    res = subprocess.run(
        ["git", "-C", str(worktree), "apply", "--3way", str(patch)],
        capture_output=True,
        text=True,
        check=False,
    )
    if res.returncode == 0:
        return PatchApplyResult(outcome="applied", detail=f"applied {patch} cleanly.")
    stderr = (res.stderr or res.stdout).strip().splitlines()
    last = stderr[-1] if stderr else "(no error output)"
    return PatchApplyResult(
        outcome="refused_conflict",
        detail=f"git apply --3way refused: {last}",
    )


def add_to_local_exclude(repo: Path, pattern: str) -> None:
    """Add `pattern` to `.git/info/exclude` (no touch to the tracked .gitignore)."""
    exclude_file = repo / ".git" / "info" / "exclude"
    if not exclude_file.parent.exists():
        return
    existing = exclude_file.read_text() if exclude_file.exists() else ""
    if any(line.strip() == pattern for line in existing.splitlines()):
        return
    sep = "" if not existing or existing.endswith("\n") else "\n"
    exclude_file.write_text(f"{existing}{sep}{pattern}\n")
