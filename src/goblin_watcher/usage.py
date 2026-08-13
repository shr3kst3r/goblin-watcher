"""Token accounting: roll `UsageBucket`s up into totals and estimated cost.

`gw` is the only thing that knows about all N parallel sessions at once, which
makes it the only thing that can answer "what did today cost". The token counts
come from the agents' own transcripts (see `agents/claude.py`,
`agents/codex.py`); this module turns them into totals, dollar estimates, and
the short strings the CLI prints.

Cost is an **estimate against public list prices** and nothing more. It does
not model subscription plans (where marginal cost is zero), negotiated rates,
introductory pricing, batch discounts, or fast mode. Read it as "what these
tokens would have cost at API rates" — which is the number the "is it worth
running four agents on this" decision actually needs.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from datetime import date

from goblin_watcher import config
from goblin_watcher.config import ModelPricing
from goblin_watcher.models import SessionRecord, Task, UsageBucket

# List price in USD per million tokens, as published 2026-06-24. Overridable
# per model via `[cost.pricing."<model>"]` in config.toml — which is also how
# you price a model that isn't here (codex's `gpt-*` models ship unpriced
# because gw has no vendored rate for them; their tokens are still counted).
DEFAULT_PRICING: dict[str, ModelPricing] = {
    "claude-fable-5": ModelPricing(input=10.0, output=50.0),
    "claude-mythos-5": ModelPricing(input=10.0, output=50.0),
    "claude-opus-5": ModelPricing(input=5.0, output=25.0),
    "claude-opus-4-8": ModelPricing(input=5.0, output=25.0),
    "claude-opus-4-7": ModelPricing(input=5.0, output=25.0),
    "claude-opus-4-6": ModelPricing(input=5.0, output=25.0),
    "claude-sonnet-5": ModelPricing(input=3.0, output=15.0),
    "claude-sonnet-4-6": ModelPricing(input=3.0, output=15.0),
    "claude-haiku-4-5": ModelPricing(input=1.0, output=5.0),
}


@dataclass(frozen=True)
class Rollup:
    """Summed token counts plus the cost they add up to.

    `unpriced_tokens` is the honest-accounting field: tokens whose model has no
    price entry are counted in the token totals but contribute $0, so a rollup
    with `unpriced_tokens > 0` is a *lower bound* on cost. Callers say so.
    """

    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_tokens: int = 0
    cache_write_tokens: int = 0
    cost_usd: float = 0.0
    unpriced_tokens: int = 0

    def __add__(self, other: Rollup) -> Rollup:
        return Rollup(
            input_tokens=self.input_tokens + other.input_tokens,
            output_tokens=self.output_tokens + other.output_tokens,
            cache_read_tokens=self.cache_read_tokens + other.cache_read_tokens,
            cache_write_tokens=self.cache_write_tokens + other.cache_write_tokens,
            cost_usd=self.cost_usd + other.cost_usd,
            unpriced_tokens=self.unpriced_tokens + other.unpriced_tokens,
        )

    @property
    def total_tokens(self) -> int:
        return (
            self.input_tokens
            + self.output_tokens
            + self.cache_read_tokens
            + self.cache_write_tokens
        )

    @property
    def is_empty(self) -> bool:
        return self.total_tokens == 0


EMPTY = Rollup()


def resolve_pricing(model: str | None, cfg: config.Config | None = None) -> ModelPricing | None:
    """Price entry for `model`, or None when gw has no rate for it.

    Handles the two shapes agents actually write: a bare alias
    (`claude-opus-5`), a context-window-suffixed variant (`claude-opus-5[1m]`),
    and a dated snapshot (`claude-haiku-4-5-20251001`) — the last two resolve to
    their family's rate, which is the same price.
    """
    if not model:
        return None
    cfg = cfg or config.load()
    table = {**DEFAULT_PRICING, **cfg.cost.pricing}
    key = model.split("[", 1)[0].strip()
    if key in table:
        return table[key]
    # Longest prefix wins so `claude-opus-4-8-2026...` never resolves against a
    # shorter, cheaper family key.
    candidates = [name for name in table if key.startswith(f"{name}-")]
    if not candidates:
        return None
    return table[max(candidates, key=lambda name: len(name))]


def price_bucket(bucket: UsageBucket, cfg: config.Config | None = None) -> Rollup:
    """One bucket as a `Rollup`, priced if gw knows the model's rate."""
    cfg = cfg or config.load()
    tokens = Rollup(
        input_tokens=bucket.input_tokens,
        output_tokens=bucket.output_tokens,
        cache_read_tokens=bucket.cache_read_tokens,
        cache_write_tokens=bucket.cache_write_tokens + bucket.cache_write_1h_tokens,
    )
    pricing = resolve_pricing(bucket.model, cfg)
    if pricing is None:
        return Rollup(
            input_tokens=tokens.input_tokens,
            output_tokens=tokens.output_tokens,
            cache_read_tokens=tokens.cache_read_tokens,
            cache_write_tokens=tokens.cache_write_tokens,
            unpriced_tokens=tokens.total_tokens,
        )
    c = cfg.cost
    dollars = (
        bucket.input_tokens * pricing.input
        + bucket.output_tokens * pricing.output
        + bucket.cache_read_tokens * pricing.input * c.cache_read_multiplier
        + bucket.cache_write_tokens * pricing.input * c.cache_write_multiplier
        + bucket.cache_write_1h_tokens * pricing.input * c.cache_write_1h_multiplier
    ) / 1_000_000
    return Rollup(
        input_tokens=tokens.input_tokens,
        output_tokens=tokens.output_tokens,
        cache_read_tokens=tokens.cache_read_tokens,
        cache_write_tokens=tokens.cache_write_tokens,
        cost_usd=dollars,
    )


