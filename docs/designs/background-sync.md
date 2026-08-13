# Background Sync

Current-state design of `gw sync` and the locking it depends on. Decisions are
recorded in ADR 0004 (advisory locking) and ADR 0005 (sync as a scheduled
command); this document describes what exists.

## Shape: a scheduled command, not a daemon

There is no resident process. `gw sync run` performs one short-lived,
idempotent pass and exits; a launchd agent fires it on an interval. Every firing
executes the currently installed `gw`, so upgrades take effect on the next tick
and a crashed pass is simply retried by the next one.

```
launchd (StartInterval)  ──▶  gw sync run  ──▶  one pass  ──▶  exit
                                    │
                    ┌───────────────┼────────────────┐
                    ▼               ▼                ▼
              task records    indicator cache    journal + notifications
              (narrow patches)  (sync/…json)     (logs/sync.jsonl)
```

## The pass

Guarded by a machine-wide single-instance lock (`sync/pass.lock`, acquired with
`timeout=0`): an overlapping firing exits `skipped` rather than doubling work.
Failure is isolated at two levels, because an unattended job must never wedge:

- A **`GoblinError` in one step** is journaled and the pass continues to the next
  step. One unreachable remote cannot stop the other tasks from refreshing. The
  pass ends `partial`.
- An **unexpected exception on one task** (a bug, a corrupt record) is caught at
  the task boundary, journaled as `task-crashed`, and the remaining tasks still
  run. The pass ends `error` and exits non-zero, so it is loud in the launchd log
  without costing every other task its refresh.

Only a failure outside the per-task loop aborts the pass; that is journaled as
`pass-crashed` and still recorded as `last_pass`, so `gw sync status` shows the
crash rather than the last successful run.

Per project, then per task (iterating `task.all_repos()` for multi-repo tasks):

| # | Step | Notes |
|---|---|---|
| 1 | `git fetch` | Skipped for scratch and for local-only projects (no `repo_url`) |
| 2 | Linear state | TTL-gated via `linear_state.LinearStateFetcher`, shared with `gw status` |
| 2b | GitHub issue state | `github-issue` step; TTL-gated via `github_state.refresh`, also shared with `gw status` |
| 3 | Reconcile + summaries | Discovery outside the lock, plan applied inside |
| 4 | Descriptions | Invoked inline, with failure backoff |
| 5 | PR state + CI checks | Read from the pass's batched lookup (below); drives `Task.status` transitions |
| 6 | Indicator cache | Uncommitted / ahead / PR / checks written to the sidecar cache |
| 7 | Prune | Merged **and** clean only; never `--force` |
| 8 | Notifications | Edge-triggered; see below |

An unexpected (non-`GoblinError`) exception is *not* isolated: it is journaled as
`pass-crashed`, recorded as the last pass with `status: error`, and re-raised, so
a bug exits 1 into the launchd log instead of quietly halving a pass. Skipped
passes are journaled too — a wedged holder of the single-instance lock shows up
as a run of `pass-skipped` records rather than silence.

### PR lookups are batched per repo, not per task

Steps 5 and 7 both need a task's PR state, and asking `gh` per task made a pass
cost three GitHub round-trips for every PR-bearing task — `pr_state`,
`pr_checks`, then `pr_state` again from `merge_detection` inside prune. At a
five-minute interval that scaled linearly with task count and never decayed.

`_collect_pr_snapshots` now runs once per project, before the task loop, and
returns a `gh.PrSnapshot` per PR URL that steps 5 and 7 both read. Step 7 passes
its snapshot to `merge_detection(…, snapshot=…)`, which is what stops the prune
step re-asking. The mapping is *total* over PR-bearing tasks — a lookup that
produced nothing still gets an all-`None` snapshot — so "already asked, got no
signal" stays distinguishable from "no PR here", and a failed batch falls
through to ancestry rather than fanning back out into per-task calls.

Three tiers, cheapest first:

- A task already recorded as `merged` is not looked up at all. `MERGED` is the
  only terminal PR state and a merged PR's CI result is not actionable, so its
  `checks` indicator is dropped rather than frozen. This matters because merged
  tasks accumulate: prune refuses to delete one with a dirty worktree, and those
  used to poll forever. `CLOSED` is deliberately *not* treated as terminal — a
  closed PR can be reopened, and it rides along in the batch at no extra cost.
- github.com PRs are grouped by repo and fetched with `gh.pr_snapshots`, one
  aliased GraphQL query per repo (chunked at 100 PRs). GitHub charges **one**
  rate-limit point per query regardless of how many PRs it names, and
  `statusCheckRollup { state }` returns a pre-aggregated
  SUCCESS/PENDING/FAILURE, so there is no per-check payload to walk.
- Anything else — a GitHub Enterprise host, a URL shape `gh` accepts but
  `gh.parse_pr_url` doesn't recognise — falls back to the per-PR
  `pr_state` + `pr_checks` pair, which is what every PR used before batching.

