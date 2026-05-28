import random

import pytest

from goblin_watcher.slug import _WORDS, branch_slug, random_branch_name, slugify


@pytest.mark.parametrize(
    "text,expected",
    [
        ("Add rate limit", "add-rate-limit"),
        ("  weird   spacing  !!", "weird-spacing"),
        ("Émoji 🚀 stuff", "moji-stuff"),
        ("", "task"),
        ("---", "task"),
    ],
)
def test_slugify(text: str, expected: str) -> None:
    assert slugify(text) == expected


def test_slugify_respects_max_len() -> None:
    out = slugify("x" * 200)
    assert len(out) <= 40


@pytest.mark.parametrize(
    "linear_id,title,prefix,expected",
    [
        ("ENG-123", "Add rate limit", "", "eng-123-add-rate-limit"),
        ("ENG-123", "Add rate limit", "claude/", "claude/eng-123-add-rate-limit"),
        (None, "Spike: profile pricing", "", "spike-profile-pricing"),
        ("ENG-1", "", "", "eng-1"),
        ("ENG-1", "---", "", "eng-1"),
    ],
)
def test_branch_slug(linear_id: str | None, title: str, prefix: str, expected: str) -> None:
    assert branch_slug(linear_id, title, prefix) == expected


def test_branch_slug_caps_total_length() -> None:
    out = branch_slug("ENG-999", "x" * 200, "long-prefix/")
    assert len(out) <= 80


def test_random_branch_name_shape() -> None:
    name = random_branch_name("goblin-watcher", random.Random(42))
    project, _, word = name.rpartition("-")
    assert project == "goblin-watcher"
    assert word in _WORDS


def test_random_branch_name_slugifies_project() -> None:
    name = random_branch_name("My Cool Repo", random.Random(42))
    project, _, word = name.rpartition("-")
    assert project == "my-cool-repo"
    assert word in _WORDS


def test_random_branch_name_is_deterministic_with_seed() -> None:
    assert random_branch_name("alpha", random.Random(7)) == random_branch_name(
        "alpha", random.Random(7)
    )
