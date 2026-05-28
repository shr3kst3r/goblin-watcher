# docs/AGENTS.md

Documentation conventions for `goblin-watcher`.

## Two kinds of docs, separated by directory

- **`docs/adrs/`** — Architecture Decision Records. Point-in-time decisions. Append-only history. To update a decision, write a new ADR that supersedes the old one; do not rewrite history. Filename: `NNNN-kebab-title.md`, numbered sequentially.
- **`docs/designs/`** — Current-state designs. Living descriptions of features, architectural patterns, workflows, system boundaries. Edit in place as the system changes. One file per surface (e.g. `sessions-and-windowing.md`).

## When agents must update these

- New decision (framework choice, data-model shape, integration policy) → write an ADR.
- New or materially changed system surface (e.g. a new windower, a new agent integration, a new task source) → update or create the relevant design doc.
- Stale design after a refactor → edit the design doc in the same PR as the code.

## What does not belong here

- Investigation notes, branch-local planning, copied logs, one-off agent transcripts — those go in `.context/` (gitignored).
- Auto-generated API references — leave to code.
- Roadmap or status — owned elsewhere (issue tracker).

## ADR template

```markdown
# NNNN. Decision title

- Status: accepted (or: proposed, superseded by NNNN)
- Date: YYYY-MM-DD

## Context
What forced the decision? What constraints, deadlines, or tradeoffs framed it?

## Decision
What did we decide?

## Consequences
What changes because of this? What's now easier or harder?

## Alternatives considered
Briefly — and why they were rejected.
```
