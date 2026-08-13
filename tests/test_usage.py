"""Pricing resolution, cost math, and rollup shapes for token accounting."""

from __future__ import annotations

from datetime import date

from goblin_watcher import config, usage
from goblin_watcher.config import CostConfig, ModelPricing
from goblin_watcher.models import UsageBucket


def _cfg(**cost: object) -> config.Config:
    return config.Config(cost=CostConfig(**cost))


def test_resolves_a_known_alias() -> None:
    pricing = usage.resolve_pricing("claude-opus-5", _cfg())
    assert pricing is not None
    assert (pricing.input, pricing.output) == (5.0, 25.0)


def test_resolves_context_window_suffix_and_dated_snapshot() -> None:
    # `claude --model claude-opus-5[1m]` and a dated haiku snapshot both bill at
    # their family's rate; neither string is a table key.
    suffixed = usage.resolve_pricing("claude-opus-5[1m]", _cfg())
    dated = usage.resolve_pricing("claude-haiku-4-5-20251001", _cfg())
    assert suffixed is not None and suffixed.input == 5.0
    assert dated is not None and dated.input == 1.0


def test_prefix_match_requires_a_dash_boundary() -> None:
    assert usage.resolve_pricing("claude-opus-51000", _cfg()) is None


def test_unknown_model_has_no_price() -> None:
    assert usage.resolve_pricing("gpt-5-codex", _cfg()) is None
    assert usage.resolve_pricing(None, _cfg()) is None


def test_config_pricing_overrides_the_built_in_table() -> None:
    cfg = _cfg(pricing={"claude-opus-5": ModelPricing(input=1.0, output=2.0)})
    pricing = usage.resolve_pricing("claude-opus-5", cfg)
    assert pricing is not None
    assert (pricing.input, pricing.output) == (1.0, 2.0)


def test_config_pricing_can_price_a_model_gw_ships_no_rate_for() -> None:
    cfg = _cfg(pricing={"gpt-5-codex": ModelPricing(input=1.25, output=10.0)})
    bucket = UsageBucket(model="gpt-5-codex", input_tokens=1_000_000, output_tokens=1_000_000)
    rolled = usage.rollup([bucket], cfg)
    assert rolled.unpriced_tokens == 0
    assert rolled.cost_usd == 11.25


def test_cost_prices_each_token_class_at_its_own_rate() -> None:
    # 1M of each class on a $5/$25 model: input 5, output 25, cache read
    # 5 * 0.1, 5m write 5 * 1.25, 1h write 5 * 2.
    bucket = UsageBucket(
        model="claude-opus-5",
        input_tokens=1_000_000,
        output_tokens=1_000_000,
        cache_read_tokens=1_000_000,
        cache_write_tokens=1_000_000,
        cache_write_1h_tokens=1_000_000,
    )
    rolled = usage.rollup([bucket], _cfg())
    assert rolled.cost_usd == 5 + 25 + 0.5 + 6.25 + 10
    # The two cache-write TTLs are billed apart but reported together.
    assert rolled.cache_write_tokens == 2_000_000
    assert rolled.total_tokens == 5_000_000


def test_cache_multipliers_are_configurable() -> None:
    bucket = UsageBucket(model="claude-opus-5", cache_read_tokens=1_000_000)
    assert usage.rollup([bucket], _cfg(cache_read_multiplier=0.5)).cost_usd == 2.5


def test_unpriced_tokens_are_counted_but_cost_nothing() -> None:
    rolled = usage.rollup(
        [
            UsageBucket(model="claude-opus-5", output_tokens=1_000_000),
            UsageBucket(model="gpt-5-codex", output_tokens=2_000_000),
        ],
        _cfg(),
    )
    assert rolled.output_tokens == 3_000_000
    assert rolled.cost_usd == 25
    assert rolled.unpriced_tokens == 2_000_000
    note = usage.unpriced_note(rolled)
    assert note is not None and "lower bound" in note


def test_no_note_when_everything_was_priced() -> None:
    rolled = usage.rollup([UsageBucket(model="claude-opus-5", output_tokens=10)], _cfg())
    assert usage.unpriced_note(rolled) is None


def test_by_day_orders_oldest_first_and_parks_undated_last() -> None:
    buckets = [
        UsageBucket(model="claude-opus-5", day=date(2026, 8, 12), output_tokens=1_000_000),
        UsageBucket(model="claude-opus-5", day=date(2026, 8, 10), output_tokens=2_000_000),
        UsageBucket(model="claude-opus-5", output_tokens=3_000_000),
        UsageBucket(model="claude-haiku-4-5", day=date(2026, 8, 10), output_tokens=1_000_000),
    ]
    per_day = usage.by_day(buckets, _cfg())
    assert list(per_day) == [date(2026, 8, 10), date(2026, 8, 12), None]
    # Same-day buckets from different models merge, each priced on its own rate.
    assert per_day[date(2026, 8, 10)].output_tokens == 3_000_000
    assert per_day[date(2026, 8, 10)].cost_usd == 50 + 5


def test_empty_rollup_reports_itself_empty() -> None:
    assert usage.rollup([], _cfg()).is_empty
    assert usage.badge(usage.EMPTY) == ""


def test_fmt_tokens_scales_units() -> None:
    assert usage.fmt_tokens(512) == "512"
    assert usage.fmt_tokens(34_000) == "34.0K"
    assert usage.fmt_tokens(4_200_000) == "4.2M"


def test_fmt_cost_marks_estimates_and_sub_cent_amounts() -> None:
    priced = usage.rollup([UsageBucket(model="claude-opus-5", output_tokens=1_000_000)], _cfg())
    assert usage.fmt_cost(priced) == "~$25.00"
    tiny = usage.rollup([UsageBucket(model="claude-opus-5", output_tokens=1)], _cfg())
    assert usage.fmt_cost(tiny) == "<~$0.01"
    unpriced = usage.rollup([UsageBucket(model="gpt-5-codex", output_tokens=1_000)], _cfg())
    assert usage.fmt_cost(unpriced) == "cost n/a"
