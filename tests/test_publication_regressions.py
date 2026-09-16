from copy import deepcopy
from io import BytesIO
from unittest.mock import Mock

import pytest
from PIL import Image
from test_editorial import reply
from test_editorial import writer as model_writer
from test_pipeline import entry_for, setup_pipeline
from test_routing import assessment_for
from test_wordpress import client_fixture, publish

from rss_to_wp import cli
from rss_to_wp.content_policy import ContentRejectedError, plain_text
from rss_to_wp.editorial import (
    DraftRequiredError,
    Review,
    SourceAssessment,
    assessment_issues,
    validate_article,
    validate_reader_text,
)
from rss_to_wp.rewriter.openai_client import OpenAIRewriter
from rss_to_wp.wordpress.client import WordPressClient


def test_nonmaterial_image_note_does_not_block_publication(
    monkeypatch, settings, feed_config, article, review, context, image_bytes
):
    wp, writer = setup_pipeline(monkeypatch, article, review, context, image_bytes)
    data = assessment_for(article, route="continue").model_dump()
    data["image_readings"][0]["uncertainties"] = [
        {
            "detail": "None about the material facts; identity is attributed to NEMCC's post.",
            "blocks_publication": False,
        }
    ]
    writer.assess_source.side_effect = None
    writer.assess_source.return_value = SourceAssessment.model_validate(data)
    result = cli.process_entry(entry_for(article), feed_config, settings, writer, wp, False, Mock())
    assert result["status"] == "publish"
    wp.create_editorial_draft.assert_not_called()


@pytest.mark.parametrize(
    "issue",
    [
        "Application deadline unreadable",
        {"detail": "Application deadline unreadable", "blocks_publication": True},
    ],
)
def test_legacy_and_material_uncertainty_still_block(article, issue):
    data = assessment_for(article, route="continue").model_dump()
    data["image_readings"][0]["uncertainties"] = [issue]
    assert assessment_issues(SourceAssessment.model_validate(data)) == [
        "Application deadline unreadable"
    ]


def test_uncertainty_schema_requires_explicit_boolean(article):
    schema = SourceAssessment.model_json_schema()["$defs"]["SourceIssue"]
    assert "blocks_publication" in schema["required"]
    assert schema["properties"]["blocks_publication"]["type"] == "boolean"
    data = assessment_for(article).model_dump()
    data["uncertainties"] = [{"detail": "Unclear date", "blocks_publication": "false"}]
    with pytest.raises(ValueError):
        SourceAssessment.model_validate(data)


def test_one_copyedit_preserves_independent_review(article, review, context, image_bytes):
    broken = article | {
        "headline": "This is an overly long headline " * 5,
        "body": "<p>Corinth Library announced a reading room.</p>",
    }
    writer = model_writer(reply(broken), reply(article), reply(review))
    result = writer.rewrite(
        plain_text(article["body"]), article["headline"], context=context, image_bytes=image_bytes
    )
    assert result["review"]["quality_score"] == 95
    calls = writer.client.chat.completions.create.call_args_list
    assert len(calls) == 3
    assert calls[1].kwargs["max_completion_tokens"] == 3500
    assert calls[2].kwargs["response_format"]["json_schema"]["name"] == "Review"


def test_failed_copyedit_is_not_retried_forever(article, context, image_bytes):
    broken = article | {"headline": "Too long " * 20}
    writer = model_writer(reply(broken), reply(broken))
    with pytest.raises(DraftRequiredError):
        writer.rewrite(
            plain_text(article["body"]),
            article["headline"],
            context=context,
            image_bytes=image_bytes,
        )
    assert writer.client.chat.completions.create.call_count == 2


def test_unused_tag_is_omitted_before_independent_review(article, review, context, image_bytes):
    writer = model_writer(
        reply(article | {"tags": article["tags"] + ["Unsupported training program"]}), reply(review)
    )
    result = writer.rewrite(
        plain_text(article["body"]), article["headline"], context=context, image_bytes=image_bytes
    )
    assert result["tags"] == article["tags"]
    assert writer.client.chat.completions.create.call_count == 2


def test_overlong_seo_uses_complete_measured_option(article, review, context, image_bytes):
    options = {
        key + "_options": ["Overlong " * 50, article[key]]
        for key in ("seo_title", "meta_description", "excerpt")
    }
    writer = model_writer(
        reply(article | {"meta_description": "Too long " * 25}), reply(options), reply(review)
    )
    result = writer.rewrite(
        plain_text(article["body"]), article["headline"], context=context, image_bytes=image_bytes
    )
    assert result["meta_description"] == article["meta_description"]
    calls = writer.client.chat.completions.create.call_args_list
    assert len(calls) == 3
    assert calls[1].kwargs["max_completion_tokens"] == 1500
    assert calls[1].kwargs["response_format"]["json_schema"]["name"] == "ArticleSEO"
    assert calls[-1].kwargs["response_format"]["json_schema"]["name"] == "Review"


def test_no_invented_county_category(article, context):
    with pytest.raises(ContentRejectedError, match="local_category_not_supported"):
        validate_article(
            article | {"body": article["body"].replace("Corinth", "Northeast")},
            context["categories"],
            [],
            150,
        )


def test_draft_rejects_observed_control_character():
    with pytest.raises(RuntimeError, match="Control characters"):
        validate_reader_text("The college\x02s Adult Education program")


def test_public_article_rejects_control_character(article, context):
    with pytest.raises(ContentRejectedError, match="invalid_control_characters"):
        validate_article(
            article | {"body": article["body"].replace("Library", "Library\x02")},
            context["categories"],
            [],
            150,
        )


