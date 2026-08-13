from datetime import date, datetime
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


class LinearWorkflowState(_Frozen):
    """One state in a Linear team's workflow (`Todo`, `In Progress`, …)."""

    id: str
    name: str


class LinearIssueWorkflow(_Frozen):
    """Where one issue sits in its team's workflow, and where it could go.

    Read in a single query by `linear_transitions` so a configured state name
    can be resolved to the id `issueUpdate` wants without a second round-trip,
    and so a ticket already in the target state costs no write at all. Never
    persisted — it is a snapshot of the API, not part of the task record.
    """

    issue_id: str
    team_key: str
    state: str
    states: list[LinearWorkflowState] = Field(default_factory=list)

    def find_state(self, name: str) -> LinearWorkflowState | None:
        """The workflow state called `name`, matched case-insensitively."""
        wanted = name.strip().casefold()
        return next((s for s in self.states if s.name.casefold() == wanted), None)

    @property
    def state_names(self) -> list[str]:
        return [s.name for s in self.states]


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


class UsageBucket(_Frozen):
    """Token counts for one (model, local calendar day) pair of a session.

    Derived from the agent's own transcript — gw never calls a model to get
    these. Bucketing by day is what lets `gw history --cost` answer "what did
    today cost" without re-walking every transcript; bucketing by model is what
    makes the cost estimate priceable, since rates are per model.

    Cache writes are split by TTL because they are billed differently (1.25x
    input for the 5-minute cache, 2x for the 1-hour one). Agents that don't
    distinguish them report everything as `cache_write_tokens`.
    """

    model: str | None = None
    day: date | None = None
    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_tokens: int = 0
    cache_write_tokens: int = 0
    cache_write_1h_tokens: int = 0


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
    # Token usage parsed out of the agent's transcript, refreshed on the same
    # pass (and the same TTL) as `summary`. Empty for agents whose transcripts
    # gw can't read.
    usage: list[UsageBucket] = Field(default_factory=list)


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
    # The base-branch commit this branch was cut from, recorded when gw created
    # it. `None` means "we don't know" — records written before the field
    # existed, and branches gw adopted rather than created. Readers must never
    # read a missing value as "the branch has no commits": that inversion is
    # what let ancestry-based prune delete brand-new tasks.
    fork_sha: str | None = None


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
    # The task this one is stacked on, when `base_branch` turned out to be
    # another task's branch (`gw new --from`, or a `--pr` targeting a tracked
    # branch). An id rather than a branch name: the branch disappears when the
    # parent lands, but the id is what `gw status` and the PR body cite. Never a
    # validated reference — the parent can be pruned, and readers treat a
    # dangling id as "no longer tracked" rather than an error.
    parent_task: str | None = None
    pr_url: str | None = None
    # Where the primary branch started — see `TaskRepo.fork_sha`. Ancestry-based
    # merge detection is gated on it, because a branch still sitting on its fork
    # point is indistinguishable from a merged one in the commit graph.
    fork_sha: str | None = None
    created_at: datetime
    status: TaskStatus = "open"
    sessions: list[SessionRecord] = Field(default_factory=list)
    # Multi-repo support. `secondary_repos` is empty for the common single-repo
    # case (so existing task JSON validates unchanged). `workspace_path` is the
    # parent directory holding every repo's worktree as a subdir; it is the
    # agent's cwd and is set iff the task spans more than one repo.
    secondary_repos: list[TaskRepo] = Field(default_factory=list)
    workspace_path: Path | None = None
    # Archived: the worktree(s) were dropped but the record, the branch, and the
    # session history were kept (`gw task archive`). `gw run` rematerializes the
    # checkout from the branch and clears both fields. Deliberately not a
    # `TaskStatus` — a task can be archived at any point in the PR lifecycle, so
    # the two are orthogonal, and status must keep tracking the PR.
    archived: bool = False
    archived_at: datetime | None = None
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
            fork_sha=self.fork_sha,
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
