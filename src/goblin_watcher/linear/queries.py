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

# The issue's internal id, its current state, and every state its team offers —
# everything `linear_transitions` needs to decide whether to write, in one
# round-trip. `Team.states` is the team's own workflow, so a state name is only
# ever matched against the workflow the ticket actually lives in.
FETCH_ISSUE_WORKFLOW = """
query GoblinFetchIssueWorkflow($team: String!, $number: Float!) {
  issues(filter: {team: {key: {eq: $team}}, number: {eq: $number}}) {
    nodes {
      id
      state { id name }
      team {
        key
        states(first: 100) {
          nodes { id name }
        }
      }
    }
  }
}
""".strip()

# The two writes gw performs against Linear, both opt-in (see AGENTS.md safety
# boundaries: the API is read-only by default). CREATE_COMMENT is reachable only
# through the explicit `--notify-linear` flag on `gw pr open`; UPDATE_ISSUE_STATE
# only when the user has set a `[linear.transitions]` key (ADR 0012).
CREATE_COMMENT = """
mutation GoblinCreateComment($issueId: String!, $body: String!) {
  commentCreate(input: {issueId: $issueId, body: $body}) {
    success
  }
}
""".strip()

UPDATE_ISSUE_STATE = """
mutation GoblinUpdateIssueState($issueId: String!, $stateId: String!) {
  issueUpdate(id: $issueId, input: {stateId: $stateId}) {
    success
  }
}
""".strip()