Measured on a real 50-task registry: 36 rate-limit points per pass became 3, and
the cost is now O(repos with unlanded PRs) rather than O(tasks).

`gh api graphql` exits non-zero when *any* alias fails to resolve while still
returning every other alias's data, so `pr_snapshots` parses stdout regardless of
the return code and lets unresolvable PRs fall out as absent entries.

### What prune refuses to touch

`destroy_task` force-deletes every repo's branch (`git branch -D`), so an
unattended prune has to be stricter than the interactive one. A merged task is
left alone, and reported via the `prunable` notification, when:

- any worktree has uncommitted changes, or
- on a multi-repo task (ADR 0003), a secondary repo's branch still carries
  commits its base branch does not have — `merge_detection` only inspects the
  primary. A secondary sitting on its base has nothing unique to lose and does
  not block the prune.

Pruning also drops the task's cache row, edge-trigger keys, and description
backoff, so derived state does not outlive the record it describes.

### Why the indicator cache is a sidecar

Derived git/PR facts live in `sync/indicators.json`, keyed `<project>/<task_id>`
— not on the task record. Task JSON stays owned by the interactive flows, so the
hottest, most contended files don't get hotter, and regenerable data stays out of
durable records. `gw status` reads the cache when it is fresher than twice the
sync interval and otherwise recomputes live, so behaviour is unchanged when sync
was never installed. A cached reading always renders with its age (`↑2 unpushed
(3m)`); `gw status --no-cache` forces a live recompute.

### The live dashboard reads it, and nothing else

`gw status --watch` redraws the tree every `--interval` seconds (default 2). What
makes that affordable is that a tick is confined to local reads: task JSON, the
indicator cache, transcript mtimes, and headless pid files. Everything a
one-shot `gw status` does that costs a round-trip or a subprocess is off —
Linear and GitHub issue refresh, LLM description spawns, session reconciliation
— and a summary refresh only takes the task lock when it actually changed
something, since otherwise a 2-second poll would rewrite identical JSON forever.
That's the division of labour the cache was built for: sync pays the network
cost on its own schedule, and the dashboard renders what sync left behind.

The consequence to know about: a session started outside `gw` is adopted by a
sync pass or by a plain `gw status`, never by a watch tick.

`gw status --active` narrows the tree to tasks with work in flight — a session
whose transcript moved within `defaults.activity_grace_seconds` (default 900),
or a live headless pid. The grace window is much wider than the 120s behind the
`● active` badge on purpose: an agent that stops to ask a question goes quiet
within two minutes, which is precisely when it most needs to stay on screen. The
filter runs *before* the ticket refresh, so `--active` is faster than a full
status, not just shorter.

### Edge-triggered notifications

The pass compares each signal against the last value recorded in
`sync/state.json` and fires only on change, so an event is announced exactly
once and a quiet day produces zero notifications:

- `agent-idle` — a session's transcript stopped being written (active → idle only)
- `pr-merged` — PR state became `MERGED`
- `parent-merged` — a task stacked on this one (`Task.parent_task`) needs a rebase,
  because the branch under it just landed. Rides the parent's `pr-merged` edge, so
  it inherits once-per-transition for free and reaches the children while the
  parent record still exists (step 5 runs before the prune in step 7). The
  notification names the *child* — that's the branch with work to do.
- `checks-failed` / `checks-passed` — CI rollup flipped
- `prunable` — a merged branch that can't be auto-pruned because it's dirty

Transports: `macos` (osascript), `command` (user argv, title and body appended,
never shell-interpolated), or `off`. `auto` resolves to `macos` on darwin. Every
notification is journaled regardless of transport, so `gw sync watch` shows what
happened even when delivery is disabled.

### Self-healing sweep

Derived state is keyed `<project>/<task_id>`, but sync only clears it for tasks
*it* prunes. `gw task rm`, `gw new --rm`, or a hand-deleted record would otherwise
leave rows behind forever. Because gw task ids come from Linear tickets and branch
names, ids get reused — and a recreated task inheriting the dead one's rows would
render the old task's indicators in `gw status` and, worse, inherit its
edge-trigger memory, silently suppressing its first notification.

So a **full-scope pass owns the whole keyspace**: anything not visited during the
pass is dropped at the end (cached indicators, `last_seen` keys, description
backoff for vanished sessions), journaled as `swept-dead-state`. A `--project`
pass never sweeps — it only saw one project. Discarding a row we shouldn't have
costs one recompute on the next pass, which is the asymmetry that makes this safe.

### Description backoff

The lazy `_describe` path has no negative caching: a failure leaves
`description_updated_at` untouched, so every subsequent gw invocation re-spawns a
doomed subprocess forever. Sync tracks failures per session in
`SyncState.description_backoff` and, after three, retries only hourly.

