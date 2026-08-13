# Token usage and cost

How `gw` answers "what did today cost" across N parallel agent sessions.

## Why it lives here

Each agent already writes its own token usage into its transcript, but each one
only knows about itself. `gw` is the only thing that knows about every session on
every task on every project at once, which makes it the only thing that can total
them. That total is what the "is it worth running four agents on this" decision
needs; without it the decision is made on vibes.

## Where the numbers come from

Nothing is measured or estimated by gw, and no model is called. Both readable
transcript formats carry per-request usage:

- **claude** — each assistant record's `message.usage` (`input_tokens`,
  `output_tokens`, `cache_read_input_tokens`, and a `cache_creation` breakdown by
  cache TTL), with `message.model` and the record's `timestamp`.
- **codex** — `event_msg.token_count` events carrying `info.total_token_usage`,
  with the model taken from the preceding `turn_context`.

`gemini` and `antigravity` report nothing: gw can't read their transcripts, so
their sessions carry no usage rather than a misleading zero.

Two parsing hazards, both handled in the agent modules and covered by tests:

- **claude repeats usage per content block.** One assistant turn is written to
  the JSONL once per content block, and every record repeats the *same*
  cumulative `usage` object. Summing them naively over-counts a session by ~2x
  on real transcripts, so records are deduplicated by `message.id`.
- **codex reports cumulative totals.** Each `token_count` event restates the
  session's running total, so a turn's own usage is the delta against the
  previous event. A total that moves backwards (a resumed or re-based rollout) is
  treated as a fresh baseline instead of a negative turn. Differencing means the
  buckets add back up to exactly the last event's totals; `cached_input_tokens`
  is a *subset* of `input_tokens`, so only the remainder is charged at the full
  input rate.

## Storage: buckets on the session record

`SessionRecord.usage` is a list of `UsageBucket` — one row per (model, local
calendar day):

```json
{"model": "claude-opus-5", "day": "2026-08-13", "input_tokens": 108,
 "output_tokens": 65100, "cache_read_tokens": 20400000,
 "cache_write_tokens": 12000, "cache_write_1h_tokens": 417000}
```

Three consequences of that shape:

- **Per model**, because rates are per model, and a session can switch models
  mid-conversation (or spawn a subagent on a cheaper one).
- **Per day**, because that's what makes a "what did today cost" rollup possible
  without re-walking every transcript. The day is *local* — the question is asked
  in the timezone the person is sitting in.
- **Cache writes split by TTL**, because the 5-minute and 1-hour caches are
  billed at different multiples of the input rate (1.25x and 2x). They're
  summed for display but priced apart.

Buckets are parsed on the same transcript pass that refreshes a session's rolling
summary (`sessions.refresh_summary`), so accounting adds no extra file reads and
inherits the existing `defaults.summary_ttl_seconds` freshness rules and the
narrow-patch write path from ADR 0004 (`usage` is one of the summary-owned
fields). Background sync refreshes summaries the same way
(`sync.engine` → `refresh_task_summaries` → `persist_refresh`), so a scheduled
`gw sync` keeps token counts current without anyone running a command.

`gw history --cost` reads recorded state only — it never parses a transcript
itself, which keeps it cheap but means it reflects whatever the last `gw status`,
`gw session refresh`, or sync pass wrote.

## Pricing: a vendored table, overridable

`usage.DEFAULT_PRICING` holds list prices in USD per million tokens, keyed by
model id, with `[cost.pricing]` from config.toml merged over the top. Lookup
resolves the two shapes agents actually write beyond a bare alias: a
context-window suffix (`claude-opus-5[1m]`) and a dated snapshot
(`claude-haiku-4-5-20251001`), both of which fall back to their family's rate.

Two deliberate positions:

- **Cost is an estimate against public list prices, and says so.** Every figure
  is `~`-prefixed and every surface carries the caveat. It does not model
  subscription plans (where the marginal cost of another session is zero),
  negotiated rates, introductory pricing, batch discounts, or fast mode. It
  answers "what would these tokens have cost at API rates", which is the right
  input to a fan-out decision even on a flat-rate plan.
- **Unknown models are counted, not guessed.** A model with no rate contributes
  its tokens to the counts and `$0` to the cost, and the rollup carries
  `unpriced_tokens` so surfaces can say the total is a lower bound. Codex's
  `gpt-*` models ship unpriced for exactly this reason: gw has no vendored
  OpenAI rate, and a fabricated one would be worse than an honest gap. Adding
  `[cost.pricing."gpt-5-codex"]` closes it.

## Surfaces

| Command | Shows |
| --- | --- |
| `gw session show <id>` | one session's token breakdown, its cost, and which models it used |
| `gw status --cost` | the same tree, with a badge on every session, task, and project, plus a grand total |
| `gw history --cost [--days N]` | a day-by-day table across every project (default window: 14 days; `0` = all time) |

`gw status` without `--cost` is unchanged — no dollar figures appear unless
asked for.
