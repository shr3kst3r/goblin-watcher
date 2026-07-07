import subprocess
from pathlib import Path

import pytest

from goblin_watcher import git
from goblin_watcher.errors import GitCommandError


def _init_repo(path: Path, branch: str = "main") -> None:
    path.mkdir(parents=True, exist_ok=True)
    subprocess.run(["git", "init", "-q", "-b", branch, str(path)], check=True)
    subprocess.run(["git", "-C", str(path), "config", "user.email", "t@t.example"], check=True)
    subprocess.run(["git", "-C", str(path), "config", "user.name", "tester"], check=True)
    (path / "README.md").write_text("hi")
    subprocess.run(["git", "-C", str(path), "add", "."], check=True)
    subprocess.run(["git", "-C", str(path), "commit", "-qm", "init"], check=True)


def test_is_git_repo_true_for_init(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    _init_repo(repo)
    assert git.is_git_repo(repo)


def test_is_git_repo_false_for_plain_dir(tmp_path: Path) -> None:
    plain = tmp_path / "plain"
    plain.mkdir()
    assert not git.is_git_repo(plain)


def test_adopt_returns_repo_root(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    _init_repo(repo)
    # adopt() should normalize/resolve the path
    sub = repo / "subdir"
    sub.mkdir()
    assert git.adopt(sub).resolve() == repo.resolve()


def test_adopt_raises_for_non_repo(tmp_path: Path) -> None:
    with pytest.raises(GitCommandError):
        git.adopt(tmp_path)


def test_current_branch(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    _init_repo(repo, branch="trunk")
    assert git.current_branch(repo) == "trunk"


def test_current_branch_repo_with_no_commits(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init", "-q", "-b", "trunk", str(repo)], check=True)
    assert git.current_branch(repo) == "trunk"


def test_default_branch_clone_of_empty_remote(tmp_path: Path) -> None:
    # A brand-new (zero-commit) remote: clone succeeds but HEAD is unborn.
    remote = tmp_path / "remote.git"
    subprocess.run(["git", "init", "-q", "--bare", "-b", "main", str(remote)], check=True)
    clone = git.clone(str(remote), tmp_path / "clone")
    assert git.default_branch(clone) == "main"


def test_commit_exists(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    _init_repo(repo)
    assert git.commit_exists(repo, "main")
    assert not git.commit_exists(repo, "no-such-ref")


def test_commit_exists_false_in_repo_with_no_commits(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init", "-q", "-b", "main", str(repo)], check=True)
    assert not git.commit_exists(repo, "main")


def test_has_remote_false_for_fresh_init(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    _init_repo(repo)
    assert not git.has_remote(repo)


def test_has_remote_true_when_configured(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    _init_repo(repo)
    subprocess.run(
        ["git", "-C", str(repo), "remote", "add", "origin", "https://example.invalid/x.git"],
        check=True,
    )
    assert git.has_remote(repo)


def test_has_remote_false_for_non_repo(tmp_path: Path) -> None:
    plain = tmp_path / "plain"
    plain.mkdir()
    assert not git.has_remote(plain)


def test_last_commit_title_returns_subject(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    _init_repo(repo)
    (repo / "next.txt").write_text("more")
    subprocess.run(["git", "-C", str(repo), "add", "."], check=True)
    subprocess.run(["git", "-C", str(repo), "commit", "-qm", "add next file"], check=True)
    assert git.last_commit_title(repo, "main") == "add next file"


def test_last_commit_title_none_for_missing_ref(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    _init_repo(repo)
    assert git.last_commit_title(repo, "no-such-branch") is None


def _make_patch(src_repo: Path, modify: callable) -> Path:  # type: ignore[type-arg]
    """Produce a single-commit patch by editing src_repo, committing, and
    `git format-patch`-ing the new commit. Returns the path to the patch file.
    """
    modify(src_repo)
    subprocess.run(["git", "-C", str(src_repo), "add", "-A"], check=True)
    subprocess.run(["git", "-C", str(src_repo), "commit", "-qm", "patch commit"], check=True)
    out_dir = src_repo / "_patches"
    out_dir.mkdir(exist_ok=True)
    subprocess.run(
        ["git", "-C", str(src_repo), "format-patch", "-1", "HEAD", "-o", str(out_dir)],
        check=True,
        capture_output=True,
    )
    patches = sorted(out_dir.glob("*.patch"))
    assert patches, "format-patch produced nothing"
    return patches[-1]


def test_apply_patch_safely_applies_on_clean_matching_head(tmp_path: Path) -> None:
    src = tmp_path / "src"
    _init_repo(src)
    target = tmp_path / "target"
    _init_repo(target)
    base_sha = git.head_sha(target)

    def add_file(repo: Path) -> None:
        (repo / "new.txt").write_text("from patch\n")

    patch = _make_patch(src, add_file)
    result = git.apply_patch_safely(worktree=target, patch=patch, base_sha=base_sha)
    assert result.outcome == "applied", result.detail
    assert (target / "new.txt").exists()


def test_apply_patch_safely_refuses_when_worktree_dirty(tmp_path: Path) -> None:
    src = tmp_path / "src"
    _init_repo(src)
    target = tmp_path / "target"
    _init_repo(target)
    base_sha = git.head_sha(target)
    # Dirty the target.
    (target / "dirty.txt").write_text("untracked\n")

    def add_file(repo: Path) -> None:
        (repo / "new.txt").write_text("from patch\n")

    patch = _make_patch(src, add_file)
    result = git.apply_patch_safely(worktree=target, patch=patch, base_sha=base_sha)
    assert result.outcome == "refused_dirty"
    assert "uncommitted" in result.detail.lower()


def test_apply_patch_safely_refuses_when_head_moved(tmp_path: Path) -> None:
    src = tmp_path / "src"
    _init_repo(src)
    target = tmp_path / "target"
    _init_repo(target)
    stale_base = "0" * 40

    def add_file(repo: Path) -> None:
        (repo / "new.txt").write_text("from patch\n")

    patch = _make_patch(src, add_file)
    result = git.apply_patch_safely(worktree=target, patch=patch, base_sha=stale_base)
    assert result.outcome == "refused_diverged"
    assert "moved" in result.detail.lower()


def test_apply_patch_safely_refuses_conflicting_patch(tmp_path: Path) -> None:
    src = tmp_path / "src"
    _init_repo(src)
    target = tmp_path / "target"
    _init_repo(target)
    # Diverge the target's README so the patch can't 3-way-merge cleanly.
    (target / "README.md").write_text("target side\n")
    subprocess.run(["git", "-C", str(target), "add", "-A"], check=True)
    subprocess.run(["git", "-C", str(target), "commit", "-qm", "target divergence"], check=True)
    base_sha = git.head_sha(target)

    def edit_readme(repo: Path) -> None:
        (repo / "README.md").write_text("src side\n")

    patch = _make_patch(src, edit_readme)
    result = git.apply_patch_safely(worktree=target, patch=patch, base_sha=base_sha)
    assert result.outcome == "refused_conflict"


def test_default_branch_falls_back_to_current_without_origin(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    _init_repo(repo, branch="main")
    # No origin remote → falls back to current branch.
    assert git.default_branch(repo) == "main"


def test_clone_local_repo(tmp_path: Path) -> None:
    src = tmp_path / "source"
    _init_repo(src)
    dest = tmp_path / "cloned"
    cloned_root = git.clone(str(src), dest)
    assert (cloned_root / "README.md").exists()


def test_rev_list_count_counts_commits_in_range(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    _init_repo(repo)
    subprocess.run(["git", "-C", str(repo), "checkout", "-qb", "feature"], check=True)
    (repo / "a.txt").write_text("a")
    subprocess.run(["git", "-C", str(repo), "add", "a.txt"], check=True)
    subprocess.run(["git", "-C", str(repo), "commit", "-qm", "a"], check=True)
    (repo / "b.txt").write_text("b")
    subprocess.run(["git", "-C", str(repo), "add", "b.txt"], check=True)
    subprocess.run(["git", "-C", str(repo), "commit", "-qm", "b"], check=True)
    assert git.rev_list_count(repo, "main..feature") == 2
    assert git.rev_list_count(repo, "feature..main") == 0


def test_rev_list_count_returns_zero_for_missing_ref(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    _init_repo(repo)
    # `origin/main` doesn't exist in a freshly init'd local repo.
    assert git.rev_list_count(repo, "origin/main..main") == 0


def test_add_to_local_exclude_appends_pattern(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    _init_repo(repo)
    git.add_to_local_exclude(repo, ".goblin/")
    text = (repo / ".git" / "info" / "exclude").read_text()
    assert ".goblin/" in text
    # Idempotent.
    git.add_to_local_exclude(repo, ".goblin/")
    assert (
        text.count(".goblin/") == 1
        or (repo / ".git" / "info" / "exclude").read_text().count(".goblin/") == 1
    )


def _commit_file(repo: Path, name: str, body: str, message: str) -> None:
    (repo / name).write_text(body)
    subprocess.run(["git", "-C", str(repo), "add", name], check=True)
    subprocess.run(["git", "-C", str(repo), "commit", "-qm", message], check=True)


def _clone(src: Path, dest: Path) -> None:
    subprocess.run(["git", "clone", "-q", str(src), str(dest)], check=True)
    subprocess.run(["git", "-C", str(dest), "config", "user.email", "t@t"], check=True)
    subprocess.run(["git", "-C", str(dest), "config", "user.name", "tester"], check=True)


def test_pull_base_from_remote_no_remote(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    _init_repo(repo)
    result = git.pull_base_from_remote(repo, "main")
    assert result.outcome == "no_remote"


def test_pull_base_from_remote_up_to_date(tmp_path: Path) -> None:
    upstream = tmp_path / "upstream"
    _init_repo(upstream)
    clone = tmp_path / "clone"
    _clone(upstream, clone)
    result = git.pull_base_from_remote(clone, "main")
    assert result.outcome == "up_to_date", result.detail


def test_pull_base_from_remote_updates_when_behind(tmp_path: Path) -> None:
    upstream = tmp_path / "upstream"
    _init_repo(upstream)
    clone = tmp_path / "clone"
    _clone(upstream, clone)
    # Upstream advances after the clone.
    _commit_file(upstream, "next.txt", "new\n", "next")
    upstream_head = git.head_sha(upstream)

    result = git.pull_base_from_remote(clone, "main")
    assert result.outcome == "updated", result.detail
    # The local `main` ref must now point at the upstream HEAD, AND the
    # worktree where main is checked out must have the new file present.
    assert git.head_sha(clone) == upstream_head
    assert (clone / "next.txt").exists()


def test_pull_base_from_remote_diverged_leaves_local_alone(tmp_path: Path) -> None:
    upstream = tmp_path / "upstream"
    _init_repo(upstream)
    clone = tmp_path / "clone"
    _clone(upstream, clone)
    # Clone advances on top of main with a commit that isn't on upstream.
    _commit_file(clone, "local.txt", "mine\n", "local commit")
    local_head = git.head_sha(clone)
    # Upstream also advances, on a different file.
    _commit_file(upstream, "their.txt", "theirs\n", "their commit")

    result = git.pull_base_from_remote(clone, "main")
    assert result.outcome == "diverged", result.detail
    assert git.head_sha(clone) == local_head
    assert (clone / "local.txt").exists()
    assert not (clone / "their.txt").exists()


def test_pull_base_from_remote_dirty_worktree_leaves_local_alone(tmp_path: Path) -> None:
    upstream = tmp_path / "upstream"
    _init_repo(upstream)
    clone = tmp_path / "clone"
    _clone(upstream, clone)
    _commit_file(upstream, "next.txt", "new\n", "next")
    local_head = git.head_sha(clone)
    # Dirty the checkout so the ff would be unsafe.
    (clone / "README.md").write_text("scratch")

    result = git.pull_base_from_remote(clone, "main")
    assert result.outcome == "dirty", result.detail
    assert git.head_sha(clone) == local_head


def test_pull_base_from_remote_no_remote_branch(tmp_path: Path) -> None:
    upstream = tmp_path / "upstream"
    _init_repo(upstream)
    clone = tmp_path / "clone"
    _clone(upstream, clone)
    result = git.pull_base_from_remote(clone, "does-not-exist")
    assert result.outcome == "no_remote_branch", result.detail


def test_pull_base_from_remote_creates_missing_local_branch(tmp_path: Path) -> None:
    upstream = tmp_path / "upstream"
    _init_repo(upstream)
    subprocess.run(["git", "-C", str(upstream), "checkout", "-q", "-b", "feat/x"], check=True)
    _commit_file(upstream, "x.txt", "x\n", "x")
    subprocess.run(["git", "-C", str(upstream), "checkout", "-q", "main"], check=True)
    clone = tmp_path / "clone"
    _clone(upstream, clone)
    assert not git.branch_exists(clone, "feat/x")

    result = git.pull_base_from_remote(clone, "feat/x")
    assert result.outcome == "created", result.detail
    assert git.branch_exists(clone, "feat/x")


def test_pull_base_from_remote_updates_non_checked_out_branch(tmp_path: Path) -> None:
    """When the base branch isn't HEAD anywhere, the ref still fast-forwards."""
    upstream = tmp_path / "upstream"
    _init_repo(upstream)
    subprocess.run(["git", "-C", str(upstream), "checkout", "-q", "-b", "feat/x"], check=True)
    _commit_file(upstream, "x.txt", "v1\n", "x v1")
    subprocess.run(["git", "-C", str(upstream), "checkout", "-q", "main"], check=True)
    clone = tmp_path / "clone"
    _clone(upstream, clone)
    # Create a local tracking branch for feat/x without checking it out.
    git.create_branch_from_remote(clone, "feat/x")
    starting_sha = subprocess.run(
        ["git", "-C", str(clone), "rev-parse", "feat/x"],
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip()
    # Upstream advances feat/x further.
    subprocess.run(["git", "-C", str(upstream), "checkout", "-q", "feat/x"], check=True)
    _commit_file(upstream, "x.txt", "v2\n", "x v2")
    upstream_head = git.head_sha(upstream)
    subprocess.run(["git", "-C", str(upstream), "checkout", "-q", "main"], check=True)

    result = git.pull_base_from_remote(clone, "feat/x")
    assert result.outcome == "updated", result.detail
    final_sha = subprocess.run(
        ["git", "-C", str(clone), "rev-parse", "feat/x"],
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip()
    assert final_sha == upstream_head
    assert final_sha != starting_sha
