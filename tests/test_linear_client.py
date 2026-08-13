import pytest
from pytest_httpx import HTTPXMock

from goblin_watcher.errors import GoblinError, LinearAuthError
from goblin_watcher.linear import LinearClient, parse_identifier
from goblin_watcher.linear.client import LINEAR_ENDPOINT


def test_parse_identifier_valid() -> None:
    assert parse_identifier("ENG-123") == ("ENG", 123)
    assert parse_identifier("eng-7") == ("ENG", 7)
    assert parse_identifier("BIG_TEAM-42") == ("BIG_TEAM", 42)


@pytest.mark.parametrize("bad", ["", "ENG", "123", "ENG_", "ENG-", "ENG-abc", " ENG-1 "])
def test_parse_identifier_invalid(bad: str) -> None:
    if bad.strip() == "ENG-1":
        return  # the spaced variant happens to be valid after .strip()
    with pytest.raises(GoblinError):
        parse_identifier(bad)


def test_fetch_issue_happy_path(httpx_mock: HTTPXMock) -> None:
    httpx_mock.add_response(
        url=LINEAR_ENDPOINT,
        method="POST",
        json={
            "data": {
                "issues": {
                    "nodes": [
                        {
                            "id": "uuid-abc",
                            "identifier": "ENG-123",
                            "title": "Add rate limit",
                            "description": "Body",
                            "url": "https://linear.app/x/issue/ENG-123",
                            "state": {"name": "Todo"},
                            "team": {"key": "ENG"},
                        }
                    ]
                }
            }
        },
    )
    client = LinearClient("lin_api_test")
    issue = client.fetch_issue("ENG-123")
    assert issue.identifier == "ENG-123"
    assert issue.title == "Add rate limit"
    assert issue.team_key == "ENG"


def test_fetch_issue_not_found(httpx_mock: HTTPXMock) -> None:
    httpx_mock.add_response(
        url=LINEAR_ENDPOINT,
        method="POST",
        json={"data": {"issues": {"nodes": []}}},
    )
    client = LinearClient("lin_api_test")
    with pytest.raises(GoblinError, match="not found"):
        client.fetch_issue("ENG-999")


def test_fetch_issue_401_raises_auth_error(httpx_mock: HTTPXMock) -> None:
    httpx_mock.add_response(
        url=LINEAR_ENDPOINT,
        method="POST",
        status_code=401,
        json={"errors": [{"message": "unauthorized"}]},
    )
    client = LinearClient("lin_api_test")
    with pytest.raises(LinearAuthError):
        client.fetch_issue("ENG-1")


def test_fetch_issue_graphql_error(httpx_mock: HTTPXMock) -> None:
    httpx_mock.add_response(
        url=LINEAR_ENDPOINT,
        method="POST",
        json={"errors": [{"message": "bad filter"}]},
    )
    client = LinearClient("lin_api_test")
    with pytest.raises(GoblinError, match="bad filter"):
        client.fetch_issue("ENG-1")


def test_empty_api_key_rejected() -> None:
    with pytest.raises(LinearAuthError):
        LinearClient("")


def test_create_comment_posts_mutation(httpx_mock: HTTPXMock) -> None:
    httpx_mock.add_response(json={"data": {"commentCreate": {"success": True}}})
    with LinearClient("key") as client:
        client.create_comment("issue-uuid", "PR opened: https://github.com/o/r/pull/1")
    request = httpx_mock.get_requests()[0]
    import json as _json

    payload = _json.loads(request.content)
    assert "commentCreate" in payload["query"]
    assert payload["variables"] == {
        "issueId": "issue-uuid",
        "body": "PR opened: https://github.com/o/r/pull/1",
    }


def test_create_comment_unconfirmed_raises(httpx_mock: HTTPXMock) -> None:
    httpx_mock.add_response(json={"data": {"commentCreate": {"success": False}}})
    with LinearClient("key") as client, pytest.raises(GoblinError, match="did not confirm"):
        client.create_comment("issue-uuid", "body")


def test_fetch_issue_workflow_parses_team_states(httpx_mock: HTTPXMock) -> None:
    httpx_mock.add_response(
        json={
            "data": {
                "issues": {
                    "nodes": [
                        {
                            "id": "issue-uuid",
                            "state": {"id": "s1", "name": "Todo"},
                            "team": {
                                "key": "ENG",
                                "states": {
                                    "nodes": [
                                        {"id": "s1", "name": "Todo"},
                                        {"id": "s2", "name": "In Progress"},
                                        # Malformed rows are dropped, not fatal.
                                        {"id": None, "name": "Broken"},
                                    ]
                                },
                            },
                        }
                    ]
                }
            }
        }
    )
    with LinearClient("key") as client:
        workflow = client.fetch_issue_workflow("eng-7")

    assert workflow.issue_id == "issue-uuid"
    assert workflow.team_key == "ENG"
    assert workflow.state == "Todo"
    assert workflow.state_names == ["Todo", "In Progress"]
    found = workflow.find_state("in progress")
    assert found is not None and found.id == "s2"
    assert workflow.find_state("Done") is None


def test_fetch_issue_workflow_not_found(httpx_mock: HTTPXMock) -> None:
    httpx_mock.add_response(json={"data": {"issues": {"nodes": []}}})
    with LinearClient("key") as client, pytest.raises(GoblinError, match="not found"):
        client.fetch_issue_workflow("ENG-999")


def test_update_issue_state_posts_mutation(httpx_mock: HTTPXMock) -> None:
    httpx_mock.add_response(json={"data": {"issueUpdate": {"success": True}}})
    with LinearClient("key") as client:
        client.update_issue_state("issue-uuid", "state-id")
    import json as _json

    payload = _json.loads(httpx_mock.get_requests()[0].content)
    assert "issueUpdate" in payload["query"]
    assert payload["variables"] == {"issueId": "issue-uuid", "stateId": "state-id"}


def test_update_issue_state_unconfirmed_raises(httpx_mock: HTTPXMock) -> None:
    httpx_mock.add_response(json={"data": {"issueUpdate": {"success": False}}})
    with LinearClient("key") as client, pytest.raises(GoblinError, match="did not confirm"):
        client.update_issue_state("issue-uuid", "state-id")
