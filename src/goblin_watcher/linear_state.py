"""TTL-cached refresh of a task's Linear workflow state.

Shared by `gw status` (lazy, on the render path) and the background sync engine,
so both honour the same TTL and write through the same narrow patch. One fetch
per Linear-backed task is slow with many tasks; the cache timestamp on the task
is what keeps subsequent reads off the network.
"""

from __future__ import annotations

from datetime import UTC, datetime

from goblin_watcher import config, secrets, state
from goblin_watcher.errors import GoblinError
from goblin_watcher.linear import LinearClient
from goblin_watcher.models import Project, Task

DEFAULT_TTL_SECONDS = 300


class LinearStateFetcher:
    """Lazy-constructed Linear client that fetches state with cached fallback.

    The client is built on first use; if API-key resolution fails, the fetcher
    permanently disables itself for this run so the caller still completes.
    Per-issue fetch errors fall back to the cached state silently — which is
    also how a scheduled sync degrades when `op://` secrets can't be resolved
    from a non-interactive context.
    """

    def __init__(self) -> None:
        self._client: LinearClient | None = None
        self._disabled = False

    def _client_or_none(self) -> LinearClient | None:
        if self._disabled:
            return None
        if self._client is None:
            try:
                api_key = secrets.get_linear_api_key()
                self._client = LinearClient(api_key)
            except GoblinError:
                self._disabled = True
                return None
        return self._client

    @property
    def disabled(self) -> bool:
        return self._disabled

    def refresh(self, project: Project, task: Task) -> Task:
        """Fetch the latest Linear state for `task` and persist it. No-op on failure."""
        if task.linear is None:
            return task
        if not self.cache_expired(task):
            return task
        client = self._client_or_none()
        if client is None:
            return task
        try:
            fresh = client.fetch_issue_state(task.linear.identifier)
        except GoblinError:
            return task

        # Persist even when the state is unchanged: the timestamp is what keeps
        # the next read inside the TTL window. Narrow patch under the task lock
        # (ADR 0004) — the fetch above took a network round-trip, so `task` may
        # already be stale.
        def _patch(latest: Task) -> Task:
            if latest.linear is None:
                return latest
            return latest.model_copy(
                update={
                    "linear": latest.linear.model_copy(update={"state": fresh}),
                    "linear_state_updated_at": datetime.now(UTC),
                }
            )

        try:
            return state.update_task(project, task.id, _patch)
        except GoblinError:
            return task

    @staticmethod
    def cache_expired(task: Task) -> bool:
        ts = task.linear_state_updated_at
        if ts is None:
            return True
        if ts.tzinfo is None:
            ts = ts.replace(tzinfo=UTC)
        try:
            ttl = int(config.load().defaults.linear_state_ttl_seconds)
        except Exception:
            ttl = DEFAULT_TTL_SECONDS
        return (datetime.now(UTC) - ts).total_seconds() >= ttl

    def close(self) -> None:
        if self._client is not None:
            self._client.close()
