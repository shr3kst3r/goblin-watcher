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
| 5 | PR state + CI checks | `gh.pr_state` / `gh.pr_checks`; drives `Task.status` transitions |
| 6 | Indicator cache | Uncommitted / ahead / PR / checks written to the sidecar cache |
| 7 | Prune | Merged **and** clean only; never `--force` |
| 8 | Notifications | Edge-triggered; see below |

An unexpected (non-`GoblinError`) exception is *not* isolated: it is journaled as
`pass-crashed`, recorded as the last pass with `status: error`, and re-raised, so
a bug exits 1 into the launchd log instead of quietly halving a pass. Skipped
passes are journaled too — a wedged holder of the single-instance lock shows up
as a run of `pass-skipped` records rather than silence.

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

### Edge-triggered notifications

The pass compares each signal against the last value recorded in
`sync/state.json` and fires only on change, so an event is announced exactly
once and a quiet day produces zero notifications:

- `agent-idle` — a session's transcript stopped being written (active → idle only)
- `pr-merged` — PR state became `MERGED`
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

`gw doctor` carries an advisory row reporting whether sync is scheduled.

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
notify_events = ["agent-idle", "pr-merged", "checks-failed", "checks-passed", "prunable"]
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
  the multi-repo guard), backoff, crash and skip bookkeeping.
- `tests/test_sync_cache.py` — `gw status` preferring, ageing out, and bypassing
  the indicator cache.
- `tests/test_sync_store.py`, `test_sync_journal.py`, `test_sync_notify.py`,
  `test_sync_launchd.py`, `test_cli_sync.py`, `test_gh_pr_checks.py`.

Nothing in the suite invokes real `launchctl`, `osascript`, `gh`, or the network.
