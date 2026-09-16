"""Fail-closed stock image integration and attribution tests."""

from unittest.mock import Mock

import pytest
import requests
from test_pipeline import entry_for, setup_pipeline

from rss_to_wp import cli
from rss_to_wp.content_policy import ContentRejectedError
from rss_to_wp.editorial import StockPlan, StockSelection, render_content
from rss_to_wp.images.downloader import download_image
from rss_to_wp.images.pexels import PexelsClient, StockPhoto, stock_credit, stock_topic_blocked
from rss_to_wp.rewriter.openai_client import OpenAIRewriter


@pytest.fixture
def photo():
    return dict(
        photo_id=123,
        url="https://images.pexels.com/photos/123/books.jpeg?w=1880",
        photo_url="https://www.pexels.com/photo/books-123/",
        photographer="A & B",
        photographer_url="https://www.pexels.com/@photographer",
        description="A stack of books",
        width=3000,
        height=2000,
    )


def provider_photo(photo):
    return dict(
        id=photo["photo_id"],
        url=photo["photo_url"],
        photographer=photo["photographer"],
        photographer_url=photo["photographer_url"],
        width=photo["width"],
        height=photo["height"],
        alt=photo["description"],
        src={"large2x": photo["url"], "large": "https://images.pexels.com/photos/123/small.jpg"},
    )


def test_search_returns_bounded_large_candidates_not_first_hit(photo):
    client = PexelsClient("unused")
    raw = provider_photo(photo)
    candidates = [raw | {"width": 500}, raw, raw, raw | {"id": 456}]
    response = Mock(status_code=200)
    response.json.return_value = {"photos": candidates + [raw | {"id": 789}]}
    client.session.get = Mock(return_value=response)
    actual = client.search("stack of library books")
    assert [p["photo_id"] for p in actual] == [123, 456]
    assert actual[0]["url"] == photo["url"]
    assert client.session.get.call_args.kwargs["params"]["per_page"] == 4
    assert client.session.get.call_args.kwargs["allow_redirects"] is False
    response.close.assert_called_once()


@pytest.mark.parametrize(
    "query", ["news", "", "http://bad.test", "one two three four five six seven"]
)
def test_no_generic_random_search(query):
    client = PexelsClient("unused")
    client.session.get = Mock()
    with pytest.raises(ValueError):
        client.search(query)
    client.session.get.assert_not_called()


@pytest.mark.parametrize(
    "field,value",
    [
        ("url", "https://images.pexels.com.evil.test/photos/a.jpg"),
        ("url", "http://images.pexels.com/photos/a.jpg"),
        ("url", "https://images.pexels.com@evil.test/photos/a.jpg"),
        ("photo_url", "javascript:alert(1)"),
        ("photographer_url", "https://evil.test/@author"),
        ("width", 1199),
    ],
)
def test_untrusted_or_small_photo_rejected(photo, field, value):
    with pytest.raises(ValueError):
        StockPhoto.model_validate(photo | {field: value})


def test_api_failure_does_not_become_approval():
    client = PexelsClient("unused")
    response = Mock(status_code=429)
    response.raise_for_status.side_effect = requests.HTTPError("rate limited")
    client.session.get = Mock(return_value=response)
    with pytest.raises(requests.HTTPError):
        client.search("library books")
    response.close.assert_called_once()


def test_stock_download_blocks_redirect_and_external_host(monkeypatch):
    response = Mock(status_code=302)
    get = Mock(return_value=response)
    monkeypatch.setattr("requests.get", get)
    assert (
        download_image(
            "https://images.pexels.com/photos/x.jpg", allowed_hosts={"images.pexels.com"}
        )
        is None
    )
    assert get.call_args.kwargs["allow_redirects"] is False
    response.close.assert_called_once()
    get.reset_mock()
    assert download_image("https://evil.test/a.jpg", allowed_hosts={"images.pexels.com"}) is None
    get.assert_not_called()


@pytest.mark.parametrize(
    "title",
    [
        "Police arrest suspect",
        "Missing person appeal",
        "Tornado warning",
        "Election candidate forum",
        "Patient diagnosed with illness",
        "Flood preparedness",
    ],
)
def test_sensitive_topics_never_search_or_call_model(title):
    assert stock_topic_blocked(title, "Substantial source material")
    writer = OpenAIRewriter("unused")
    writer._request = Mock()
    with pytest.raises(ContentRejectedError):
        writer.plan_stock_image(title, "Substantial source material", {})
    writer._request.assert_not_called()


@pytest.mark.parametrize(
    "override",
    [
        {"photo_id": 0},
        {"photo_id": 999},
        {"quality_score": 89},
        {"safe_illustration": False},
        {"relevant": False},
        {"issues": ["unrelated building"]},
    ],
)
def test_visual_selection_rejects_any_failed_check(photo, image_bytes, override):
    writer = OpenAIRewriter("unused")
    writer._request = Mock(
        return_value=StockSelection(
            **(
                dict(
                    photo_id=123,
                    relevant=True,
                    safe_illustration=True,
                    quality_score=95,
                    issues=[],
                )
                | override
            )
        )
    )
    plan = StockPlan(eligible=True, query="library books", reason="Public service")
    assert (
        writer.select_stock_image(
            "Library services", "Source text", plan, [{"photo": photo, "bytes": image_bytes}]
        )
        is None
    )


