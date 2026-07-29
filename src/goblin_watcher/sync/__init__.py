"""Background sync (ADR 0005).

A sync *pass* is a short-lived, idempotent sweep over every registered project
and task: refresh what interactive commands would otherwise refresh lazily,
cache the derived git/PR indicators, prune what is safely prunable, and notify
on state transitions. Periodic execution belongs to the host scheduler
(launchd/cron) — there is no resident process.
"""

from goblin_watcher.sync.models import (
    PassReport,
    StepOutcome,
    SyncState,
    TaskIndicators,
)

__all__ = ["PassReport", "StepOutcome", "SyncState", "TaskIndicators"]
