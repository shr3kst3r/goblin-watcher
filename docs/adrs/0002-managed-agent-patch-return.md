# 0002. Managed-agent execution returns a patch, not a push

- Status: proposed
- Date: 2026-05-22

## Context

Today every agent in `gw` (`claude`, `codex`, `gemini`) runs as a local subprocess inside a worktree, via `InlineWindower` or `TmuxWindower`. The agent reads and writes the user's filesystem directly. This is the right default for interactive work, but it ties session lifetime to the user's machine: the laptop has to stay awake, the agent competes for local CPU, and "fire and forget" workflows aren't really possible.

Anthropic's managed-agent API runs the agent in a remote sandbox instead. To do useful work it needs the project's source. Two ways to give it that:

1. **Push-back**: sandbox clones from `origin`, pushes a branch back when done. Requires write credentials inside Anthropic infrastructure.
2. **Patch-return**: sandbox receives the source (read-only clone, or an uploaded tarball) and returns a unified diff or `git format-patch` series. `gw` applies it locally.

The repos `gw` targets vary in trust posture, and several have no remote at all (`gw new --dir` adopts local-only checkouts). A push-back model needs both a remote *and* a token with write access scoped to the right branches — a non-trivial credential surface to delegate.

## Decision

For the first managed-agent integration, `gw` will:

1. Gate the managed agent on the project having a usable git remote (checked via `git.py`). Projects without one cannot select it; `gw doctor` surfaces this preemptively.
2. Open the project to the sandbox with read-only access — initial implementation: a read-only clone URL; tarball upload as a fallback for repos behind auth the sandbox can't reach. The sandbox never receives credentials that can push back to user repos.
3. Run the agent as a **long-lived, interactive remote session**. `gw run` attaches to it bidirectionally; detaching (closing the terminal, laptop sleep) leaves the agent running in the sandbox. A later `gw run <task>` reattaches via the session picker the same way it would resume a local claude session.
4. Surface diffs as a patch artifact at user-triggered checkpoints and at session end. `gw` writes the patch under `<project>/.goblin/patches/<session-id>-<checkpoint>.patch` and applies it to the task's worktree with `git apply` / `git am`.
5. Refuse to apply automatically when the local worktree is dirty or `HEAD` has moved since the patch was generated. Surface the patch path so the user can resolve manually.
6. Represent a managed run as a `SessionRecord` whose `session_id` is the remote session ID and whose lifecycle is decoupled from any particular attach. The record persists a last-seen-message offset so reattach can resume the transcript stream without replaying the whole conversation.

The managed agent slots in as a new `Agent` impl (`agents/managed.py`) plus a managed-API client that handles session create, attach (streaming turns in/out), checkpoint, and patch fetch. `read_transcript` streams from the API rather than tailing a local file. A new `Windower`-equivalent or a launcher branch will host the attach loop — exact shape decided at implementation time, but the `Windower` protocol's "decide where the agent runs" framing already covers remote.

## Consequences

- The credential surface stays narrow: Anthropic-side has read access at most, and never holds tokens that can push to user repos.
- The local worktree remains the source of truth. Conflicts surface as `git apply` failures the user already knows how to read.
- Managed mode is opt-in per task, naturally limited to repos that have somewhere to clone from. Local-only adoptions (`--dir` with no remote) continue to use local agents.
- A managed session outlives any individual `gw run` invocation. The agent keeps working through laptop sleep, network blips, and reboots — a property local agents cannot offer. The session picker treats managed sessions identically to local ones; the only visible difference is a remote-state badge.
- `read_transcript` for managed sessions streams from the API rather than tailing a JSONL file. Summary refresh stays lazy on read; the stale threshold still applies. The transcript path on `SessionRecord` becomes optional or repurposed as an API resource URL for managed runs.
- Tmux mode interacts cleanly: a managed session can occupy a pane just like a local one, with the attach loop driving stdio. `mark_idle` works if the API surfaces a quiet signal; otherwise it's a no-op for managed panes.
- Patch application is no longer tied to session end. Users can checkpoint mid-conversation to land a partial result on their branch, then keep iterating remotely. This is a UX gain over local agents, where there is no equivalent "snapshot the current state to my branch" affordance.

## Alternatives considered

- **Single-shot submit / patch / done.** Rejected: forces the user to specify the entire task up front and removes mid-task course correction. The interactive-but-detachable model gives us fire-and-forget *and* live iteration without picking one.
- **Push-back from the sandbox.** Rejected for v1: requires distributing a write-scoped token to Anthropic infrastructure, and offers no UX win over apply-local-patch for a single-developer workflow. Worth revisiting if managed agents start collaborating across multiple PRs in one run.
- **Stream the sandbox's filesystem back wholesale (rsync-style).** Rejected: noisier diff, harder to review, and loses the "this is a normal commit on your branch" property that `git am` gives us for free.
- **Allow managed mode on remote-less repos by uploading a tarball both ways.** Rejected for v1 on safety grounds: a tarball reply bypasses git's review affordances and we'd need to invent our own conflict resolution. Easier to require a remote and lean on `git apply`.
- **Skip managed agents entirely.** Rejected: the long-running / parallel-many use case is real, and we already have the agent-abstraction shape to absorb it cleanly.
