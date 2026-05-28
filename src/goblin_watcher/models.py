from datetime import datetime
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

AgentName = Literal["claude", "codex", "gemini", "managed"]
TaskStatus = Literal["open", "pushed", "pr-open", "merged", "closed", "abandoned"]


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


class Task(_Frozen):
    id: str
    project: str
    linear: LinearIssue | None = None
    branch: str
    worktree_path: Path
    base_branch: str
    pr_url: str | None = None
    created_at: datetime
    status: TaskStatus = "open"
    sessions: list[SessionRecord] = Field(default_factory=list)


class Project(_Frozen):
    name: str
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
