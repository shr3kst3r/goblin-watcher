# 0001. Adopt OP agent-readiness standards

- Status: accepted
- Date: 2026-05-18

## Context

`goblin-watcher` is a Python CLI built to be a daily-driver for AI coding agents (claude, codex, gemini). Its own working tree should be an exemplar of the agent-readiness practices the OP team applies to other repos — short curated `AGENTS.md`, durable docs separated from scratch, gitignored `.context/`, and CI parity with local checks.

The first run of `/op-dev:assess-agent-readiness` produced a `not-ready` result driven by structural items (missing `@AGENTS.md` import in `CLAUDE.md`, no `docs/` folder, `.context/` not gitignored, a broken `uv sync --dev` in the README). The standard's Required-Floor items were the right rubric to align to.

## Decision

Adopt the OP agent-readiness standard verbatim:

- Root `AGENTS.md` is human-curated, ~100–150 lines, with purpose, architectural role, conventions, verification commands, safety boundaries, and "what not to refactor."
- Root `CLAUDE.md` starts with `@AGENTS.md` and adds only narrow Claude-specific lines below.
- Durable docs under `docs/` with ADR/design separation. Scratch under `.context/` (gitignored).
- Local `just verify` mirrors the CI workflow step-for-step.

## Consequences

- Future contributors (human or agent) drop into a known shape: read root `AGENTS.md`, read the linked design under `docs/designs/`, run `just verify`.
- Decisions get ADRs (this is the first). Designs evolve in place under `docs/designs/`.
- Scratch — assessment outputs, plan files, investigation notes — stops accidentally bleeding into tracked history.

## Alternatives considered

- **Skip docs/ and keep everything in `AGENTS.md`.** Rejected: AGENTS.md is for cognition-budget-preserved guardrails, not long-form designs or historical decisions.
- **One mega `DECISIONS.md`.** Rejected: hard to diff, hard to supersede individual entries, and breaks the standard's append-only ADR model.
