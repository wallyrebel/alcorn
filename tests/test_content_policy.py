import json
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from rss_to_wp import cli
from rss_to_wp.config import FeedConfig
from rss_to_wp.content_policy import (
    ContentRejectedError,
    access_error_reason,
    require_usable_source,
)
from rss_to_wp.feeds.parser import get_entry_content
from rss_to_wp.rewriter.openai_client import OpenAIRewriter
from rss_to_wp.wordpress.client import WordPressClient


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


def article(**overrides):
    return {
        "headline": "Library closes Monday",
        "excerpt": "Corinth Library closes Monday.",
        "body": "<p>Corinth Library closes Monday.</p>",
        **overrides,
    }


def response(value, finish="stop"):
    return SimpleNamespace(
        choices=[
            SimpleNamespace(
                message=SimpleNamespace(
                    content=json.dumps(value) if not isinstance(value, str) else value
                ),
                finish_reason=finish,
            )
        ]
    )


def rewriter_with(*replies):
    rewriter = OpenAIRewriter("test-unused-key")
    rewriter._rate_limit = Mock()
    rewriter.client = Mock()
    rewriter.client.chat.completions.create.side_effect = replies
    return rewriter


def test_one_short_paragraph_can_pass_both_checks():
    rewriter = rewriter_with(
        response({"publish": True, **article()}),
        response(
            {
                "source_usable": True,
                "faithful": True,
                "issues": [],
            }
        ),
    )
    assert rewriter.rewrite("Corinth Library closes Monday.", "Library update") == article()
    assert rewriter.client.chat.completions.create.call_count == 2


def test_source_error_never_reaches_model():
    rewriter = rewriter_with()
    with pytest.raises(ContentRejectedError):
        rewriter.rewrite("This content isn't available right now", "Community event")
    rewriter.client.chat.completions.create.assert_not_called()


@pytest.mark.parametrize(
    "verdict",
    [
        {"source_usable": True, "faithful": False, "issues": ["invented date"]},
        {"source_usable": False, "faithful": True, "issues": []},
        {"source_usable": True, "faithful": "true", "issues": []},
        {"source_usable": True, "faithful": True},
        {"source_usable": True, "faithful": True, "issues": ["unsupported quote"]},
        [],
    ],
)
def test_review_must_explicitly_approve_every_claim(verdict):
    rewriter = rewriter_with(response({"publish": True, **article()}), response(verdict))
    with pytest.raises(ContentRejectedError):
        rewriter.rewrite("Corinth Library closes Monday.", "Library update")


@pytest.mark.parametrize(
    "reply",
    [
        response("not json"),
        response({"publish": False}),
        response({"publish": True, **article(body="<p>Content unavailable</p>")}),
        response({"publish": True, **article(body="<script>alert(1)</script>")}),
        response({"publish": True, **article(headline=42)}),
        response({"publish": True, **article()}, finish="length"),
        response({"publish": True, **article(body="")}),
    ],
)
def test_invalid_or_blocked_output_cannot_publish(reply):
    rewriter = rewriter_with(reply)
    try:
        assert rewriter.rewrite("Corinth Library closes Monday.", "Library update") is None
    except ContentRejectedError:
        pass
    assert rewriter.client.chat.completions.create.call_count == 1


@pytest.mark.parametrize(
    "review", [response("not json"), RuntimeError("review service unavailable")]
)
def test_unavailable_or_malformed_review_fails_closed(review):
    rewriter = rewriter_with(response({"publish": True, **article()}), review)
    assert rewriter.rewrite("Corinth Library closes Monday.", "Library update") is None


def test_original_title_is_checked_after_override():
    rewriter = rewriter_with(
        response({"publish": True, **article()}),
        response(
            {
                "source_usable": True,
                "faithful": True,
                "issues": [],
            }
        ),
    )
    result = rewriter.rewrite(
        "Corinth Library closes Monday.", "Corinth Library announcement", True
    )
    reviewed = json.loads(
        rewriter.client.chat.completions.create.call_args.kwargs["messages"][1]["content"]
    )
    assert reviewed["article"]["headline"] == result["headline"] == "Corinth Library announcement"


