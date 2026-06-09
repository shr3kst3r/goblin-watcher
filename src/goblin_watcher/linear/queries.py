FETCH_ISSUE = """
query GoblinFetchIssue($team: String!, $number: Float!) {
  issues(filter: {team: {key: {eq: $team}}, number: {eq: $number}}) {
    nodes {
      id
      identifier
      title
      description
      url
      state { name }
      team { key }
      comments(first: 100) {
        nodes {
          body
          createdAt
          user { displayName name }
        }
      }
    }
  }
}
""".strip()

FETCH_ISSUE_STATE = """
query GoblinFetchIssueState($team: String!, $number: Float!) {
  issues(filter: {team: {key: {eq: $team}}, number: {eq: $number}}) {
    nodes {
      state { name }
    }
  }
}
""".strip()

# The one write gw performs against Linear. Only reachable through the
# explicit `--notify-linear` flag on `gw pr open` (see AGENTS.md safety
# boundaries: the API is read-only by default).
CREATE_COMMENT = """
mutation GoblinCreateComment($issueId: String!, $body: String!) {
  commentCreate(input: {issueId: $issueId, body: $body}) {
    success
  }
}
""".strip()
