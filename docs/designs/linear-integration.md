# Linear Integration

Current-state design of how `goblin-watcher` reads from — and, when asked, writes to — Linear.

## Surface area

Read-only by default. Two writes exist, and each is unreachable until the user turns it on.

| Operation | Where | Status |
|---|---|---|
| Fetch issue by identifier | `gw new --linear ENG-123`, `gw ENG-123` | Implemented |
| Refresh an issue's workflow state | `gw status`, `gw sync` (TTL-cached, `linear_state.py`) | Implemented |
| Post a comment with the PR URL | `gw pr open --notify-linear` | Write · opt-in per invocation (`LinearClient.create_comment`) |
| Move the issue's workflow state | session start and PR open, via `[linear.transitions]` | Write · opt-in via config (`LinearClient.update_issue_state`, ADR 0012) |

The boundary is stated in root AGENTS.md's safety section: Linear is read-only **unless** `--notify-linear` is passed explicitly, or a `[linear.transitions]` key is set.

## Authentication

Resolved by `secrets.get_linear_api_key()`. Order:

1. `LINEAR_API_KEY` env var (literal `lin_api_...` token).
2. `config.linear.api_key` in `~/.config/goblin-watcher/config.toml`. Either a literal value or an `op://vault/item/field` reference.
3. If the configured value starts with `op://`, shell out to `op read <ref>`:
   - Missing `op` binary on PATH → `MissingDependencyError` with the install hint.
   - `op read` non-zero exit → `LinearAuthError` carrying the underlying message.
   - Empty stdout → `LinearAuthError`.
4. Nothing resolved → `LinearAuthError` with a hint about both knobs.

The resolved key is **never** persisted or logged. It exists only in the `LinearClient` instance for the duration of one `_post` call.

## GraphQL query

`linear/queries.FETCH_ISSUE`:

```graphql
query GoblinFetchIssue($team: String!, $number: Float!) {
  issues(filter: {team: {key: {eq: $team}}, number: {eq: $number}}) {
    nodes {
      id identifier title description url
      state { name }
      team { key }
    }
  }
}
```

**Why this query and not `issue(id: $id)`:**

Linear's GraphQL `issue(id:)` field takes a UUID, not the human-readable `ENG-123` identifier. We don't have the UUID up front — only the team key + number. The `issues(filter:{...})` form does the lookup server-side and returns at most one node.

`client.parse_identifier("ENG-123")` splits the input via regex (`^[A-Z][A-Z0-9_]*-\d+$`, case-insensitive). The team-key half goes into `$team`; the numeric half is cast to `float` because Linear's `IntegerComparator` uses GraphQL `Float`.

A missing node (`nodes == []`) → `GoblinError("Linear issue ENG-123 not found.")`. We never re-fetch with a different filter; if the user typo'd the team key or pulled an issue from a team they don't have access to, the error makes that obvious.

## State transitions (`[linear.transitions]`)

Tickets go stale at two moments gw is synchronously present for. Both are opt-in and unset by default (ADR 0012):

```toml
[linear.transitions]
on_session_start = "In Progress"   # gw new (below --no-launch), gw run (fresh and resume)
on_pr_open       = "In Review"     # gw pr open, after the PR URLs are printed
timeout_seconds  = 8.0             # wall-clock cap on the two round-trips
```

`linear_transitions.apply(project, task, trigger)` is the single entry point, and all three call sites (`commands/new.py`, `commands/run.py`, `commands/pr.py`) are one line each. What it does, in order:

1. No ticket on the task, or no config key for this trigger → return, having touched nothing. This is the default path.
2. `FETCH_ISSUE_WORKFLOW` — one query returning the issue's internal id, its current state, and every state its **own team** defines.
3. Already in the target state (case-insensitive) → no write. Resuming a session all day doesn't churn the ticket's activity feed.
4. Name not in the team's workflow → one muted line naming the states that *are* available; no write.
5. `UPDATE_ISSUE_STATE` (`issueUpdate(id:, input: {stateId:})`), then a success line and a write-back of the new state into `Task.linear.state` / `Task.linear_state_updated_at` — so `gw status` doesn't keep rendering the state gw just moved away from.

**`apply` never raises.** A missing API key, an unreachable or slow Linear, an unconfirmed mutation, a malformed response — each prints `Skipped Linear transition on <trigger>: …` and returns the task unchanged. The agent still launches; the PR is already open. This is the same fail-open posture as `classify.advise`, and for the same reason: the hook sits between the user and the thing they actually asked for.