def rollup(buckets: Iterable[UsageBucket], cfg: config.Config | None = None) -> Rollup:
    cfg = cfg or config.load()
    total = EMPTY
    for bucket in buckets:
        total = total + price_bucket(bucket, cfg)
    return total


# ---------------------------------------------------------------------------
# Rollups over gw's own records


def for_session(session: SessionRecord, cfg: config.Config | None = None) -> Rollup:
    return rollup(session.usage, cfg)


def for_task(task: Task, cfg: config.Config | None = None) -> Rollup:
    cfg = cfg or config.load()
    total = EMPTY
    for session in task.sessions:
        total = total + for_session(session, cfg)
    return total


def by_day(
    buckets: Iterable[UsageBucket], cfg: config.Config | None = None
) -> dict[date | None, Rollup]:
    """Rollups keyed by calendar day, oldest first. `None` collects undated buckets."""
    cfg = cfg or config.load()
    out: dict[date | None, Rollup] = {}
    for bucket in buckets:
        out[bucket.day] = out.get(bucket.day, EMPTY) + price_bucket(bucket, cfg)
    dated = sorted(d for d in out if d is not None)
    ordered: dict[date | None, Rollup] = {d: out[d] for d in dated}
    if None in out:
        ordered[None] = out[None]
    return ordered


# ---------------------------------------------------------------------------
# Formatting


def fmt_tokens(n: int) -> str:
    """Compact token count: `512`, `34.0K`, `4.2M`."""
    if n < 1_000:
        return str(n)
    if n < 1_000_000:
        return f"{n / 1_000:.1f}K"
    return f"{n / 1_000_000:.1f}M"


def fmt_cost(r: Rollup) -> str:
    """Estimated cost, `~` prefixed so it never reads as a billed figure."""
    if r.cost_usd == 0 and r.unpriced_tokens:
        return "cost n/a"
    if 0 < r.cost_usd < 0.01:
        return "<~$0.01"
    return f"~${r.cost_usd:,.2f}"


def fmt_tokens_line(r: Rollup) -> str:
    """`1.2M in · 340.0K out · 8.4M cache read · 210.0K cache write`."""
    parts = [
        f"{fmt_tokens(r.input_tokens)} in",
        f"{fmt_tokens(r.output_tokens)} out",
    ]
    if r.cache_read_tokens:
        parts.append(f"{fmt_tokens(r.cache_read_tokens)} cache read")
    if r.cache_write_tokens:
        parts.append(f"{fmt_tokens(r.cache_write_tokens)} cache write")
    return " · ".join(parts)


def badge(r: Rollup) -> str:
    """Rich-markup suffix for a `gw status --cost` task/project line. '' when empty."""
    if r.is_empty:
        return ""
    return f"  [muted]{fmt_cost(r)} · {fmt_tokens_line(r)}[/]"


def compact_badge(r: Rollup) -> str:
    """Cost plus a single token total — for per-session lines, where the full
    breakdown would repeat the task line above it. '' when empty."""
    if r.is_empty:
        return ""
    return f"{fmt_cost(r)} · {fmt_tokens(r.total_tokens)} tok"


def unpriced_note(r: Rollup) -> str | None:
    """A one-line caveat when some tokens had no rate, or None."""
    if not r.unpriced_tokens:
        return None
    return (
        f"{fmt_tokens(r.unpriced_tokens)} tokens came from models gw has no price for, "
        "so the cost shown is a lower bound. Add rates under [cost.pricing] "
        "(`gw config edit`)."
    )
