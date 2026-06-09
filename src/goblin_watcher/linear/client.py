from __future__ import annotations

import re
from datetime import datetime
from typing import Any

import httpx

from goblin_watcher.errors import GoblinError, LinearAuthError
from goblin_watcher.linear.queries import CREATE_COMMENT, FETCH_ISSUE, FETCH_ISSUE_STATE
from goblin_watcher.models import LinearComment, LinearIssue

LINEAR_ENDPOINT = "https://api.linear.app/graphql"
_IDENTIFIER_RE = re.compile(r"^([A-Z][A-Z0-9_]*)-(\d+)$", re.IGNORECASE)


def parse_identifier(identifier: str) -> tuple[str, int]:
    """Parse `ENG-123` into (team_key='ENG', number=123). Case-insensitive."""
    m = _IDENTIFIER_RE.match(identifier.strip())
    if not m:
        raise GoblinError(
            f"{identifier!r} is not a valid Linear identifier.",
            hint="Use the form TEAM-N, e.g. ENG-123.",
        )
    return m.group(1).upper(), int(m.group(2))


class LinearClient:
    def __init__(self, api_key: str, client: httpx.Client | None = None) -> None:
        if not api_key:
            raise LinearAuthError("Empty Linear API key.")
        self._owns_client = client is None
        self._client = client or httpx.Client(
            headers={"Authorization": api_key, "Content-Type": "application/json"},
            timeout=15.0,
        )

    def close(self) -> None:
        if self._owns_client:
            self._client.close()

    def __enter__(self) -> LinearClient:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    def _post(self, query: str, variables: dict[str, Any]) -> dict[str, Any]:
        try:
            response = self._client.post(
                LINEAR_ENDPOINT,
                json={"query": query, "variables": variables},
            )
        except httpx.HTTPError as e:
            raise GoblinError(
                f"Linear API request failed: {e}",
                hint="Check your network and try again.",
            ) from e
        if response.status_code == 401:
            raise LinearAuthError(
                "Linear API rejected the credentials (401).",
                hint="Check LINEAR_API_KEY or the `op://` reference in config.",
            )
        if response.status_code >= 400:
            raise GoblinError(
                f"Linear API returned {response.status_code}: {response.text[:200]}",
            )
        payload = response.json()
        if "errors" in payload:
            msg = "; ".join(e.get("message", "?") for e in payload["errors"])
            raise GoblinError(f"Linear API error: {msg}")
        data = payload.get("data")
        if data is None:
            raise GoblinError("Linear API returned no data.")
        return data

    def fetch_issue_state(self, identifier: str) -> str:
        """Return the current workflow-state name for `identifier` (e.g. "In Progress")."""
        team_key, number = parse_identifier(identifier)
        data = self._post(FETCH_ISSUE_STATE, {"team": team_key, "number": float(number)})
        nodes = data.get("issues", {}).get("nodes", [])
        if not nodes:
            raise GoblinError(
                f"Linear issue {team_key}-{number} not found.",
                hint="Check the identifier and that you have access to the team.",
            )
        return (nodes[0].get("state") or {}).get("name", "Unknown")

    def create_comment(self, issue_id: str, body: str) -> None:
        """Post a markdown comment on the issue with internal id `issue_id`.

        The only Linear write gw performs; gated behind `gw pr open
        --notify-linear`. `issue_id` is the API's internal id (stored on
        `LinearIssue.id`), not the human identifier.
        """
        data = self._post(CREATE_COMMENT, {"issueId": issue_id, "body": body})
        payload = data.get("commentCreate") or {}
        if not payload.get("success"):
            raise GoblinError("Linear API did not confirm the comment was created.")

    def fetch_issue(self, identifier: str) -> LinearIssue:
        team_key, number = parse_identifier(identifier)
        data = self._post(FETCH_ISSUE, {"team": team_key, "number": float(number)})
        nodes = data.get("issues", {}).get("nodes", [])
        if not nodes:
            raise GoblinError(
                f"Linear issue {team_key}-{number} not found.",
                hint="Check the identifier and that you have access to the team.",
            )
        node = nodes[0]
        return LinearIssue(
            id=node["id"],
            identifier=node["identifier"],
            title=node["title"],
            description=node.get("description"),
            state=(node.get("state") or {}).get("name", "Unknown"),
            team_key=(node.get("team") or {}).get("key", team_key),
            url=node["url"],
            comments=_parse_comments(node.get("comments")),
        )


def _parse_comments(payload: Any) -> list[LinearComment]:
    """Convert `comments { nodes [...] }` into LinearComment models, oldest first."""
    if not payload:
        return []
    nodes = payload.get("nodes") or []
    parsed: list[LinearComment] = []
    for n in nodes:
        body = (n.get("body") or "").strip()
        if not body:
            continue
        user = n.get("user") or {}
        author = user.get("displayName") or user.get("name")
        created_at = n.get("createdAt")
        if not created_at:
            continue
        # Linear returns ISO-8601 with `Z`; datetime.fromisoformat in Py3.11+ accepts that.
        try:
            ts = datetime.fromisoformat(created_at.replace("Z", "+00:00"))
        except ValueError:
            continue
        parsed.append(LinearComment(body=body, created_at=ts, author=author))
    parsed.sort(key=lambda c: c.created_at)
    return parsed
