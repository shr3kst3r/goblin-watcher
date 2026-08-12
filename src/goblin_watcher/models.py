from datetime import datetime
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

AgentName = Literal["claude", "codex", "gemini", "antigravity", "managed"]
TaskStatus = Literal["open", "pushed", "pr-open", "merged", "closed", "abandoned"]
# "git" projects are repos gw manages worktrees in; the single reserved
# "scratch" project is a plain-directory container for `gw scratch` spaces.
ProjectKind = Literal["git", "scratch"]
# A "scratch" task's worktree_path is a plain directory — no git repo, no
# branches, no PRs. Defaults keep pre-existing JSON records valid.
TaskKind = Literal["repo", "scratch"]


class _Frozen(BaseModel):
    model_config = ConfigDict(extra="forbid")


class LinearComment(_Frozen):
    body: str
    created_at: datetime
    author: str | None = None  # displayName/name; None for system or anonymized comments


class LinearIssue(_Frozen):
    id: str
    identifier: str
    title: str
    description: str | None = None
    state: str
    team_key: str
    url: str
    comments: list[LinearComment] = Field(default_factory=list)


class GhIssue(_Frozen):
    """A GitHub issue snapshot, taken when the task was created.

    `repo` is the issue's own lowercased `owner/repo`, which is not necessarily
    the repo the task works in: a tracking issue may live in another repository,
    which is what makes the qualified `reference` form the safe one to cite.
    """

    number: int
    repo: str
    title: str
    body: str | None = None
    state: str
    url: str
    labels: list[str] = Field(default_factory=list)
    assignees: list[str] = Field(default_factory=list)

    @property
    def reference(self) -> str:
        """The fully-qualified `owner/repo#42` form, safe to use cross-repo."""
        return f"{self.repo}#{self.number}"


class AgentRef(_Frozen):
    name: AgentName


class SessionRecord(_Frozen):
    agent: AgentName
    session_id: str
    created_at: datetime
    last_used_at: datetime
    label: str | None = None
    summary: str | None = None
    turn_count: int = 0
    summary_updated_at: datetime | None = None
    transcript_path: Path | None = None
    # LLM-generated one-line characterization. Refreshed lazily in the
    # background; falls back to `summary` when missing.
    description: str | None = None
    description_updated_at: datetime | None = None


class TaskRepo(_Frozen):
    """One repository participating in a task.

    The task's *primary* repo is stored as scalar fields on `Task` (for zero-
    migration back-compat); every additional repo is a `TaskRepo` in
    `Task.secondary_repos`. Iterate `Task.all_repos()` to treat them uniformly.
    """

    project: str
    branch: str
    worktree_path: Path
    base_branch: str
    pr_url: str | None = None


class Task(_Frozen):
    id: str
    kind: TaskKind = "repo"
    project: str
    linear: LinearIssue | None = None
    # The GitHub issue this task tracks, for `--issue`-sourced tasks. Mutually
    # exclusive with `linear` in practice (one source flag per `gw new`), but
    # the model doesn't forbid both.
    github_issue: GhIssue | None = None
    branch: str
    worktree_path: Path
    base_branch: str
    pr_url: str | None = None
    created_at: datetime
    status: TaskStatus = "open"
    sessions: list[SessionRecord] = Field(default_factory=list)
    # Multi-repo support. `secondary_repos` is empty for the common single-repo
    # case (so existing task JSON validates unchanged). `workspace_path` is the
    # parent directory holding every repo's worktree as a subdir; it is the
    # agent's cwd and is set iff the task spans more than one repo.
    secondary_repos: list[TaskRepo] = Field(default_factory=list)
    workspace_path: Path | None = None
    # When the cached `linear.state` was last fetched from the API. Lets
    # `gw status` skip per-task Linear round-trips inside a TTL window.
    linear_state_updated_at: datetime | None = None
    # Same idea for `github_issue.state`, refreshed via `gh issue view`.
    github_issue_state_updated_at: datetime | None = None

    @property
    def is_multi_repo(self) -> bool:
        return bool(self.secondary_repos)

    @property
    def ticket_id(self) -> str:
        """Human-facing id of the tracking item, or the task id when there isn't one.

        Linear tickets report `ENG-123`; GitHub issues report the qualified
        `owner/repo#42`, which stays unambiguous for a cross-repo tracking issue.
        """
        if self.linear is not None:
            return self.linear.identifier
        if self.github_issue is not None:
            return self.github_issue.reference
        return self.id.upper()

    @property
    def ticket_title(self) -> str | None:
        """Title of the tracking item, or None when the task has no upstream."""
        if self.linear is not None:
            return self.linear.title
        if self.github_issue is not None:
            return self.github_issue.title
        return None

    @property
    def agent_cwd(self) -> Path:
        """The directory agents are launched in — and therefore the cwd their
        session stores are keyed on. Multi-repo tasks run in the workspace;
        single-repo tasks run in the worktree. Every transcript lookup must
        use this, not `worktree_path`, or multi-repo sessions go missing."""
        return self.workspace_path or self.worktree_path

    def primary_repo(self) -> TaskRepo:
        """The primary repo as a `TaskRepo`, built from the scalar fields."""
        return TaskRepo(
            project=self.project,
            branch=self.branch,
            worktree_path=self.worktree_path,
            base_branch=self.base_branch,
            pr_url=self.pr_url,
        )

    def all_repos(self) -> list[TaskRepo]:
        """Every repo on the task, primary first — the canonical iteration order."""
        return [self.primary_repo(), *self.secondary_repos]


class Project(_Frozen):
    name: str
    kind: ProjectKind = "git"
    root: Path
    repo_url: str | None = None
    default_branch: str = "main"
    branch_prefix: str = ""
    worktree_root: Path | None = None
    default_agent: AgentRef | None = None
    linear_team_key: str | None = None
    created_at: datetime


class GlobalState(_Frozen):
    schema_version: int = 1
    projects: dict[str, Path] = Field(default_factory=dict)
