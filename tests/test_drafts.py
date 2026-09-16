"""Regressions for actual draft copy, imported media and private review diagnostics."""

from copy import deepcopy
from unittest.mock import Mock

import pytest
from test_pipeline import entry_for, setup_pipeline
from test_routing import assessment_for
from test_wordpress import client_fixture

from rss_to_wp import cli
from rss_to_wp.drafts import save_editorial_draft
from rss_to_wp.editorial import DraftCopy, DraftReview, draft_presentation_issues
from rss_to_wp.rewriter import OpenAIRewriter


def bundle(article, review, image_bytes):
    assessment = assessment_for(article)
    source = {
        "source_id": 1,
        "source_url": "https://example.test/source",
        "source_name": "Corinth Library",
        "source_published_at": "2026-09-16T12:00:00-05:00",
        "content": article["body"],
        "assessment": assessment.model_dump(),
        "image_urls": ["https://example.test/image.jpg"],
    }
    prepared = {
        "copy": {
            "headline": article["headline"],
            "sections": [{"source_id": 1, "heading": "", "paragraphs": [assessment.summary]}],
        },
        "review": {
            "faithful": True,
            "reader_facing": True,
            "optional_suggestions": [],
            "issues": [],
            "image_id": 1,
            "image_alt": review["image_alt"],
            "image_caption": review["image_caption"],
            "image_reason": "Clear source photograph",
        },
    }
    return dict(
        writer=None,
        sources=[source],
        images=[{"source_id": 1, "url": source["image_urls"][0], "bytes": image_bytes}],
        assessment=assessment.model_dump(),
        issues=["Confirm opening date before publication"],
        prepared=prepared,
        dry_run=False,
    )


def test_imported_featured_image_and_clean_copy_are_verified(article, review, context, image_bytes):
    wp = client_fixture(article, review, context)
    wp.upload_media = Mock(return_value=123)
    result = save_editorial_draft(wp=wp, **bundle(article, review, image_bytes))
    assert result["status"] == "draft" and result["review_issues"]
    payload = next(
        c.kwargs["json"] for c in wp._request.call_args_list if c.args == ("POST", "posts")
    )
    assert payload["featured_media"] == 123
    assert payload["title"] == article["headline"]
    assert "Confirm opening date" not in payload["content"]
    assert "Source publication time" not in payload["content"]
    assert "Source image" not in payload["content"]
    assert "Source: <a" in payload["content"]
    wp.upload_media.assert_called_once()
    assert not wp._is_staging_draft(
        {**payload, "content": {"raw": payload["content"]}}, "https://example.test/source"
    )


@pytest.mark.parametrize(
    "defect", ["faithful", "reader_facing", "issues", "memo", "caption", "image_id", "upload"]
)
def test_failed_draft_or_image_never_creates_wordpress_post(
    defect, article, review, context, image_bytes
):
    wp = client_fixture(article, review, context)
    wp.upload_media = Mock(return_value=123)
    args = bundle(article, review, image_bytes)
    check = args["prepared"]["review"]
    if defect in {"faithful", "reader_facing"}:
        check[defect] = False
    elif defect == "issues":
        check["issues"] = ["The opening date is unsupported"]
    elif defect == "memo":
        args["prepared"]["copy"]["sections"][0]["paragraphs"] = [
            "Editorial review required before publication."
        ]
    elif defect == "caption":
        check["image_caption"] = "Working copy; verify before publishing."
    elif defect == "image_id":
        check["image_id"] = 2
    else:
        wp.upload_media.return_value = None
    with pytest.raises(RuntimeError):
        save_editorial_draft(wp=wp, **args)
    assert not any(c.args[0] == "POST" for c in wp._request.call_args_list)


def test_no_relevant_image_remains_private_note(article, review, context, image_bytes):
    wp = client_fixture(article, review, context)
    wp.upload_media = Mock()
    args = bundle(article, review, image_bytes)
    args["prepared"]["review"].update(
        image_id=0, image_alt="", image_caption="", image_reason="Unrelated photograph"
    )
    result = save_editorial_draft(wp=wp, **args)
    assert any("Unrelated photograph" in n for n in result["review_issues"])
    payload = next(
        c.kwargs["json"] for c in wp._request.call_args_list if c.args == ("POST", "posts")
    )
    assert payload["featured_media"] == 0 and "Unrelated photograph" not in payload["content"]
    wp.upload_media.assert_not_called()


