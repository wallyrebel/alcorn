"""Three-way routing, image evidence, and non-publishable review drafts."""

from io import BytesIO
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from PIL import Image
from test_editorial import reply
from test_editorial import writer as model_writer
from test_pipeline import entry_for, image_response, setup_pipeline
from test_wordpress import client_fixture

from rss_to_wp import cli
from rss_to_wp.editorial import (
    DraftRequiredError,
    SourceAssessment,
    SourceIssue,
    validate_assessment,
)
from rss_to_wp.images.downloader import download_image, featured_size
from rss_to_wp.images.rss_extractor import find_rss_images
from rss_to_wp.rewriter.openai_client import OpenAIRewriter


def assessment_for(article, *, route="draft", image_count=1):
    return SourceAssessment(
        route=route,
        requires_immediate_attention=False,
        reason="Useful official notice needs editorial verification",
        headline=article["headline"],
        summary="Corinth Library announced a new room for Alcorn County residents.",
        category_slugs=["corinth-news"],
        tags=["Corinth Library"],
        image_readings=[
            {
                "image_id": i,
                "facts": ["The official notice announces a new reading room."],
                "uncertainties": [],
            }
            for i in range(1, image_count + 1)
        ],
        uncertainties=[],
        omitted_details=[],
    )


def test_image_only_notice_is_assessed_and_saved_as_draft(
    monkeypatch, settings, feed_config, article, review, context, image_bytes
):
    wp, writer = setup_pipeline(monkeypatch, article, review, context, image_bytes)
    writer.assess_source.side_effect = None
    writer.assess_source.return_value = assessment_for(article)
    entry = entry_for(article) | {"summary": '<img src="https://example.test/notice.jpg">'}
    result = cli.process_entry(entry, feed_config, settings, writer, wp, False, Mock())
    assert result["status"] == "draft" and result["source_words"] == 0
    assert len(writer.assess_source.call_args.args[3]) == 1
    wp.create_editorial_draft.assert_called_once()
    writer.rewrite.assert_not_called()
    wp.upload_media.assert_called_once()
    wp.create_post.assert_not_called()


@pytest.mark.parametrize("body", ["", "Routine congratulations. " * 100])
def test_reject_never_creates_a_wordpress_draft(
    body, monkeypatch, settings, feed_config, article, review, context, image_bytes
):
    wp, writer = setup_pipeline(monkeypatch, article, review, context, image_bytes)
    writer.assess_source.side_effect = None
    writer.assess_source.return_value = assessment_for(article, route="reject")
    result = cli.process_entry(
        entry_for(article) | {"summary": body}, feed_config, settings, writer, wp, False, Mock()
    )
    assert result["skipped"] and result["cache_rejection"]
    assert result["reason"].startswith("no_real_news_value:")
    wp.create_editorial_draft.assert_not_called()
    wp.create_post.assert_not_called()
    wp.upload_media.assert_not_called()


@pytest.mark.parametrize("where", ["source", "final_review"])
def test_uncertainty_routes_to_draft_not_publication(
    where, monkeypatch, settings, feed_config, article, review, context, image_bytes
):
    wp, writer = setup_pipeline(monkeypatch, article, review, context, image_bytes)
    if where == "source":
        reading = assessment_for(article, route="continue")
        reading.image_readings[0].uncertainties = [
            SourceIssue(detail="Small-print email is ambiguous", blocks_publication=True)
        ]
        writer.assess_source.side_effect = None
        writer.assess_source.return_value = reading
    else:
        writer.rewrite.side_effect = DraftRequiredError("Image-derived date could not be verified")
    result = cli.process_entry(entry_for(article), feed_config, settings, writer, wp, False, Mock())
    assert result["status"] == "draft"
    wp.create_editorial_draft.assert_called_once()
    wp.create_post.assert_not_called()
    wp.upload_media.assert_called_once()


def test_source_graphic_readability_and_featured_size_are_separate(monkeypatch):
    buf = BytesIO()
    Image.new("RGB", (720, 1067), "white").save(buf, "JPEG")
    monkeypatch.setattr("requests.get", Mock(return_value=image_response(buf.getvalue())))
    assert download_image("https://example.test/notice.jpg") is None
    result = download_image("https://example.test/notice.jpg", min_width=200, min_height=200)
    assert result is not None and not featured_size(result[0])


def test_assessment_requires_every_image_to_be_accounted_for(article, context):
    assessment = assessment_for(article, image_count=1)
    with pytest.raises(RuntimeError, match="incomplete"):
        validate_assessment(assessment, context["categories"], 2)
    assessment.image_readings.append(assessment.image_readings[0])
    with pytest.raises(RuntimeError, match="incomplete"):
        validate_assessment(assessment, context["categories"], 2)


