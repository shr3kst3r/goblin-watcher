# 0009. Work modes are a named registry, not one boolean flag each

- Status: accepted
- Date: 2026-08-13

## Context

ADR 0006 settled what a work mode *is*: an alternate seed template rendered
through `build_seed_prompt`, sharing the same task-context slots, a property of
the session rather than the task. It did not settle how a mode is *selected*,
and the answer that emerged by default was "one boolean flag per mode, plus a
hand-written check against every other one".

By the time ADR 0008 landed, `commands/new.py` carried this for two modes:

- `--adversarial-review` forces `--agent claude`, rejects `--prompt`, rejects
  `--no-launch`, and conflicts with `--research`.
- `--research` requires a tracking item, rejects `--no-launch`, and conflicts
  with `--adversarial-review`.

ADR 0008 recorded the same pressure from the other side — `gw run` reached three
mode flags and a conflict check that had become a list — and explicitly deferred
to issue #24 rather than adding a fourth bespoke flag.

Three facts frame the decision:

**The conflict matrix is quadratic and the constraints are not.** Each new mode
adds one conflict check per existing mode, but the *interesting* part of a mode
is a handful of properties: which brief it renders, whether it pins an agent,
whether `--prompt` composes, whether it needs a tracking item. Those properties
are already data; only the dispatch was code.

**Two shapes exist and both are load-bearing.** `--research` renders a template.
`--adversarial-review` seeds a literal string, because Claude Code's
slash-command parser only fires when the command is the entire user message —
ADR 0006 examined that and kept it rather than converting it. Any registry has
to express both, or it re-creates the special case it was meant to remove.

**Users want modes gw will never ship.** A team's "spike", "write the RFC", or
"port this to the new API" brief is a prompt, not a feature. Every one of them
currently requires patching `new.py`.

Against that: `AGENTS.md` says the agent registry is static and rules out entry
points and plugin discovery. Whatever this is, it must not become one.

## Decision

Work modes live in a **named registry** in a new top-level `modes` module, and
`gw new --mode <name>` is how one is selected. The boolean flags stay as
**aliases** — `--research` is `--mode research`, `--adversarial-review` is
`--mode adversarial-review` — so nothing breaks for existing users or scripts.

A mode is a `ModeSpec`: exactly one of `template` (a brief rendered with the
shared task-context slots) or `seed` (a literal first message used verbatim),
plus optional `agent` (pins the agent), `requires_ticket`, `focus_lead`, and
`summary`. Users add their own under `[modes.<name>]` in `config.toml`; an entry
whose name matches a built-in replaces it whole, mirroring how a project's
`setup.toml` replaces the global `[setup]` table.

Four constraints come with it:

1. **`--mode` is a single value, and that is the only conflict check.** Two
   different modes is one error, whatever modes exist. Naming the same mode
   twice (`--mode research --research`) is allowed — the user said one thing
   twice, not two things.

2. **Every other check reads a field, never a name.** No consumer branches on
   `"adversarial-review"`. `--prompt` is refused when `spec.allows_prompt` is
   false, which is *derived* from the shape (a seed mode has no `{focus}` slot
   to put a prompt in) rather than configured, so a user's own seed mode inherits
   the refusal for free. `--no-launch` is refused for any mode, since no mode has
   an effect when no session starts.

3. **A template is resolved, not executed.** A bare name matching a packaged
   template wins; anything else is the user's own file, `~`-expanded and
   resolved against the config directory. An unknown `{slot}` in a user template
   is a `GoblinError` naming the slots gw fills, not a `KeyError` traceback.

4. **This is a registry, not a plugin system.** Nothing is discovered from
   entry points or scanned from a directory. A mode contributes prompt text and
   four booleans-worth of policy; it cannot contribute code. The `AGENTS.md`
   prohibition on plugin discovery is about behaviour, and a mode has none.

Malformed specs are validated at `resolve()` time rather than in a Pydantic
validator, so a broken `[modes.foo]` breaks `gw new --mode foo` and nothing else.

## Consequences

**Easier.** Adding a mode is a dict entry, and the next one costs no validation
code at all. Users get modes gw does not ship, which is where most of the demand
is — a team's house brief is a prompt, not a feature request. Issue #28's ticket
classification has a seam to hang off: it picks a mode name, and everything
downstream already works.

**Harder.** Prompt templates are now a supported config surface, so a slot
rename in `spawn_prompt.md`'s sibling briefs can break a user's file we never
see. The failure is at least loud (a `GoblinError` naming the valid slots) and
late (only when that mode is used). The registry also has to be documented for
users, which the flags never did.

**Accepted.** `gw run` keeps its three boolean flags for now. `--address-review`
(ADR 0008) needs a *data-gathering step* before rendering — the review feed —
which `ModeSpec` deliberately does not express, since expressing it would mean
letting a mode name code to run. Converting `gw run` is follow-up work, and the
honest cost of this ADR is that mode selection is a registry on `gw new` and
still a flag matrix on `gw run` until then.

**Accepted.** A user mode can pin an agent that isn't installed, or render a
brief that is nonsense. gw validates the shape, not the content — the same
bargain ADR 0006 made about the read-only boundary.

## Alternatives considered

- **Keep one flag per mode and just accept the matrix.** Rejected on the
  evidence: two modes on `gw new` and three on `gw run` had already produced
  seven validation branches, and ADR 0008 had to record the debt rather than pay
  it.

- **Built-in modes only, no user table.** Cheaper and safe, and it is what the
  issue's first sentence asks for. Rejected because it leaves the actual demand
  unserved — every house-specific brief still means patching `new.py` — while
  costing almost nothing extra to support, since a mode is data either way.

- **Let a mode name a command to run before rendering** (which would let
  `--address-review` become a mode). Rejected: that is the plugin system
  `AGENTS.md` forbids, arriving through a config file. If a mode needs code, the
  code belongs in gw and the mode belongs in `BUILTIN_MODES`.

- **Make `--research` and `--adversarial-review` hidden, or remove them.**
  Rejected: they are documented in the README, ADR 0006, and ADR 0008, and they
  cost one dict entry each to keep. Deprecating them is a separate decision with
  no forcing function behind it.

- **Persist the chosen mode on `Task`.** Rejected for the reason ADR 0006 gives:
  the mode is a property of the session, and a research session routinely turns
  into an implementation session on the same task.