def process(entry, monkeypatch, rewritten=None, dry_run=False):
    rewriter = Mock()
    rewriter.rewrite.return_value = rewritten or article()
    wp = Mock()
    wp.create_post.return_value = {"id": 123, "link": "https://example.test/article"}
    monkeypatch.setattr(cli, "find_rss_image", Mock(return_value=None))
    monkeypatch.setattr(cli, "find_fallback_image", Mock(return_value=None))
    settings = SimpleNamespace(pexels_api_key=None, unsplash_access_key=None)
    result = cli.process_entry(
        entry,
        FeedConfig(name="Test", url="https://example.test/rss"),
        settings,
        rewriter,
        wp,
        dry_run,
        Mock(),
    )
    return result, rewriter, wp


@pytest.mark.parametrize(
    "entry",
    [
        {"title": "Community update", "summary": "This content isn't available"},
        {
            "title": "Community update",
            "summary": "Access denied",
            "content": [{"value": "The library closes Monday."}],
        },
        {"title": "Content unavailable", "summary": "The library closes Monday."},
    ],
)
def test_pipeline_rejects_all_rss_fields_before_side_effects(entry, monkeypatch):
    result, rewriter, wp = process(entry, monkeypatch)
    assert result["skipped"]
    rewriter.rewrite.assert_not_called()
    assert wp.mock_calls == []
    cli.find_rss_image.assert_not_called()


def test_pipeline_catches_bad_model_output_before_upload(monkeypatch):
    result, _, wp = process(
        {"title": "News", "summary": "Library closes Monday."},
        monkeypatch,
        article(body="<p>Content unavailable due to privacy settings.</p>"),
    )
    assert result["skipped"]
    assert wp.mock_calls == []


def test_short_story_without_image_can_publish(monkeypatch):
    result, _, wp = process({"title": "News", "summary": "Library closes Monday."}, monkeypatch)
    assert result["id"] == 123
    wp.upload_media.assert_not_called()
    wp.create_post.assert_called_once()


def test_dry_run_does_not_publish_or_upload(monkeypatch):
    result, _, wp = process(
        {"title": "News", "summary": "Library closes Monday."}, monkeypatch, dry_run=True
    )
    assert result["link"] == "dry-run://not-published"
    assert wp.mock_calls == []


@pytest.mark.parametrize(
    "dry_run,result,counts",
    [
        (True, {"id": 0, "link": "dry-run://not-published"}, (1, 0, 0)),
        (False, {"skipped": True, "reason": "unavailable_or_restricted_content"}, (0, 1, 0)),
    ],
)
def test_previews_and_rejections_do_not_poison_dedupe(monkeypatch, dry_run, result, counts):
    entry = {"title": "Library update", "summary": "Library closes Monday."}
    monkeypatch.setattr(cli, "parse_feed", Mock(return_value=SimpleNamespace(entries=[entry])))
    monkeypatch.setattr(cli, "pick_entries", Mock(return_value=[entry]))
    monkeypatch.setattr(cli, "process_entry", Mock(return_value=result))
    store = Mock()
    store.is_processed.return_value = False
    actual = cli.process_feed(
        FeedConfig(name="Test", url="https://example.test/rss"),
        SimpleNamespace(timezone="UTC"),
        store,
        Mock(),
        Mock(),
        dry_run,
        48,
        Mock(),
    )
    assert actual == counts
    store.mark_processed.assert_not_called()


def test_last_wordpress_barrier_blocks_direct_error_post():
    wp = WordPressClient("https://example.test", "test", "unused")
    wp.session = Mock()
    with pytest.raises(ContentRejectedError):
        wp.create_post("Content unavailable", "<p>The owner changed privacy settings.</p>")
    assert wp.session.mock_calls == []


def test_empty_full_content_falls_back_to_rss_summary():
    assert (
        get_entry_content({"content": [{"value": ""}], "summary": "Library closed."})
        == "Library closed."
    )
