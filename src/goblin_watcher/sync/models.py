"""Pydantic models for background sync state, cache, and pass reports."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Literal

from pydantic import BaseModel, Field

StepName = Literal[
    "fetch",
    "linear",
    "github-issue",
    "reconcile",
    "summaries",
    "descriptions",
    "prs",
    "indicators",
    "prune",
    "notify",
    "actions",
]

PassStatus = Literal["ok", "partial", "skipped", "error"]


def _now() -> datetime:
    return datetime.now(UTC)


class TaskIndicators(BaseModel):
    """Derived git/PR facts for one task. Regenerable; never authoritative.

    Cached so `gw status` can render without shelling out to git per task. The
    task record itself stays owned by the interactive flows (ADR 0005).
    """

    uncommitted: bool = False
    # Commits on the task branch not yet on its upstream (or base, when the
    # branch was never pushed).
    ahead: int = 0
    # True when `ahead` was measured against `origin/<branch>`; False when the
    # branch has no remote counterpart and base was used instead.
    ahead_vs_remote: bool = False
    pr_state: str | None = None  # OPEN | CLOSED | MERGED
    checks: str | None = None  # passing | failing | pending
    computed_at: datetime = Field(default_factory=_now)

    def age_seconds(self, now: datetime | None = None) -> float:
        ts = self.computed_at
        if ts.tzinfo is None:
            ts = ts.replace(tzinfo=UTC)
        return ((now or _now()) - ts).total_seconds()


class IndicatorCache(BaseModel):
    """All tasks' indicators, keyed `<project>/<task_id>`."""

    entries: dict[str, TaskIndicators] = Field(default_factory=dict)


class DescriptionBackoff(BaseModel):
    """Failure tracking so a permanently failing session stops being retried.

    Closes the negative-caching gap in the lazy `_describe` path, where a
    missing binary or unparseable transcript means every invocation re-spawns a
    doomed subprocess forever.
    """

    failures: int = 0
    last_attempt: datetime = Field(default_factory=_now)


class StepOutcome(BaseModel):
    step: StepName
    ok: int = 0
    failed: int = 0
    detail: str | None = None


class PassReport(BaseModel):
    pass_id: str
    status: PassStatus = "ok"
    started_at: datetime = Field(default_factory=_now)
    finished_at: datetime | None = None
    projects: int = 0
    tasks: int = 0
    steps: list[StepOutcome] = Field(default_factory=list)
    errors: list[str] = Field(default_factory=list)
    notifications: list[str] = Field(default_factory=list)
    pruned: list[str] = Field(default_factory=list)
    # "<action>: <task_id> (<event>)" per action this pass actually ran.
    actions: list[str] = Field(default_factory=list)

    @property
    def duration_seconds(self) -> float | None:
        if self.finished_at is None:
            return None
        return (self.finished_at - self.started_at).total_seconds()


class SyncState(BaseModel):
    """Durable state carried between passes."""

    last_pass: PassReport | None = None
    # Edge-trigger memory: key -> last observed value. Key shape is
    # "<project>/<task_id>:<signal>", e.g. "alpha/alpha-1:pr-state".
    last_seen: dict[str, str] = Field(default_factory=dict)
    # session_id -> backoff record.
    description_backoff: dict[str, DescriptionBackoff] = Field(default_factory=dict)
    # Rate-limit memory for `[sync.on]` actions: key -> when it last ran. Key
    # shape is "<project>/<task_id>:<event>:<action>", which shares `last_seen`'s
    # prefix so the dead-state sweep drops both the same way.
    action_runs: dict[str, datetime] = Field(default_factory=dict)
