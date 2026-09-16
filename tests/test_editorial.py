import json
from types import SimpleNamespace
from unittest.mock import Mock

import pendulum
import pytest
from bs4 import BeautifulSoup

from rss_to_wp.content_policy import ContentRejectedError, plain_text
from rss_to_wp.editorial import (
    Article,
    Review,
    canonical_url,
    require_approved_review,
    validate_article,
)
from rss_to_wp.feeds.filter import generate_entry_key, is_within_window, parse_entry_date
from rss_to_wp.rewriter.openai_client import OpenAIRewriter


def reply(data, finish="stop"):
    if isinstance(data, dict) and "body" in data:
        data = dict(data)
        data["paragraphs"] = [
            p.get_text() for p in BeautifulSoup(data.pop("body"), "html.parser").find_all("p")
        ]
    return SimpleNamespace(
        choices=[
            SimpleNamespace(
                finish_reason=finish,
                message=SimpleNamespace(content=json.dumps(data), refusal=None),
            )
        ],
        usage=None,
    )


def writer(*replies):
    instance = OpenAIRewriter("unused")
    instance.client = Mock()
    instance.client.chat.completions.create.side_effect = replies
    return instance


def test_complete_article_requires_separate_visual_review(article, review, context, image_bytes):
    client = writer(reply(article), reply(review))
    result = client.rewrite(
        plain_text(article["body"]), article["headline"], context=context, image_bytes=image_bytes
    )
    assert result["review"]["quality_score"] == 95
    calls = client.client.chat.completions.create.call_args_list
    assert len(calls) == 2
    assert "image_url" in calls[1].kwargs["messages"][1]["content"][1]
    assert "temperature" not in calls[0].kwargs
    assert calls[0].kwargs["reasoning_effort"] == "medium"
    assert calls[0].kwargs["max_completion_tokens"] == 5000
    assert calls[0].kwargs["response_format"]["json_schema"]["strict"] is True


@pytest.mark.parametrize(
    "field",
    [
        "faithful",
        "newsworthy",
        "locally_relevant",
        "sufficiently_reported",
        "metadata_accurate",
        "not_duplicate",
        "image_relevant",
        "source_usable",
    ],
)
def test_each_failed_editorial_check_blocks(field, review):
    review[field] = False
    with pytest.raises(ContentRejectedError):
        require_approved_review(Review(**review), 90)


@pytest.mark.parametrize(
    "override", [{"quality_score": 89}, {"issues": ["unsupported quote"]}, {"image_alt": ""}]
)
def test_score_issues_and_alt_are_required(override, review):
    with pytest.raises(ContentRejectedError):
        require_approved_review(Review(**(review | override)), 90)


@pytest.mark.parametrize(
    "override",
    [
        {"body": "<p>The library closes Monday.</p>"},
        {"body": "<script>bad()</script>"},
        {"body": '<p onclick="bad()">An article</p>'},
        {"category_slugs": ["uncategorized"]},
        {"tags": ["Corinth Library", "Memphis"]},
        {"related_post_ids": [999]},
        {"meta_description": ""},
        {"publish": False},
        {"slug": "../bad"},
        {"headline": "<b>Headline</b>"},
    ],
)
def test_invalid_articles_block(override, article, context):
    with pytest.raises((ContentRejectedError, ValueError)):
        validate_article(article | override, context["categories"], [], 150)


@pytest.mark.parametrize(
    "failure", [RuntimeError("service down"), reply({}, "length"), reply({"faithful": True})]
)
def test_unavailable_or_malformed_review_never_passes(failure, article, context, image_bytes):
    client = writer(reply(article), failure)
    with pytest.raises((RuntimeError, ValueError)):
        client.rewrite(
            plain_text(article["body"]),
            article["headline"],
            context=context,
            image_bytes=image_bytes,
        )


@pytest.mark.parametrize(
    "source", ["This content is unavailable", "Congratulations to our employee of the month."]
)
def test_thin_or_unavailable_sources_cost_no_tokens(source, context, image_bytes):
    client = writer()
    with pytest.raises(ContentRejectedError):
        client.rewrite(source, "Community update", context=context, image_bytes=image_bytes)
    client.client.chat.completions.create.assert_not_called()


def test_urls_and_guid_changes_dedupe_across_feeds():
    a = {"id": "old", "link": "https://www.facebook.com/123/posts/456/?fbclid=tracking"}
    b = {"id": "new", "link": "https://m.facebook.com/123/posts/456/"}
    assert generate_entry_key(a, "one") == generate_entry_key(b, "two")
    assert (
        canonical_url("https://example.test/story?story=2&utm_source=x")
        == "https://example.test/story?story=2"
    )


def test_future_dates_rejected_and_feed_utc_preserved():
    assert not is_within_window(pendulum.now("UTC").add(days=1))
    date = parse_entry_date({"published_parsed": (2026, 9, 15, 12, 30, 0, 1, 258, 0)})
    assert date.hour == 12
    assert date.utcoffset().total_seconds() == 0


def test_schema_rejects_string_boolean(article):
    with pytest.raises(ValueError):
        Article(**(article | {"publish": "true"}))
