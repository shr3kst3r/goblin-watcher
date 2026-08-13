"""Shared helpers for turning per-request token counts into `UsageBucket`s.

Both claude and codex report usage per model request; gw stores it collapsed to
one row per (model, local calendar day) so a session's record stays small no
matter how many requests it took. The day is *local*, because the question this
feature exists to answer — "what did today cost" — is asked in the timezone the
person is sitting in.
"""

from __future__ import annotations

from datetime import UTC, date, datetime

from goblin_watcher.models import UsageBucket

_FIELDS = (
    "input_tokens",
    "output_tokens",
    "cache_read_tokens",
    "cache_write_tokens",
    "cache_write_1h_tokens",
)


def local_day(value: object) -> date | None:
    """Local calendar day for an ISO-8601 timestamp, or None if unparseable.

    Timestamps without an offset are read as UTC, which is what both agents
    write (`...Z`).
    """
    if not isinstance(value, str) or not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed.astimezone().date()


class BucketAccumulator:
    """Sums token counts into (model, day) buckets, preserving first-seen order."""

    def __init__(self) -> None:
        self._rows: dict[tuple[str | None, date | None], dict[str, int]] = {}

    def add(
        self,
        *,
        model: str | None = None,
        day: date | None = None,
        input_tokens: int = 0,
        output_tokens: int = 0,
        cache_read_tokens: int = 0,
        cache_write_tokens: int = 0,
        cache_write_1h_tokens: int = 0,
    ) -> None:
        counts = {
            "input_tokens": input_tokens,
            "output_tokens": output_tokens,
            "cache_read_tokens": cache_read_tokens,
            "cache_write_tokens": cache_write_tokens,
            "cache_write_1h_tokens": cache_write_1h_tokens,
        }
        if not any(counts.values()):
            return
        row = self._rows.setdefault((model, day), dict.fromkeys(_FIELDS, 0))
        for name, value in counts.items():
            row[name] += value

    def buckets(self) -> list[UsageBucket]:
        return [
            UsageBucket(model=model, day=day, **counts)
            for (model, day), counts in self._rows.items()
        ]


def as_int(value: object) -> int:
    """Coerce a transcript field to a non-negative int; 0 for anything odd."""
    if isinstance(value, bool) or not isinstance(value, int | float):
        return 0
    return max(0, int(value))
