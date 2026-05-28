# Managed-agent setup

Operator guide for wiring the managed agent (ADR 0002) end-to-end. This is forward-looking — the code in this repo today is scaffolding only (`agents/managed.py`, `NotConfiguredClient`); a real backend has to be implemented and configured before any of this is actually executable. This doc tells you what that backend has to provide and what secrets it needs.

## Deployment models

There is no off-the-shelf "Anthropic managed coding agent" service that the ADR's contract maps onto today. Anything you stand up will be one of three shapes:

1. **Self-hosted backend.** You operate a small service that boots sandboxes, runs Claude via the Claude API, exposes the `ManagedClient` Protocol over HTTP. `gw` talks to your service.
2. **Local-loop backend.** The `ManagedClient` implementation runs in-process inside `gw`, spawning sandboxes directly on the user's machine (Docker, devcontainer, or similar). Cheaper to ship, doesn't give you "session keeps running across laptop sleep" — the headline feature of the ADR — so this is really only useful as a development stepping-stone.
3. **Third-party hosted execution.** Some external service (yours or a vendor's) provides the `ManagedClient` API. `gw` is just a client.

The credentials matrix below covers all three.

## What needs a secret, and where it lives

| Secret                                | Required by                    | When                                                            | Suggested storage                                  |
| ------------------------------------- | ------------------------------ | --------------------------------------------------------------- | -------------------------------------------------- |
| `ANTHROPIC_API_KEY`                   | The component calling the LLM. | Always.                                                         | Backend's secret store (Vault / Secrets Manager / env). For local-loop: 1Password reference or env. |
| GitHub credentials (read repo)        | The sandbox doing the clone.   | When the project's repo is private.                             | Backend-side. **Not** in `gw`. See "GitHub access" below. |
| `gw` ↔ backend auth token             | `gw` (to identify itself).     | Self-hosted or third-party backend.                             | User's `~/.config/goblin-watcher/config.toml` as a literal or `op://...` reference, mirroring `linear.api_key`. |
| Per-session signed clone URL / token  | The sandbox (short-lived).     | Optional pattern for the backend to mint per-session, scoped to one repo + one branch. Reduces blast radius. | Generated and held server-side only.               |

What **`gw` itself does not need**:

- An Anthropic API key on the user's machine. The LLM call happens server-side.
- A GitHub token configured for the managed agent. `gw` never hands GitHub credentials to the backend.
- GitHub Actions secrets, repo secrets, or anything in CI. This is not a CI workflow.

## GitHub access

The ADR pinned read-only sandbox access to the repo. There are three ways to deliver that, ordered by safety:

1. **GitHub App (recommended for any hosted backend).** Operator installs a GitHub App on the user's repos / org with `contents: read`. The backend holds the App's private key and installation ID. Per-session, it mints a short-lived installation token (≤1h), uses it for one `git clone --depth N`, and discards it. No long-lived token ever leaves the backend.
2. **Fine-grained PAT.** Backend stores a user-issued PAT scoped to specific repos with `contents: read`. Easier to wire than a GitHub App; harder to rotate; broader blast radius.
3. **No GitHub credential — tarball upload.** `gw` packages the worktree locally and uploads it to the backend as a tarball. Backend never sees GitHub at all. ADR 0002 rejects this for v1 on safety grounds (bypasses git's review affordances on the way back), but it is the only zero-GitHub-credential option.

For public repos, none of this applies — the sandbox clones over HTTPS unauthenticated.

## End-to-end flow

What happens when a user runs `gw run my-task --agent managed`. Steps marked `gw` happen locally; `backend` steps happen wherever the `ManagedClient` is implemented.

```
[gw]      Resolve project, task, worktree. Confirm project.repo_url is set
            (validate_agent_for_project refuses if not).
[gw]      Load backend-auth token from config/op. Construct ManagedClient.
[gw]      client.create_session(repo_url, base_branch, prompt)
            ──────────────────────────────────────────────────►
[backend]                                       Mint a per-session GitHub
                                                installation token (App route).
[backend]                                       Boot sandbox (container/microvm).
[backend]                                       git clone --depth N --branch <base>
                                                  using the short-lived token.
[backend]                                       Record HEAD as base_sha. Drop
                                                  the GitHub token.
[backend]                                       Spawn Claude (Agent SDK / API)
                                                  with the user's prompt + tools.
            ◄──── RemoteSession(session_id, base_sha) ─────────
[gw]      Persist SessionRecord(agent="managed", session_id, base_sha).
            Open attach loop (stdio or tmux pane).

  ╔══ attach loop ════════════════════════════════════════════╗
  ║ [gw]      Read user input → client.submit_turn(sid, msg)   ║
  ║ [backend] Drive Claude turn; emit events on the stream.    ║
  ║ [gw]      stream_events(sid, since_offset) → display +     ║
  ║             persist last-seen offset.                      ║
  ║ [user]    Detach: just close the pane. Session keeps       ║
  ║             running server-side. Reattach later from any   ║
  ║             machine: gw run my-task --agent managed.       ║
  ╚════════════════════════════════════════════════════════════╝

[user]    Trigger checkpoint (or session ends naturally).
[gw]      client.fetch_patch(session_id, checkpoint=...)
            ──────────────────────────────────────────────────►
[backend]                                       git format-patch against
                                                  base_sha (or full diff).
            ◄──── PatchArtifact(diff, base_sha, checkpoint) ───
[gw]      Write to <project>/.goblin/patches/<sid>-<ckpt>.patch.
[gw]      apply_patch_safely(worktree, patch, base_sha)
            ↳ refuses if worktree dirty (status "refused_dirty")
            ↳ refuses if HEAD has moved (status "refused_diverged")
            ↳ git apply --3way otherwise (status "applied" /
                "refused_conflict")
[gw]      Print outcome + the patch path so the user can
            apply / resolve manually if refused.

[user]    Done. client.terminate(session_id). Backend tears down sandbox.
```

## Configuration `gw` will need (sketch)

Today's `config.toml` schema (in `src/goblin_watcher/config.py`) doesn't carry a managed-agent section. When the backend lands, expect something like:

```toml
[managed]
endpoint = "https://managed.example.invalid"
auth_token = "op://Personal/gw-managed/token"   # literal or op:// reference
# Optional: pin model / max iteration / etc, surface depends on backend.
model = "claude-sonnet-4-6"
```

Resolution mirrors `linear.api_key` today — `op://...` references resolved lazily via `secrets.py` so the actual value never sits in the file.

## Open questions

The contract is precise enough to scaffold against, but several details aren't pinned because no backend exists yet:

- **Wire format for `stream_events`.** SSE? WebSocket? Long-poll? Affects how `gw`'s attach loop is implemented.
- **Authentication.** Bearer tokens are the default assumption; mTLS or signed-request schemes are also reasonable for self-hosted.
- **Sandbox lifetime / idle reaping.** When does an unattended session get torn down? Today's session-summary refresh model assumes the user is in control of session end; managed has to handle "user disappeared for a week."
- **Cost attribution.** Server-side billing for LLM tokens is the backend's concern, but `gw` may want to surface cost-so-far in `gw status`. Not in v1.

Update this doc when those decisions land.
