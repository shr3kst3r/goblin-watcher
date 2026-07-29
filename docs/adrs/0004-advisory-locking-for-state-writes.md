# 0004. Advisory per-file locking for state writes

- Status: accepted
- Date: 2026-07-28

## Context

Every state write in `gw` is a whole-document read-modify-write:
`state.load_task` → mutate in memory → `state.save_task`. The atomic
temp-file-plus-`Path.replace()` write guarantees no torn files, but nothing
serializes concurrent writers, so overlapping updates are last-writer-wins.
These lost-update races exist today, without any background process:

- `agents/launcher.py` saves a pre-dispatch session record, the agent runs for
  minutes to hours, then writes the *same stale task object* back — reverting
  every write any other process made during the session (Linear state, PR
  backfill, descriptions, session prunes).
- `gw status` writes each task up to three times from one snapshot loaded at
  the top of its loop; a background `gw _describe` completing anywhere in that
  window has its description silently reverted.
- Two concurrent `gw project new` invocations both load the global registry,
  add their key, and save — the loser's project vanishes from `state.json`.

The existing mitigation in `description.apply` (reload the task right before
saving, patch only the target fields) narrows the window but demonstrably does
not close it: the reload→save is itself an unsynchronized read-modify-write.

Background sync (ADR 0005) adds a periodic concurrent writer and would widen
exposure on every one of these paths. gw already orchestrates multiple
concurrent processes (parallel agents, detached describe subprocesses), so the
single-writer assumption baked into `state.py` no longer matches reality.

## Decision

Serialize state read-modify-write cycles with advisory `fcntl.flock` locks:

1. **Lock sidecar files, not the data files.** The atomic-write mechanism
   renames a temp file over the target, so a lock on the data file's inode
   would not survive the write. Each lockable resource gets a stable sidecar
   (`<task-file>.lock` next to task JSON; one lock in the data dir for
   `state.json`). Lock files are empty and never deleted.
2. **The lock spans the read.** `state.py` gains update helpers — e.g.
   `update_task(project, task_id, mutate)` and `update_global(mutate)` — that
   acquire the exclusive lock, re-read the file from disk, apply the caller's
   narrow mutation to the fresh object, save, and release. Locking only the
   write would fix nothing; the lost update happens between read and write.
3. **Writers that hold long-lived snapshots convert to narrow patches.** The
   launcher's post-agent write, `gw status`'s persists, `description.apply`,
   `session rm`/`prune`, PR backfill, and the registry mutators all move to
   the update helpers, writing only the fields they own instead of persisting
   a stale whole-task snapshot.
4. **Blocking acquire with a short timeout.** Locks are held for milliseconds
   (read + mutate + write of small JSON). Acquisition blocks up to a few
   seconds, then raises `GoblinError` — a held lock beyond that indicates a
   wedged process, and failing loudly beats deadlocking an interactive
   command.

`fcntl` is POSIX-only, which matches gw's supported platforms (macOS, Linux).
Plain `save_task` remains for creation of records that do not exist yet.

## Consequences

- Lost-update races between interactive commands, the launcher, describe
  subprocesses, and future background sync are closed, not narrowed.
- Every writer must adopt the update-helper pattern; persisting a stale
  snapshot becomes a code-review smell. The helpers are the seam that makes
  the discipline easy to follow.
- Empty `.lock` sidecar files appear next to task JSON and in the data dir.
  They are noise in `ls`, excluded from task discovery by the existing
  `*.json` glob, and covered by the `.git/info/exclude` entries.
- Callbacks passed to `update_task` must be cheap and side-effect-free
  (no network, no subprocesses) so lock hold times stay in milliseconds.
- Crash safety is unchanged: flock releases automatically when a process dies,
  so a crashed writer never leaves a stuck lock.

## Alternatives considered

- **Optimistic concurrency (version/etag field + compare-and-swap retry).**
  Rejected: requires a schema change to every record, a retry loop at every
  call site, and still needs a lock to make the compare-and-swap itself
  atomic. flock gets the same guarantee with less machinery.
- **Narrow reload-before-save everywhere, no locks.** Rejected: this is the
  `description.apply` pattern, and the race it leaves open (two reload→save
  cycles interleaving) is exactly the bug observed today.
- **SQLite.** Rejected previously and still: JSON files are diff-friendly and
  human-inspectable, and the storage design doc commits to them. Locking is a
  far smaller change than a storage engine swap.
- **A lock-server / single writer daemon.** Rejected: a resident process to
  serialize writes contradicts ADR 0005's whole premise and adds a lifecycle
  problem to solve a lifecycle problem.