Failure is measured, not read off the exit code: `description.apply` returns 0
both for a real refresh and for a graceful give-up (LLM unreachable, transcript
unparseable) — which is precisely the case the backoff exists for. The engine
re-reads the task after its attempts and treats an unmoved
`description_updated_at` as a failure.

## Observability

`logs/sync.jsonl` is the single source of truth — one JSON record per step
outcome, notification, and error, tagged with a `pass_id`. Degradations that
would otherwise be invisible (`gh` absent, Linear key unresolvable in a
non-interactive launchd context) are journaled once per pass, not per task.
Four surfaces sit on top:

- **`gw sync`** — one foreground pass, narrating each action as it happens.
- **`gw sync watch`** — follows the journal live (handles rotation/truncation).
- **`gw sync status`** — doctor-style table: schedule installed and loaded (with
  the interval read from the plist, which `--interval` can set independently of
  config), last and next run, Linear key, `gh`, notification transport, prune
  posture.
- **`gw sync prune-journal --days N`** — trims the journal, same shape as
  `gw history prune`. A pass every few minutes forever is otherwise an unbounded
  file.

`gw doctor` carries a `background sync` row. "Not scheduled" stays advisory —
sync is opt-in. An *installed* job is held to a higher bar and fails doctor when
it isn't actually running: launchd never loaded the plist, the plist's
`StartInterval` is unreadable, or the last pass finished more than three
intervals ago (the plist's mtime standing in for "installed at" when no pass has
run yet, since `RunAtLoad` is off). A job that quietly stopped firing is
otherwise indistinguishable from one with nothing to report.

The plist bakes in the installing shell's `PATH`. launchd hands a job only
`/usr/bin:/bin:/usr/sbin:/sbin`, so without it a scheduled pass loses `gh` (PR
state, CI checks) and `op` (1Password secrets) while still reporting `ok`.

## Locking (ADR 0004)

Sync adds a periodic concurrent writer, which is only safe because state writes
are now serialized. `locks.exclusive` takes an advisory `fcntl.flock` on a stable
sidecar (`.<task_id>.lock` beside task JSON, `state.lock` in the data dir) —
never the data file itself, whose inode is replaced by every atomic write.

`state.update_task` / `state.update_global` hold that lock **across the read**,
so the caller's mutation always applies to current on-disk state. Callers pass
narrow patches touching only the fields they own; persisting a whole stale `Task`
is the bug this exists to prevent. Expensive work — network calls, transcript
parsing, agent-store discovery — happens outside the lock, which is why
reconciliation is split into `plan_reconciliation` (discovery) and
`apply_reconciliation` (cheap, pure).

## Configuration

```toml
[sync]
interval_seconds = 300     # also the worst-case staleness of cached indicators
prune = true               # merged + clean tasks; never forces
scratch_prune_days = 0     # 0 disables; scratch has no merge signal, only idleness
notify = "auto"            # auto | macos | command | off
notify_command = []        # argv; title and body appended
notify_events = ["agent-idle", "pr-merged", "parent-merged", "checks-failed", "checks-passed", "prunable"]
```

## Files

```
$XDG_DATA_HOME/goblin-watcher/
  sync/
    state.json        ← last pass, edge-trigger memory, description backoff
    indicators.json   ← derived per-task git/PR facts
    pass.lock         ← single-instance guard
  logs/
    sync.jsonl        ← the journal
    sync.launchd.log  ← stdout/stderr of scheduled passes
  state.lock          ← registry lock
<project>/.goblin/tasks/
  .<task_id>.lock     ← per-task lock (excluded from the `*.json` task glob)
```

## Tests

- `tests/test_locks.py` — lock contention across processes, `update_task`
  re-reading, interleaved narrow patches.
- `tests/test_sync_engine.py` — full passes over real git repos with `gh` and the
  notifier faked: PR transitions, edge-trigger once-only, prune safety (including
  the multi-repo guard), backoff, crash and skip bookkeeping, and the batched PR
  lookup — one query per repo, no second fetch inside prune, no fetch at all for
  an already-merged task.
- `tests/test_sync_cache.py` — `gw status` preferring, ageing out, and bypassing
  the indicator cache.
- `tests/test_cli_status_watch.py` — what `--active` counts as in flight (grace
  window, live headless pid, configured override), and that a `--watch` tick
  makes no network call, schedules no description, and writes no state when
  nothing moved. The loop is driven by patching `time.sleep` to raise
  `KeyboardInterrupt`, so the real Ctrl-C exit path is what's under test.
- `tests/test_sync_store.py`, `test_sync_journal.py`, `test_sync_notify.py`,
  `test_sync_launchd.py`, `test_cli_sync.py`, `test_gh_pr_checks.py`,
  `test_gh_pr_snapshots.py`.

Nothing in the suite invokes real `launchctl`, `osascript`, `gh`, or the network.
