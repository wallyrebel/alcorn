from unittest.mock import Mock

import pytest

from rss_to_wp.content_policy import (
    access_error_reason,
    require_usable_source,
)
from rss_to_wp.feeds.parser import get_entry_content


@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    # Every test uses canned RSS/model replies and mocked WordPress boundaries.
    monkeypatch.setattr(
        "requests.sessions.Session.request", Mock(side_effect=AssertionError("network forbidden"))
    )
    monkeypatch.setattr(
        "socket.create_connection", Mock(side_effect=AssertionError("network forbidden"))
    )


@pytest.mark.parametrize(
    "notice",
    [
        "This content isn't available right now",
        "This content isn’t available right now",
        "This content isn&#8217;t available right now",
        "<p>Content</p><p>unavailable due to privacy settings or deletion</p>",
        "<header>This content isn't available right now</header>",
        "Access to Content Restricted or Removed",
        "However, the content is no longer accessible.",
        "The content may be limited to a small audience or has been removed.",
        "The owner shared it with a small group of people",
        "The owner changed their privacy settings",
        "The post has been removed",
        "Log in to continue",
        "Sign in to view this post",
        "403 Forbidden",
        "404 Not Found",
        "Access denied",
        "Subscribe to continue reading",
        "Verify you are human",
        "Content\u200bunavailable",
    ],
)
def test_reject_access_notices(notice):
    assert access_error_reason(notice)


@pytest.mark.parametrize(
    "text",
    [
        "Corinth Library closes Monday.",
        "The tip line is temporarily unavailable. Call 911 in an emergency.",
        "Road access is restricted during construction on Main Street.",
        "Two people were arrested. Additional details were not available.",
    ],
)
def test_short_news_and_legitimate_restrictions_are_allowed(text):
    require_usable_source("Local update", text)


def test_empty_full_content_falls_back_to_rss_summary():
    assert (
        get_entry_content({"content": [{"value": ""}], "summary": "Library closed."})
        == "Library closed."
    )