def test_draft_review_reads_original_pixels_and_can_choose_second_image(
    article, review, image_bytes
):
    args = bundle(article, review, image_bytes)
    prepared = args["prepared"]
    prepared["review"]["image_id"] = 2
    writer = OpenAIRewriter("unused")
    writer._request = Mock(
        side_effect=[
            DraftCopy.model_validate(prepared["copy"]),
            DraftReview.model_validate(prepared["review"]),
        ]
    )
    result = writer.prepare_editorial_draft(args["sources"], args["images"] * 2)
    assert result["review"]["image_id"] == 2
    parts = writer._request.call_args.args[2]
    assert sum(p["type"] == "image_url" for p in parts) == 2
    assert all(p["image_url"]["detail"] == "high" for p in parts if p["type"] == "image_url")


@pytest.mark.parametrize("fixed", [True, False])
def test_source_packet_gets_at_most_one_copyedit_before_independent_review(
    fixed, article, review, image_bytes
):
    args = bundle(article, review, image_bytes)
    clean = DraftCopy.model_validate(args["prepared"]["copy"])
    bad = clean.model_copy(deep=True)
    bad.sections[0].paragraphs.append("A graphic shows example renewal dates and product(s).")
    writer = OpenAIRewriter("unused")
    writer._request = Mock(
        side_effect=[
            bad,
            clean if fixed else bad,
            DraftReview.model_validate(args["prepared"]["review"]),
        ]
    )
    if fixed:
        result = writer.prepare_editorial_draft(args["sources"], args["images"])
        assert result["copy"] == clean.model_dump()
        assert writer._request.call_count == 3
    else:
        with pytest.raises(RuntimeError, match="one copyediting pass"):
            writer.prepare_editorial_draft(args["sources"], args["images"])
        assert writer._request.call_count == 2


@pytest.mark.parametrize(
    "paragraph",
    [
        "Jackson averaged 97°F versus 86°F/90°F/91°F.",
        "A graphic titled Enroll in AutoRenew shows a sample checkout.",
        "The source includes attached tables of temperatures.",
    ],
)
def test_live_source_packet_examples_are_caught(paragraph, article, review, image_bytes):
    copy = DraftCopy.model_validate(bundle(article, review, image_bytes)["prepared"]["copy"])
    copy.sections[0].paragraphs = [paragraph]
    assert draft_presentation_issues(copy) == [paragraph]


def test_incidental_unknowns_do_not_block_roundup_admission(
    monkeypatch, settings, feed_config, article, review, context, image_bytes
):
    wp, writer = setup_pipeline(monkeypatch, article, review, context, image_bytes)
    reading = assessment_for(article, route="roundup")
    reading.omitted_details = ["Podcast runtime is not supplied", "The graphic has no timestamp"]
    writer.assess_source.side_effect = None
    writer.assess_source.return_value = reading
    result = cli.process_entry(entry_for(article), feed_config, settings, writer, wp, False, Mock())
    assert result["queued_roundup"]
    wp.create_editorial_draft.assert_not_called()


@pytest.mark.parametrize("changed", ["status", "author", "body", "modified", "marker"])
def test_manual_repair_cannot_overwrite_published_or_edited_drafts(
    changed, article, review, context, image_bytes
):
    wp = client_fixture(article, review, context)
    args = bundle(article, review, image_bytes)
    wp.upload_media = Mock(return_value=123)
    save_editorial_draft(wp=wp, **args)
    saved = wp._request("GET", "posts/456", params={"context": "edit"})
    saved["modified_gmt"] = "2026-09-16T12:00:00"
    original_request = wp._request.side_effect
    current = deepcopy(saved)
    if changed == "status":
        current["status"] = "publish"
    elif changed == "author":
        current["author"] = 99
    elif changed in {"body", "marker"}:
        current["content"]["raw"] = "Human edited text"
    else:
        current["modified_gmt"] = "2026-09-16T12:05:00"
    wp._request.reset_mock()
    wp._request.side_effect = lambda method, endpoint, **kwargs: (
        current if endpoint == "posts/456" else original_request(method, endpoint, **kwargs)
    )
    with pytest.raises(RuntimeError, match="repair stopped"):
        save_editorial_draft(wp=wp, replace_existing=saved, **args)
    assert not any(c.args[0] == "POST" for c in wp._request.call_args_list)
