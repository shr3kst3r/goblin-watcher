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
