# Linear Integration

Current-state design of how `goblin-watcher` reads from Linear.

## Surface area

Read-only by default. Two read operations are exposed today; everything else (comments, status changes) is **not** wired up to avoid surprising the team.

| Operation | Where | Status |
|---|---|---|
| Fetch issue by identifier | `gw new --linear ENG-123`, `gw ENG-123` | Implemented |
| Post a comment with the PR URL | `gw pr open --notify-linear` | Documented in safety, **not yet implemented** (placeholder prints `--notify-linear: Linear comment posting lands in a follow-up`) |

The boundary is stated in root AGENTS.md's safety section: Linear is read-only **unless** `--notify-linear` is passed explicitly.

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

## Client lifecycle

`linear/client.LinearClient` wraps an `httpx.Client`. Two construction modes:

- **Self-owned** (`LinearClient(api_key)`): creates an internal `httpx.Client` with `timeout=15.0` and closes it via the `__exit__` / `close()` path.
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
- **No caching.** We refetch every time. The plan reserved `<project>/.goblin/cache/linear/<id>.json` for later; not implemented.
- **No issue mutation.** Even `--notify-linear` is on hold until the team confirms how PR-link comments should be formatted.
- **No async.** `httpx.Client` (sync) is plenty fast for one query per ticket invocation.

## Code map

- `src/goblin_watcher/linear/__init__.py` — re-exports `LinearClient`, `parse_identifier`.
- `src/goblin_watcher/linear/client.py` — `LinearClient`, identifier parsing, error mapping.
- `src/goblin_watcher/linear/queries.py` — the single GraphQL query.
- `src/goblin_watcher/secrets.py` — `get_linear_api_key`, `resolve_op_reference`.
- `src/goblin_watcher/commands/new.py` — `_resolve_or_register_linear_project`, `_from_linear`.
- `src/goblin_watcher/commands/doctor.py` — `_linear_key_check`.

## Tests

- `tests/test_linear_client.py` — happy path, 401, GraphQL errors, not-found, empty-key rejection. Uses `pytest_httpx`.
- `tests/test_secrets.py` — env precedence, config literal, `op://` resolution (mocked), missing-op handling.
- `tests/test_cli_linear_flow.py` — full CLI flow with team-matched project, with `--repo` auto-register, and via the `gw <LINEAR-ID>` shortcut.
