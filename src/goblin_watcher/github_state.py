"""TTL-cached refresh of a task's GitHub issue state.

The GitHub-issue counterpart to `linear_state`, shared by `gw status` (lazy, on
the render path) and the background sync engine so both honour the same TTL and
write through the same narrow patch. One `gh issue view` per issue-backed task is
slow with many tasks; the cache timestamp on the task is what keeps subsequent
reads off the network.

There is no client to keep alive — `gh` is a subprocess per call — so this is a
pair of functions rather than a fetcher object.
"""

from __future__ import annotations

from datetime import UTC, datetime

from goblin_watcher import config, gh, state
from goblin_watcher.errors import GoblinError
from goblin_watcher.models import Project, Task

DEFAULT_TTL_SECONDS = 300


def cache_expired(task: Task) -> bool:
    ts = task.github_issue_state_updated_at
    if ts is None:
        return True
    if ts.tzinfo is None:
        ts = ts.replace(tzinfo=UTC)
    try:
        ttl = int(config.load().defaults.github_issue_state_ttl_seconds)
    except Exception:
        ttl = DEFAULT_TTL_SECONDS
    return (datetime.now(UTC) - ts).total_seconds() >= ttl


def refresh(project: Project, task: Task) -> Task:
    """Fetch the latest issue state for `task` and persist it. No-op on failure.

    A missing `gh`, an unreachable API, or a deleted issue all read as "no
    signal": the cached state stays, and the timestamp is left untouched so the
    next pass retries rather than treating the failure as fresh data.
    """
    issue = task.github_issue
    if issue is None or not issue.repo:
        return task
    if not cache_expired(task):
        return task
    fresh = gh.issue_state(issue.repo, issue.number)
    if fresh is None:
        return task

    # Persist even when the state is unchanged: the timestamp is what keeps the
    # next read inside the TTL window. Narrow patch under the task lock (ADR
    # 0004) — the lookup above took a subprocess round-trip, so `task` may
    # already be stale.
    def _patch(latest: Task) -> Task:
        if latest.github_issue is None:
            return latest
        return latest.model_copy(
            update={
                "github_issue": latest.github_issue.model_copy(update={"state": fresh}),
                "github_issue_state_updated_at": datetime.now(UTC),
            }
        )

    try:
        return state.update_task(project, task.id, _patch)
    except GoblinError:
        return task
