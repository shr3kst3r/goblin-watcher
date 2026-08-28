# 0014. Pre-accept the agent's first-run workspace-trust prompt

- Status: accepted
- Date: 2026-08-28

## Context

Claude Code gates the first run in any directory behind a dialog:

```
Quick safety check: Is this a project you created or one you trust?
  ❯ No, exit
    Yes, I trust this folder
```

For a person who works in three or four checkouts, that is a once-a-quarter
keystroke. For `gw` it is a keystroke on **every task**, because a fresh
directory the agent has never seen is precisely what `gw` produces: a worktree
under `<project>/.worktrees/<branch>`, a multi-repo workspace under the XDG data
dir, a scratch space under `~/goblin/scratch/`. Verified against the local
install: 195 project entries in `~/.claude.json`, 100 of them one-shot `gw`
directories.

Three facts pin the shape of any fix.

**No flag or env var skips it.** `--dangerously-skip-permissions` does not —
confirmed empirically at claude 2.1.250, where the dialog appears with the flag
set — and nothing in the CLI's env surface (`CLAUDE_CONFIG_DIR`,
`CLAUDE_CODE_*`) touches it. `claude config` was removed, so there is no
supported command to set it either.

**The persisted answer is a documented key.** Trust lives in claude's global
config at `projects["<absolute dir>"].hasTrustDialogAccepted`, and setting it by
hand is the remedy claude's *own* error messages name, verbatim: "Run Claude
Code in that folder once and accept the trust dialog, or set
`projects[...].hasTrustDialogAccepted: true` in `~/.claude.json`." Seeding the
key ahead of launch was verified to suppress the dialog outright.

**It is the last piece of friction that survives `unsafe = true`.** `gw` is
built for parallel autonomous agents, and `defaults.unsafe` is already true
(AGENTS.md). A headless fleet run started from launchd meets this dialog with
nobody at the terminal to answer it.

Cutting the other way: `~/.claude.json` is not `gw`'s file. It holds claude's
onboarding state and its OAuth account, and `gw`'s own safety boundary says
never write outside `<project>/.goblin/`, `<project>/.worktrees/`, the user's
XDG dirs, or the working tree. This is a deliberate exception to that list, and
the trust dialog is a security gate, not a nag.

## Decision

**`gw` pre-accepts the trust prompt for the directory it is about to launch in,
as a capability the agent declares.**

Four parts.

1. **`Agent.pretrust_workspace(cwd) -> bool`** joins `transcripts` and
   `supports_remote_control` as a per-agent declaration, for the same reason
   both of those exist: the answer varies by agent and a `agent.name ==
   "claude"` check at the call site would go stale the moment a fifth CLI grows
   one. The agent owns the write because it owns the file format. It returns
   True only when it actually recorded something, so the already-trusted case —
   every resume, and every launch after the first in a directory — is a pure
   read.

2. **`workspace_trust.apply` is the config gate and the fail-open wrapper**, and
   `launcher.launch` is its only call site, because `launch` is the one place a
   session starts: `gw new`, `gw run`, `gw scratch`, interactive and headless,
   all route through it. `apply` never raises. The dialog is answerable by hand
   in two keystrokes, so a bookkeeping write that failed does not get to veto
   the session the user asked for — the same posture as
   `linear_transitions.apply` (ADR 0012) and `classify.advise` (ADR 0011).

3. **`defaults.trust_workspaces` is `true`.** Unlike `[linear.transitions]` and
   `[sync.on]`, which are opt-in because they act on systems other people can
   see, this one only tells a local CLI something the user has already decided:
   `gw` made the directory itself, from a repo they named, on the machine they
   are sitting at. Off by default would mean the feature exists and the friction
   stays.

4. **The write is conservative in three specific ways.** It never creates
   `.claude.json` — a missing file means claude has never run here, and
   inventing the file its auth lives in is not `gw`'s business. It rewrites the
   whole document under a `gw` lock (ADR 0004) on a `.claude.json.gw.lock`
   sidecar, so parallel `gw` launches do not lose each other's keys. And it
   seeds both the literal `cwd` and its `resolve()`d form when they differ,
   because node resolves symlinks in `process.cwd()` and a path reached through
   a symlinked parent is keyed on the physical one.

Only claude implements it. codex, gemini, antigravity, and managed return False
with a comment saying why — an honest declaration, in the spirit of
`TranscriptCapability(parseable=False, reason=...)`.

## Consequences

**Easier.** A new task launches straight into work. A headless fleet run started
from launchd no longer has a way to stall before its first turn on a dialog no
one will see. `gw scratch`, which creates a brand-new directory every single
time and is therefore the worst case, stops asking entirely.

**Harder.** `gw` now writes to a file another program owns, which AGENTS.md's
safety boundary otherwise forbids. That exception is documented there and is
narrow: one boolean, one key, per directory `gw` itself created.

**Accepted — a residual write race.** claude does not take `gw`'s lock, so a
claude write landing between this read and this write would be lost. The window
is one small mutation wide and only opens on the *first* launch in a directory;
every subsequent one returns without writing. The alternative — not touching the
file — is the status quo this ADR exists to end.

**Accepted — trust is granted without a human in the loop.** That is the point,
and it is bounded by what `gw` will pre-trust: the directory a task is launching
in, which `gw` created from a repo the user pointed it at. `gw new --dir`
adopting a checkout the user already has is the same posture. Anyone who wants
the gate back sets `trust_workspaces = false`.

**Accepted — the key is reverse-engineered, not a public API.** If claude
renames it, `gw` silently stops helping and the dialog comes back: a return to
today's behaviour, not a broken launch. That is why the wrapper fails open and
why nothing downstream reads the result.

## Alternatives considered

- **Do nothing; tell users to edit `~/.claude.json` by hand.** Rejected. The
  friction is per *task*, and `gw`'s whole reason for existing is producing
  tasks in bulk. Hand-editing a 450 KB JSON file once per worktree is not a
  workflow.

- **Shell out to `claude config set`.** Would have been the safe route, using
  claude's own locking. The subcommand no longer exists (2.1.250 parses `claude
  config list` as a prompt).

- **A `[setup]` run step that seeds the key** (ADR 0007). Rejected on two
  counts: it is per project, so scratch spaces and workspaces are not covered,
  and it would put a `~/.claude.json` edit in user-supplied shell — exactly the
  arbitrary-command surface `[sync.on]` refuses to become.

- **Gate it behind opt-in config, defaulting off.** Rejected, per decision point
  3. The comparison is not to `[linear.transitions]` but to `defaults.unsafe`,
  which this repo already ships as `true` for the same "built for parallel
  autonomous agents" reason.

- **A `pretrust_workspace` no-op resolved by `agent.name == "claude"` in the
  launcher.** Rejected; the name check AGENTS.md forbids for windowers and modes,
  and ADR 0013 already declined for remote control.

- **Merge only the one key with a partial write instead of rewriting the
  document.** There is no partial write for JSON. A jq-style surgical patch would
  mean shelling out to a tool `gw` does not depend on, to shrink a race window
  that only opens once per directory.