def test_visual_selection_inspects_all_and_can_choose_second(photo, image_bytes):
    writer = OpenAIRewriter("unused")
    writer._request = Mock(
        return_value=StockSelection(
            photo_id=456,
            relevant=True,
            safe_illustration=True,
            quality_score=95,
            issues=[],
        )
    )
    plan = StockPlan(eligible=True, query="library books", reason="Public service")
    candidates = [
        {"photo": photo, "bytes": image_bytes},
        {"photo": photo | {"photo_id": 456}, "bytes": image_bytes},
    ]
    chosen = writer.select_stock_image("Library services", "Source text", plan, candidates)
    assert chosen == candidates[1]
    parts = writer._request.call_args.args[2]
    assert len([p for p in parts if p["type"] == "image_url"]) == 2


def test_credit_visible_in_article_even_without_theme_caption(photo, article):
    content = render_content(article, "https://example.test/source", "Library", [], photo)
    assert content.startswith("<p><em>Stock illustration;")
    assert "does not depict the people, place or event" in content
    assert photo["photo_url"] in content and photo["photographer_url"] in content
    assert "A &amp; B" in content
    with pytest.raises(ValueError):
        stock_credit(photo | {"photo_url": "https://evil.test/"})


@pytest.mark.parametrize("dry_run", [True, False])
def test_fallback_selected_image_reaches_final_review_and_credit(
    monkeypatch,
    settings,
    feed_config,
    article,
    review,
    context,
    image_bytes,
    photo,
    dry_run,
):
    settings.pexels_api_key = "unused"
    wp, writer = setup_pipeline(monkeypatch, article, review, context, image_bytes)
    cli.find_rss_image.return_value = None
    writer.plan_stock_image.return_value = StockPlan(
        eligible=True, query="library books", reason="Service"
    )
    writer.select_stock_image.side_effect = lambda title, text, plan, candidates: candidates[0]
    provider = Mock()
    provider.search.return_value = [photo]
    monkeypatch.setattr(cli, "PexelsClient", Mock(return_value=provider))
    budget = {"candidates": 0, "posts": 0}
    result = cli.process_entry(
        entry_for(article), feed_config, settings, writer, wp, dry_run, Mock(), budget=budget
    )
    assert budget["candidates"] == 1
    assert writer.rewrite.call_args.kwargs["context"]["image_provenance"]["kind"] == "pexels_stock"
    assert writer.rewrite.call_args.kwargs["image_bytes"] == image_bytes
    if dry_run:
        assert result["preview"] and "Stock illustration" in result["content"]
        wp.upload_media.assert_not_called()
        wp.create_post.assert_not_called()
    else:
        assert "Pexels</a>" in wp.upload_media.call_args.kwargs["caption"]
        assert wp.create_post.call_args.kwargs["image_credit"] == photo


def test_usable_source_never_calls_pexels(
    monkeypatch, settings, feed_config, article, review, context, image_bytes
):
    settings.pexels_api_key = "unused"
    wp, writer = setup_pipeline(monkeypatch, article, review, context, image_bytes)
    provider = Mock()
    monkeypatch.setattr(cli, "PexelsClient", provider)
    assert cli.process_entry(entry_for(article), feed_config, settings, writer, wp, True, Mock())[
        "preview"
    ]
    provider.assert_not_called()
    writer.plan_stock_image.assert_not_called()


@pytest.mark.parametrize("failure", ["none_suitable", "final_review"])
def test_stock_rejection_never_uploads_or_publishes(
    monkeypatch,
    settings,
    feed_config,
    article,
    review,
    context,
    image_bytes,
    photo,
    failure,
):
    settings.pexels_api_key = "unused"
    wp, writer = setup_pipeline(monkeypatch, article, review, context, image_bytes)
    cli.find_rss_image.return_value = None
    writer.plan_stock_image.return_value = StockPlan(
        eligible=True, query="library books", reason="Service"
    )
    provider = Mock()
    provider.search.return_value = [photo]
    monkeypatch.setattr(cli, "PexelsClient", Mock(return_value=provider))
    writer.select_stock_image.return_value = (
        {"photo": photo, "bytes": image_bytes} if failure == "final_review" else None
    )
    writer.rewrite.side_effect = ContentRejectedError("image misrepresents local facility")
    result = cli.process_entry(entry_for(article), feed_config, settings, writer, wp, False, Mock())
    assert result["skipped"]
    wp.upload_media.assert_not_called()
    wp.create_post.assert_not_called()


def test_wordpress_verifies_stock_disclosure_before_publishing(photo, article, review, context):
    from test_wordpress import client_fixture

    wp = client_fixture(article, review, context)
    result = wp.create_post(
        article=article | {"review": review},
        source_url="https://example.test/source",
        source_name="Library",
        related=[],
        featured_media_id=123,
        image_credit=photo,
    )
    assert result["status"] == "publish"
    staged = next(
        c.kwargs["json"] for c in wp._request.call_args_list if c.args == ("POST", "posts")
    )
    assert staged["content"].startswith("<p><em>Stock illustration;")
    assert photo["photo_url"] in staged["content"]


def test_wordpress_blocks_if_stock_disclosure_removed(photo, article, review, context):
    from test_wordpress import client_fixture

    wp = client_fixture(article, review, context)
    original = wp._request.side_effect

    def remove_credit(method, endpoint, **kwargs):
        value = original(method, endpoint, **kwargs)
        if method == "GET" and endpoint == "posts/456":
            value["content"]["raw"] = article["body"]
        return value

    wp._request.side_effect = remove_credit
    with pytest.raises(RuntimeError, match="altered content"):
        wp.create_post(
            article=article | {"review": review},
            source_url="https://example.test/source",
            source_name="Library",
            related=[],
            featured_media_id=123,
            image_credit=photo,
        )
    assert not any(
        c.kwargs.get("json") == {"status": "publish"} for c in wp._request.call_args_list
    )
