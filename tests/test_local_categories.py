from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from rss_to_wp import cli
from rss_to_wp.config import FeedConfig
from rss_to_wp.local_categories import additional_local_categories
from rss_to_wp.wordpress.client import WordPressClient


@pytest.mark.parametrize("source", [
    "A meeting in Corinth, Mississippi, is set for Monday.",
    "Corinth MS residents are invited.",
    "The Corinth Police Department announced road closures.",
    "The Corinth Elks Lodge donated to student programs.",
    "Northeast Mississippi Community College students met in Corinth.",
    "<p>Corinth&nbsp;City Park hosts the event.</p>",
])
def test_explicit_local_rss_signals(source):
    assert additional_local_categories("https://alcornnewsms.com", "Update", source) == [
        "Corinth MS News"
    ]


@pytest.mark.parametrize("source", [
    "Corintheis Cullins appeared in Lee County court.",
    "Corinth Baptist Church in Tupelo held a picnic.",
    "The Corinth Police Department in Corinth, Texas announced an event.",
    "Corinth, Greece hosted a festival.",
    "Corinth Coca-Cola sponsored an unrelated school activity.",
    "An Alcorn County meeting is set for Monday.",
    "Mississippi schools announced a holiday.",
])
def test_ambiguous_unrelated_and_other_cities_not_routed(source):
    assert additional_local_categories("https://alcornnewsms.com", "Update", source) == []


def test_other_sites_not_changed():
    assert additional_local_categories(
        "https://anothernews.com", "Corinth, MS", "Community event."
    ) == []


@pytest.mark.parametrize("source_title,generated_title,expected", [
    ("Corinth Police Department update", "Police update", [221]),
    ("Library update", "Corinth, MS library update", []),
])
def test_pipeline_routes_original_rss_and_retains_default_category(
    monkeypatch, source_title, generated_title, expected
):
    monkeypatch.setattr(cli, "find_rss_image", Mock(return_value=None))
    monkeypatch.setattr(cli, "find_fallback_image", Mock(return_value=None))
    rewriter = Mock()
    rewriter.rewrite.return_value = {
        "publish": True, "headline": generated_title,
        "body": "<p>The office will close Monday.</p>", "excerpt": "Office closure."
    }
    wp = Mock()
    wp.get_or_create_category.side_effect = lambda name: {
        "Local News": 760, "Corinth MS News": 221
    }[name]
    cli.process_entry(
        {"title": source_title, "summary": "The office will close Monday."},
        FeedConfig(name="Local", url="https://example.test/rss", default_category="Local News"),
        SimpleNamespace(wordpress_base_url="https://alcornnewsms.com",
                        pexels_api_key=None, unsplash_access_key=None),
        rewriter, wp, False, Mock(),
    )
    assert wp.create_post.call_args.kwargs["category_id"] == 760
    assert wp.create_post.call_args.kwargs["additional_category_ids"] == expected


def test_wordpress_sends_both_categories_without_duplicates():
    wp = WordPressClient("https://example.test", "unused", "unused")
    wp.session = Mock()
    wp._rate_limit = Mock()
    wp.session.post.return_value.json.return_value = {"id": 1}
    wp.create_post("Office closes Monday", "<p>The office closes Monday.</p>",
                   category_id=760, additional_category_ids=[221, 760])
    assert wp.session.post.call_args.kwargs["json"]["categories"] == [760, 221]