def test_multiple_images_collected_without_duplicate_thumbnail():
    entry = {
        "media_content": [{"url": "https://example.test/one.jpg?token=a"}],
        "media_thumbnail": [{"url": "https://example.test/one.jpg?token=b"}],
        "summary": '<img src="https://example.test/one.jpg?token=c"><img src="/two.jpg">',
    }
    assert find_rss_images(entry, "https://example.test/news") == [
        "https://example.test/one.jpg?token=a",
        "https://example.test/two.jpg",
    ]


def test_reader_uses_high_detail_and_independent_editor_gets_originals(
    article, review, context, image_bytes
):
    source_images = [{"url": "https://example.test/notice.jpg", "bytes": image_bytes}]
    client = OpenAIRewriter("unused")
    assessment = assessment_for(article, route="continue")
    client._request = Mock(return_value=assessment)
    assert client.assess_source("Source", article["body"], context, source_images) == assessment
    assert client._request.call_args.args[2][-1]["image_url"]["detail"] == "high"
    client = model_writer(reply(article), reply(review))
    client.rewrite(
        article["body"],
        article["headline"],
        context=context | {"source_assessment": assessment.model_dump()},
        image_bytes=image_bytes,
        source_images=source_images,
    )
    calls = client.client.chat.completions.create.call_args_list
    assert calls[1].kwargs["messages"][1]["content"][-1]["image_url"]["detail"] == "high"


def test_review_draft_dry_run_has_no_writes(
    monkeypatch, settings, feed_config, article, review, context, image_bytes
):
    wp, writer = setup_pipeline(monkeypatch, article, review, context, image_bytes)
    writer.assess_source.side_effect = None
    writer.assess_source.return_value = assessment_for(article)
    result = cli.process_entry(entry_for(article), feed_config, settings, writer, wp, True, Mock())
    assert result["intended_status"] == "draft" and result["review_issues"]
    wp.create_editorial_draft.assert_not_called()
    wp.create_post.assert_not_called()


def test_draft_cap_defers_without_caching_or_writing(
    monkeypatch, settings, feed_config, article, review, context, image_bytes
):
    wp, writer = setup_pipeline(monkeypatch, article, review, context, image_bytes)
    writer.assess_source.side_effect = None
    writer.assess_source.return_value = assessment_for(article)
    result = cli.process_entry(
        entry_for(article),
        feed_config,
        settings,
        writer,
        wp,
        False,
        Mock(),
        budget={"posts": 0, "candidates": 0, "drafts": 3},
    )
    assert result["reason"] == "draft_budget_exhausted" and not result.get("cache_rejection")
    wp.create_editorial_draft.assert_not_called()


def test_drafts_do_not_consume_publication_quota(monkeypatch, settings, feed_config, article):
    feed_config.max_per_run = 10
    settings.max_candidates_per_run = 10
    feed = SimpleNamespace(
        entries=[entry_for(article) | {"link": f"https://example.test/{i}"} for i in range(8)],
        feed={"title": "Corinth Library on Facebook", "link": feed_config.source_url},
    )
    monkeypatch.setattr(cli, "parse_feed", Mock(return_value=feed))
    store = Mock()
    store.is_processed.return_value = store.source_seen.return_value = False
    store.rejection_reason.return_value = None

    def result(*args, budget, **kwargs):
        budget["candidates"] += 1
        return {
            "id": budget["candidates"],
            "status": "draft" if budget["candidates"] <= 3 else "publish",
        }

    monkeypatch.setattr(cli, "process_entry", Mock(side_effect=result))
    budget = {"posts": 0, "candidates": 0}
    assert cli.process_feed(
        feed_config, settings, store, Mock(), Mock(), False, 48, Mock(), budget=budget
    ) == (8, 0, 0)
    assert budget == {"posts": 5, "drafts": 3, "candidates": 8}


def hold(wp, assessment):
    return wp.create_editorial_draft(
        assessment=assessment.model_dump(),
        source_url="https://example.test/source",
        source_name="Library",
        issues=["Confirm opening date and supply a featured image"],
        source_images=["https://example.test/original.jpg"],
        source_published_at="2026-09-15T12:00:00-05:00",
        prepared_copy={
            "headline": assessment.headline,
            "sections": [{"source_id": 1, "heading": "", "paragraphs": [assessment.summary]}],
        },
    )


