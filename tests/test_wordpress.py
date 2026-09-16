import hashlib
from unittest.mock import Mock

import pytest
import requests

from rss_to_wp.wordpress.client import WordPressClient


def client_fixture(article, review, context):
    wp = WordPressClient("https://example.test", "unused", "unused")
    wp.categories = context["categories"]
    wp.find_source_posts = Mock(return_value=[])
    wp.check_duplicate_by_slug = Mock(return_value=False)
    wp.get_or_create_tags = Mock(return_value=[10, 11])
    state = {}

    def request(method, endpoint, **kwargs):
        if endpoint == "users/1":
            return {"id": 1, "name": "Jon R Myers"}
        if endpoint == "media/123":
            return {
                "id": 123,
                "source_url": "https://example.test/image.jpg",
                "alt_text": review["image_alt"],
                "media_details": {"width": 1200, "height": 800},
            }
        if method == "POST" and endpoint == "posts":
            state.update(kwargs["json"])
            return {"id": 456, **state}
        if method == "GET" and endpoint == "posts/456":
            return {
                "id": 456,
                **state,
                **{k: {"raw": state[k]} for k in ("content", "excerpt", "title")},
            }
        if method == "POST" and endpoint == "posts/456":
            state.update(kwargs["json"])
            return {"id": 456, "link": "https://example.test/news", **state}
        raise AssertionError((method, endpoint))

    wp._request = Mock(side_effect=request)

    def seo_write(method, post_id, suffix, **kwargs):
        state.setdefault("meta", {})
        if suffix == "title-description-metas":
            state["meta"].update(
                _seopress_titles_title=kwargs["json"]["title"],
                _seopress_titles_desc=kwargs["json"]["description"],
            )
        else:
            state["meta"].update(kwargs["json"])
        return {"code": "success"}

    wp._seo_request = Mock(side_effect=seo_write)
    return wp


def publish(wp, article, review):
    return wp.create_post(
        article=article | {"review": review},
        source_url="https://example.test/source",
        source_name="Corinth Library",
        related=[],
        featured_media_id=123,
    )


def test_stage_metadata_verify_then_publish(article, review, context):
    wp = client_fixture(article, review, context)
    result = publish(wp, article, review)
    assert result["status"] == "publish"
    posts = [c for c in wp._request.call_args_list if c.args[0] == "POST"]
    assert posts[0].kwargs["json"]["status"] == "draft"
    assert posts[0].kwargs["json"]["author"] == 1
    assert posts[0].kwargs["json"]["categories"] == [221]
    assert posts[-1].kwargs["json"] == {"status": "publish"}
    assert wp._seo_request.call_count == 2


@pytest.mark.parametrize(
    "failure", ["seo_write", "seo_read", "author", "media", "content", "taxonomy"]
)
def test_failure_never_sends_publish(failure, article, review, context):
    wp = client_fixture(article, review, context)
    if failure == "seo_write":
        wp._seo_request.side_effect = RuntimeError("metadata unavailable")
    elif failure == "seo_read":
        wp._seo_request.side_effect = None
        wp._seo_request.return_value = {"title": "Wrong title"}
    else:
        original = wp._request.side_effect

        def altered(method, endpoint, **kwargs):
            value = original(method, endpoint, **kwargs)
            if failure == "author" and endpoint == "users/1":
                value["name"] = "Wrong Author"
            if failure == "media" and endpoint == "media/123":
                value["alt_text"] = ""
            if method == "GET" and endpoint == "posts/456":
                if failure == "content":
                    value["content"]["raw"] = "<p>Changed by a plugin</p>"
                if failure == "taxonomy":
                    value["categories"] = [1]
            return value

        wp._request.side_effect = altered
    with pytest.raises(RuntimeError):
        publish(wp, article, review)
    assert not any(
        c.kwargs.get("json") == {"status": "publish"} for c in wp._request.call_args_list
    )


def test_draft_status_remains_draft(article, review, context):
    wp = client_fixture(article, review, context)
    wp.default_status = "draft"
    assert publish(wp, article, review)["status"] == "draft"
    assert not any(
        c.kwargs.get("json") == {"status": "publish"} for c in wp._request.call_args_list
    )


def test_existing_source_does_not_create_post(article, review, context):
    wp = client_fixture(article, review, context)
    wp.find_source_posts.return_value = [{"id": 44, "status": "publish"}]
    assert publish(wp, article, review)["duplicate"]
    assert not any(c.args[0] == "POST" for c in wp._request.call_args_list)


def test_source_check_outage_raises():
    wp = WordPressClient("https://example.test", "unused", "unused")
    wp._request = Mock(side_effect=requests.ConnectionError("offline"))
    with pytest.raises(requests.ConnectionError):
        wp.check_duplicate_by_source_url("https://example.test/source")


def test_duplicate_search_finds_legacy_www_and_tracking_links():
    wp = WordPressClient("https://example.test", "unused", "unused")
    wp._request = Mock(
        return_value=[
            {
                "id": 1,
                "status": "publish",
                "content": {
                    "raw": '<p>Source: <a href="https://www.facebook.com/123/posts/456/?utm_source=rss">Original</a></p>'
                },
            }
        ]
    )
    assert wp.check_duplicate_by_source_url("https://facebook.com/123/posts/456")
    assert wp._request.call_args.kwargs["params"]["search"] == "facebook.com/123/posts/456"


def test_unknown_draft_is_not_overwritten():
    wp = WordPressClient("https://example.test", "unused", "unused")
    wp.find_source_posts = Mock(
        return_value=[
            {"id": 1, "status": "draft", "author": 1, "content": {"raw": "Human editorial draft"}}
        ]
    )
    assert wp.check_duplicate_by_source_url("https://example.test/source")


def test_incomplete_staging_draft_resumes_without_second_creation(article, review, context):
    wp = client_fixture(article, review, context)
    marker = hashlib.sha256(b"https://example.test/source").hexdigest()[:12]
    wp.find_source_posts.return_value = [
        {
            "id": 456,
            "status": "draft",
            "author": 1,
            "content": {"raw": f"<!-- rss-to-wp:quality-v1:{marker} -->"},
        }
    ]
    assert publish(wp, article, review)["status"] == "publish"
    assert not any(c.args == ("POST", "posts") for c in wp._request.call_args_list)