`Trigger` is a `Literal["on_session_start", "on_pr_open"]` whose values *are* the config keys, so a trigger is looked up rather than branched on. A third moment would be a new key plus a call site.

## Client lifecycle

`linear/client.LinearClient` wraps an `httpx.Client`. Two construction modes:

- **Self-owned** (`LinearClient(api_key)`): creates an internal `httpx.Client` with `timeout=15.0` and closes it via the `__exit__` / `close()` path. The `timeout=` parameter overrides that default; `linear_transitions` passes its own, shorter cap.
- **Injected** (`LinearClient(api_key, client=...)`): tests pass `pytest_httpx`'s mocked client. The mocked client is not closed by `__exit__`.

`__enter__` / `__exit__` exist solely so `with LinearClient(...) as c:` reads cleanly in `commands/new.py`. There is no async path.

## Error model

| HTTP / GraphQL state | Raised |
|---|---|
| Connection error, DNS failure, timeout | `GoblinError("Linear API request failed: ...")` |
| HTTP 401 | `LinearAuthError("Linear API rejected the credentials (401).")` |
| Other HTTP ≥ 400 | `GoblinError("Linear API returned <code>: <body excerpt>")` |
| GraphQL `errors[]` populated | `GoblinError("Linear API error: <joined messages>")` |
| `data == null` | `GoblinError("Linear API returned no data.")` |
| Issue not found (empty nodes) | `GoblinError("Linear issue X-N not found.")` |

The top-level CLI handler in `cli.main` renders these via Rich (`Error: ...` + `Hint: ...`) and exits with the error's `exit_code`.

## Team key → project resolution

For `gw <LINEAR-ID>` and `gw new --linear ...`, the project to use comes from a mapping on the Project record:

```python
class Project:
    linear_team_key: str | None  # set via `gw project new --team ENG`
```

Resolution order in `commands/new._resolve_or_register_linear_project`:

1. `--project <name>` if given.
2. The first registered project where `linear_team_key.upper() == team_key.upper()`.
3. `--repo <url>` → clone + auto-register a new project named after the lowercased team key, with `linear_team_key` set.
4. Otherwise `GoblinError`.

A team can be served by multiple projects (e.g. one repo per service in a monorepo team), in which case the user should pass `--project` to disambiguate. We don't try to be clever about multi-project teams.

## What we deliberately don't do

- **No webhook listener / pull loop.** The integration is poll-on-demand. The user types `gw ENG-123` and we fetch.
- **No unprompted mutation.** Both writes require the user to ask: a flag per invocation for the comment, a config key for the state moves. Nothing gw does by default changes anything in Linear.
- **No transition on merge or prune.** The two implemented triggers are moments gw is synchronously present for and can report on. A background `gw sync` pass writing to the tracker is a different decision and hasn't been made.
- **No state creation.** A configured name that the team's workflow doesn't define is reported, never created.
- **No async.** `httpx.Client` (sync) is plenty fast for one query per ticket invocation.

## Code map

- `src/goblin_watcher/linear/__init__.py` — re-exports `LinearClient`, `parse_identifier`.
- `src/goblin_watcher/linear/client.py` — `LinearClient`, identifier parsing, error mapping.
- `src/goblin_watcher/linear/queries.py` — the GraphQL documents (two queries for reads, one for the workflow lookup, two mutations).
- `src/goblin_watcher/linear_state.py` — TTL-cached workflow-state refresh for `gw status` / `gw sync`.
- `src/goblin_watcher/linear_transitions.py` — the opt-in state moves and their fail-open wrapper.
- `src/goblin_watcher/secrets.py` — `get_linear_api_key`, `resolve_op_reference`.
- `src/goblin_watcher/commands/new.py` — `_resolve_or_register_linear_project`, `_from_linear`.
- `src/goblin_watcher/commands/doctor.py` — `_linear_key_check`.

## Tests

- `tests/test_linear_client.py` — happy path, 401, GraphQL errors, not-found, empty-key rejection, the workflow lookup and the state mutation. Uses `pytest_httpx`.
- `tests/test_linear_transitions.py` — unset config makes no request; the move and its cache write-back; case-insensitive matching; already-in-state skips the write; unknown state, auth failure, API failure and an unconfirmed mutation all warn and continue; `gw run` still launches when Linear is down.
- `tests/test_secrets.py` — env precedence, config literal, `op://` resolution (mocked), missing-op handling.
- `tests/test_cli_linear_flow.py` — full CLI flow with team-matched project, with `--repo` auto-register, and via the `gw <LINEAR-ID>` shortcut.