def test_wordpress_hold_is_verified_draft_with_exact_author(article, review, context):
    wp = client_fixture(article, review, context)
    result = hold(wp, assessment_for(article))
    assert result["status"] == "draft" and "/wp-admin/" in result["link"]
    writes = [c for c in wp._request.call_args_list if c.args[0] == "POST"]
    assert len(writes) == 1
    payload = writes[0].kwargs["json"]
    assert payload["status"] == "draft" and payload["author"] == 1
    assert "Editorial review required" not in payload["content"]
    assert "Confirm opening date" not in payload["content"]
    assert not payload["title"].startswith("[Review]")
    assert "editorial-hold:v1:" in payload["content"]
    assert "quality-v1:" not in payload["content"]
    wp._seo_request.assert_not_called()
    assert not wp._is_staging_draft(
        {"status": "draft", "author": 1, "content": {"raw": payload["content"]}},
        "https://example.test/source",
    )


def test_existing_review_draft_never_overwritten_or_promoted(article, review, context):
    wp = client_fixture(article, review, context)
    wp.find_source_posts.return_value = [
        {
            "id": 456,
            "status": "draft",
            "author": 1,
            "content": {"raw": "<!-- rss-to-wp:editorial-hold:v1:any -->"},
        }
    ]
    assert hold(wp, assessment_for(article))["duplicate"]
    assert wp.check_duplicate_by_source_url("https://example.test/source")
    assert not any(c.args[0] == "POST" for c in wp._request.call_args_list)


def test_rejected_source_cannot_enter_wordpress_queue(article, review, context):
    wp = client_fixture(article, review, context)
    with pytest.raises(ValueError):
        hold(wp, assessment_for(article, route="reject"))
    assert wp._request.call_count == 0


def test_unsupported_optional_draft_tags_are_dropped(article, context, image_bytes):
    assessment = assessment_for(article, route="continue")
    assessment.tags = ["Corinth Library", "Unmentioned Tag", "corinth library"]
    client = OpenAIRewriter("unused")
    client._request = Mock(return_value=assessment)
    result = client.assess_source("Source", article["body"], context, [{"bytes": image_bytes}])
    assert result.tags == ["Corinth Library"]


def test_video_enclosure_does_not_hide_the_source_graphic():
    entry = {
        "media_content": [{"url": "https://video.xx.fbcdn.net/video.mp4", "medium": "video"}],
        "summary": '<img src="https://example.test/notice.jpg">',
    }
    assert find_rss_images(entry) == ["https://example.test/notice.jpg"]


def test_image_evidence_changes_invalidate_rejection_fingerprint():
    from rss_to_wp.editorial import fingerprint

    a = fingerprint("Notice", '<img src="https://cdn.test/one.jpg?expires=1">')
    b = fingerprint("Notice", '<img src="https://cdn.test/one.jpg?expires=2">')
    c = fingerprint("Notice", '<img src="https://cdn.test/two.jpg?expires=2">')
    assert a == b and a != c


def test_unavailable_image_is_retryable_error_not_permanent_rejection(
    monkeypatch, settings, feed_config, article, review, context, image_bytes
):
    wp, writer = setup_pipeline(monkeypatch, article, review, context, image_bytes)
    cli.download_image.return_value = None
    with pytest.raises(RuntimeError, match="retry later"):
        cli.process_entry(entry_for(article), feed_config, settings, writer, wp, False, Mock())
    writer.assess_source.assert_not_called()
    wp.create_editorial_draft.assert_not_called()
    wp.create_post.assert_not_called()


@pytest.mark.parametrize("route", ["continue", "reject"])
def test_unseen_graphics_require_draft_even_if_first_three_look_ignorable(
    route, monkeypatch, settings, feed_config, article, review, context, image_bytes
):
    wp, writer = setup_pipeline(monkeypatch, article, review, context, image_bytes)
    cli.find_rss_images.return_value = [f"https://example.test/{i}.jpg" for i in range(4)]
    reading = assessment_for(article, route=route, image_count=3)
    if route == "reject":
        reading.headline = reading.summary = ""
        reading.category_slugs = reading.tags = []
    writer.assess_source.side_effect = None
    writer.assess_source.return_value = reading
    result = cli.process_entry(entry_for(article), feed_config, settings, writer, wp, False, Mock())
    assert result["status"] == "draft"
    assert "exceed the automatic review limit" in result["reason"]
    kwargs = wp.create_editorial_draft.call_args.kwargs
    assert len(kwargs["source_images"]) == 3
    validate_assessment(SourceAssessment.model_validate(kwargs["assessment"]), wp.categories, 3)
    writer.rewrite.assert_not_called()
    wp.create_post.assert_not_called()
    wp.upload_media.assert_called_once()
