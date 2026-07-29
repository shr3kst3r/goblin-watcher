# 0005. Background sync is a scheduled short-lived command, not a resident daemon

- Status: accepted
- Date: 2026-07-28

## Context

All freshness work in gw runs lazily on the blocking path of interactive
commands. A `gw status` render performs, per task: a Linear API round-trip
(15 s timeout worst case), two to three git subprocesses for the
uncommitted/unpushed indicators (recomputed every render — they have no
persisted representation at all), and per session a transcript re-parse (the
30 s summary TTL means effectively every invocation re-parses). The picker
path additionally runs session reconciliation, which for codex walks the
entire `~/.codex/sessions` store. Latency grows linearly with task count.

Nothing happens between invocations: no notifications when an agent goes
idle or a PR merges, no cleanup of merged tasks, no pre-warming of git
remotes. The one existing background mechanism — the detached `gw _describe`
subprocess — has structural defects (no in-flight guard, no negative caching,
foreground-clobber races) that any ad-hoc expansion would inherit.

`docs/designs/sessions-and-windowing.md` records the original stance: "a
background daemon would be overkill for an interactive CLI. Refreshing on
read covers the natural moments without a long-running process." Half of
that stance survives contact with this decision: we still want no resident
process. What changed is the volume of freshness work (Linear, PR state,
descriptions, indicators, reconciliation — none of which existed when that
line was written) and the demand for between-invocation behavior.

A resident daemon was considered and carries real costs for a `uv`-installed
CLI: lifecycle management, version skew after upgrades (a daemon keeps
running old code), IPC, and a long-lived process holding the Linear key.

ADR 0004 (advisory locking) is a prerequisite: a periodic background writer
without it amplifies every existing lost-update race.

## Decision

Add a `gw sync` command group. Sync is a **short-lived, idempotent,
single-pass command**; periodic execution is delegated to the host scheduler
(launchd on macOS). No resident process.

### The pass

`gw sync run` executes one pass over every registered project and task,
under ADR 0004 locking, with a non-blocking single-instance lock (a second
invocation while one is running exits 0 immediately). Each step is isolated:
a failure is journaled and the pass continues. Per task, iterating
`all_repos()` (ADR 0003):

1. **Git pre-warm** — `git fetch` per project root, so worktree creation and
   merge detection work against a fresh base.
2. **Linear state refresh** — same TTL-gated fetch `gw status` does inline
   today.
3. **Session reconciliation + summary refresh** — the work currently done on
   the picker/status path.
4. **Description refresh** — invoked inline (sync is already background),
   with failure backoff recorded in sync's own state so a persistently
   failing session stops being retried every pass (the negative-caching gap
   in the `_describe` path).
5. **PR state backfill + status transitions** — reusing the
   `gw task ls --refresh-prs` logic, plus a net-new CI-checks query
   (`gh pr checks` equivalent) — the first CI surface in gw.
6. **Indicator cache** — uncommitted/unpushed/ahead counts and PR/checks
   state are written to a **sidecar cache under the data dir**, not onto task
   JSON. Task files stay owned by the interactive flows; `gw status` reads
   the cache when fresh and falls back to live computation when it is
   missing or stale, showing the cache's age.
7. **Prune** — tasks that are merged **and** have clean worktrees are pruned
   automatically using the existing non-force teardown path. **Never
   `--force`**: dirty or ambiguous tasks are reported, not deleted. Scratch
   pruning only when an idle-days threshold is configured.
8. **Notifications** — emitted on **edge-triggered transitions only**, with
   last-seen state persisted so an event fires exactly once: agent went idle
   after activity, PR merged, CI checks flipped to failed/passed, task became
   prunable. Transports: macOS notification (default on darwin), a
   user-supplied command (argv template — e.g. a Slack webhook CLI), or off;
   per-event toggles in config. Edge-triggering keeps volume low — a quiet
   day produces zero notifications.

### Observability and control surface

Every pass appends structured JSONL to a sync journal, and a sync state file
records the last pass summary and per-event last-seen state. Three
subcommands sit on top:

- **`gw sync`** (bare) — one foreground pass, verbose: prints each action and
  each notification as it happens.
- **`gw sync watch`** — follows the journal live; the way to observe the
  scheduled background passes, including what they notified.
- **`gw sync status`** — installation and health: whether the launchd job is
  installed and loaded, last run time and outcome, next run, and per-component
  readiness (Linear key, `gh`, notification transport), reusing the doctor
  check pattern.

### Scheduling

`gw sync install` writes a launchd agent plist (`~/Library/LaunchAgents/`)
that runs `gw sync run` on an interval (config `sync.interval_seconds`,
default 300) and loads it; `gw sync uninstall` reverses it. Each firing
executes the currently installed gw, so upgrades take effect on the next
tick — the version-skew problem daemons have simply does not arise. On
non-macOS platforms, `install` prints the equivalent crontab line instead of
writing it.

## Consequences

- `gw status` becomes read-mostly: indicators, Linear state, and summaries
  come from caches bounded by the sync interval, with age shown. Worst-case
  staleness equals the interval; the fallback to live computation means an
  uninstalled sync leaves today's behavior intact.
- gw gains between-invocation behavior — notifications, cleanup, pre-warm —
  without a resident process. Failure mode of a wedged or dead scheduler job
  is stale data and silence, never corruption (passes are idempotent and
  locked).
- Background API traffic: one Linear fetch per task and a handful of `gh`
  calls per interval, still TTL-gated. Secrets resolved via `op://` may be
  unavailable in a non-interactive launchd context; sync degrades the same
  way `gw status` does today (self-disables Linear for the pass, journals it).
- Automatic pruning deletes merged-and-clean tasks without a prompt. This is
  a deliberate posture choice, bounded by the never-force rule and the clean-
  worktree guard; anything ambiguous is only reported.
- New moving parts to maintain: a launchd plist writer, a journal, a sidecar
  cache, a notification module, and gw's first CI-checks query.
- The lazy-refresh rationale in `sessions-and-windowing.md` is revised in the
  same PR to reflect this decision.

## Alternatives considered

- **Resident daemon (launchd KeepAlive, watchdog process).** Rejected:
  version skew after upgrades, lifecycle/IPC complexity, a long-lived holder
  of credentials — all to gain sub-interval reactivity that tmux `mark_idle`
  already covers for the interactive case.
- **Status quo (lazy refresh only).** Rejected: blocking-path latency grows
  with task count, and between-invocation behavior (notifications, cleanup)
  is impossible by construction.
- **Extend the `_describe` fire-and-forget pattern per feature.** Rejected:
  it has no in-flight guard, no negative caching, and no observability; N
  copies of that pattern is N copies of its defects.
- **Filesystem watcher (fswatch/FSEvents) for instant idle detection.**
  Rejected for v1: it reintroduces a resident process for one feature;
  revisit only if interval-granularity idle notifications prove too coarse.
- **Cache indicators on the Task JSON instead of a sidecar.** Rejected: it
  makes the hottest, most contended files hotter and puts derived,
  regenerable data in durable records.
