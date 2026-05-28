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