@pytest.mark.parametrize(
    "size,kind,stock,approved",
    [
        ((720, 1067), "source_photo", False, True),
        ((500, 375), "official_graphic", False, True),
        ((200, 200), "official_graphic", False, False),
        ((400, 375), "source_photo", False, False),
        ((512, 480), "source_photo", False, True),
        ((720, 1067), "pexels_stock", True, False),
        ((720, 1067), "source_photo", True, False),
        ((1200, 800), "pexels_stock", True, True),
    ],
)
def test_image_requirements_before_upload(size, kind, stock, approved, review):
    stream = BytesIO()
    Image.new("RGB", size).save(stream, format="JPEG")
    parsed = Review.model_validate(review | {"image_kind": kind})
    context = {"image_provenance": {"kind": "pexels_stock" if stock else "source"}}
    if approved:
        OpenAIRewriter._check_featured_image(parsed, stream.getvalue(), context)
    else:
        with pytest.raises(DraftRequiredError):
            OpenAIRewriter._check_featured_image(parsed, stream.getvalue(), context)


@pytest.mark.parametrize(
    "width,height,kind,approved",
    [
        (720, 1067, "source_photo", True),
        (500, 375, "official_graphic", True),
        (400, 375, "source_photo", False),
        (512, 480, "source_photo", True),
    ],
)
def test_wordpress_repeats_image_type_floor(
    width, height, kind, approved, article, review, context
):
    review = review | {"image_kind": kind}
    wp = client_fixture(article, review, context)
    request = wp._request.side_effect

    def sized(method, endpoint, **kwargs):
        result = request(method, endpoint, **kwargs)
        if endpoint == "media/123":
            result["media_details"] = {"width": width, "height": height}
        return result

    wp._request.side_effect = sized
    if approved:
        assert publish(wp, article, review)["status"] == "publish"
    else:
        with pytest.raises(RuntimeError, match="Featured image"):
            publish(wp, article, review)
        assert not any(c.args[0] == "POST" for c in wp._request.call_args_list)


def test_trash_is_a_source_duplicate_barrier():
    wp = WordPressClient("https://example.test", "unused", "unused")
    wp._request = Mock(
        return_value=[
            {
                "id": 8,
                "status": "trash",
                "content": {"raw": '<a href="https://example.test/source">Source</a>'},
            }
        ]
    )
    assert wp.check_duplicate_by_source_url("https://example.test/source")
    assert "trash" in wp._request.call_args.kwargs["params"]["status"].split(",")


def hold_fixture(article, review, context):
    import hashlib

    wp = client_fixture(article, review, context)
    marker = hashlib.sha256(b"https://example.test/source").hexdigest()[:12]
    snapshot = {
        "id": 456,
        "status": "draft",
        "author": 1,
        "modified_gmt": "2026-09-16T12:00:00",
        "title": {"raw": "Held story"},
        "content": {"raw": f"<!-- rss-to-wp:editorial-hold:v1:{marker} -->"},
        "excerpt": {"raw": ""},
        "slug": "editorial-review-" + marker,
        "featured_media": 123,
        "categories": [221],
        "tags": [10, 11],
    }
    current = deepcopy(snapshot)
    wp.find_source_posts.return_value = [current]
    request = wp._request.side_effect
    staged = False

    def with_hold(method, endpoint, **kwargs):
        nonlocal staged
        if method == "GET" and endpoint == "posts/456" and not staged:
            return current
        if method == "POST" and endpoint == "posts/456":
            staged = True
        return request(method, endpoint, **kwargs)

    wp._request.side_effect = with_hold
    return wp, snapshot, current


def promote(wp, snapshot, article, review):
    return wp.create_post(
        article=article | {"review": review},
        source_url="https://example.test/source",
        source_name="Corinth Library",
        related=[],
        featured_media_id=123,
        replace_review_draft=snapshot,
    )


def test_explicit_owned_hold_still_stages_and_verifies(article, review, context):
    wp, snapshot, _ = hold_fixture(article, review, context)
    assert promote(wp, snapshot, article, review)["status"] == "publish"
    assert wp._seo_request.call_count == 2
    writes = [c for c in wp._request.call_args_list if c.args[0] == "POST"]
    assert writes[0].args[1] == "posts/456"
    assert writes[0].kwargs["json"]["status"] == "draft"
    assert writes[-1].kwargs["json"] == {"status": "publish"}


@pytest.mark.parametrize(
    "change", ["public", "trash", "modified", "content", "human", "other_coverage", "seo"]
)
def test_explicit_recheck_cannot_bypass_guards(change, article, review, context):
    wp, snapshot, current = hold_fixture(article, review, context)
    if change in ("public", "trash"):
        current["status"] = "publish" if change == "public" else "trash"
    elif change == "modified":
        current["modified_gmt"] = "2026-09-16T13:00:00"
    elif change == "content":
        current["content"]["raw"] += "An editor changed the copy"
    elif change == "human":
        current["content"]["raw"] = "A human draft"
    elif change == "other_coverage":
        wp.find_source_posts.return_value.append({"id": 8, "status": "publish"})
    elif change == "seo":
        wp._seo_request.side_effect = RuntimeError("SEO storage unavailable")
    with pytest.raises(RuntimeError):
        promote(wp, snapshot, article, review)
    assert not any(
        c.kwargs.get("json") == {"status": "publish"} for c in wp._request.call_args_list
    )
