from io import BytesIO
from types import SimpleNamespace
from unittest.mock import Mock

import pendulum
import pytest
from PIL import Image

from rss_to_wp import cli
from rss_to_wp.content_policy import plain_text
from rss_to_wp.editorial import SourceAssessment, fingerprint
from rss_to_wp.images.downloader import download_image
from rss_to_wp.storage import DedupeStore


def entry_for(article):
    return {
        "title": article["headline"],
        "summary": article["body"],
        "link": "https://example.test/library/news",
        "published": pendulum.now("UTC").isoformat(),
    }


def setup_pipeline(monkeypatch, article, review, context, image_bytes):
    wp = Mock(categories=context["categories"])
    wp.check_duplicate_by_source_url.return_value = False
    wp.related_candidates.return_value = []
    wp.upload_media.return_value = 123
    wp.create_post.return_value = {
        "id": 456,
        "link": "https://example.test/news",
        "status": "publish",
    }
    writer = Mock()
    writer.rewrite.return_value = article | {"review": review}
    writer.assess_source.side_effect = lambda title, content, context, images: SourceAssessment(
        route="continue",
        requires_immediate_attention=False,
        reason="Substantial local service news",
        headline=article["headline"],
        summary=plain_text(article["body"]),
        category_slugs=article["category_slugs"],
        tags=article["tags"],
        image_readings=[
            {"image_id": i, "facts": [], "uncertainties": []} for i in range(1, len(images) + 1)
        ],
        uncertainties=[],
    )
    wp.create_editorial_draft.return_value = {
        "id": 789,
        "status": "draft",
        "link": "https://example.test/wp-admin/post.php?post=789&action=edit",
    }
    monkeypatch.setattr(
        cli, "find_rss_images", Mock(return_value=["https://example.test/photo.jpg"])
    )
    monkeypatch.setattr(
        cli, "download_image", Mock(return_value=(image_bytes, "photo.jpg", "image/jpeg"))
    )
    return wp, writer


def test_dry_run_uses_live_reads_but_no_writes(
    monkeypatch, settings, feed_config, article, review, context, image_bytes
):
    wp, writer = setup_pipeline(monkeypatch, article, review, context, image_bytes)
    result = cli.process_entry(entry_for(article), feed_config, settings, writer, wp, True, Mock())
    assert result["preview"]
    wp.upload_media.assert_not_called()
    wp.create_post.assert_not_called()
    wp.get_or_create_tags.assert_not_called()


@pytest.mark.parametrize("failure", ["missing", "download", "upload", "duplicate"])
def test_images_and_duplicates_block_publication(
    failure, monkeypatch, settings, feed_config, article, review, context, image_bytes
):
    wp, writer = setup_pipeline(monkeypatch, article, review, context, image_bytes)
    if failure == "missing":
        cli.find_rss_images.return_value = []
    elif failure == "download":
        cli.download_image.return_value = None
    elif failure == "upload":
        wp.upload_media.return_value = None
    else:
        wp.check_duplicate_by_source_url.return_value = True
    try:
        result = cli.process_entry(
            entry_for(article), feed_config, settings, writer, wp, False, Mock()
        )
        assert result.get("skipped") or result.get("duplicate") or result.get("status") == "draft"
    except RuntimeError:
        assert failure in {"upload", "download"}
    wp.create_post.assert_not_called()
    if failure != "upload":
        writer.rewrite.assert_not_called()


def test_wordpress_read_outage_cannot_assume_unique(
    monkeypatch, settings, feed_config, article, review, context, image_bytes
):
    wp, writer = setup_pipeline(monkeypatch, article, review, context, image_bytes)
    wp.check_duplicate_by_source_url.side_effect = RuntimeError("WordPress unavailable")
    with pytest.raises(RuntimeError):
        cli.process_entry(entry_for(article), feed_config, settings, writer, wp, False, Mock())
    writer.rewrite.assert_not_called()
    wp.create_post.assert_not_called()


def test_all_source_fields_checked_before_model(
    monkeypatch, settings, feed_config, article, review, context, image_bytes
):
    wp, writer = setup_pipeline(monkeypatch, article, review, context, image_bytes)
    entry = entry_for(article) | {
        "content": [{"value": article["body"]}],
        "summary": "Access denied",
    }
    result = cli.process_entry(entry, feed_config, settings, writer, wp, False, Mock())
    assert result["skipped"]
    assert wp.mock_calls == []
    writer.rewrite.assert_not_called()


@pytest.mark.parametrize("dry_run", [True, False])
def test_feed_history_and_rejection_cache_separate(
    tmp_path, monkeypatch, settings, feed_config, article, dry_run
):
    entry = entry_for(article)
    feed = SimpleNamespace(
        entries=[entry],
        feed={"title": "Corinth Library on Facebook", "link": feed_config.source_url},
    )
    monkeypatch.setattr(cli, "parse_feed", Mock(return_value=feed))
    result = {"skipped": True, "reason": "editor_declined", "cache_rejection": True}
    monkeypatch.setattr(cli, "process_entry", Mock(return_value=result))
    store = DedupeStore(tmp_path / "history.db")
    assert cli.process_feed(feed_config, settings, store, Mock(), Mock(), dry_run, 48, Mock()) == (
        0,
        1,
        0,
    )
    assert store.get_processed_count() == 0
    fp = fingerprint(
        entry["title"], entry["summary"] + feed_config.source_name + entry["published"]
    )
    assert bool(store.rejection_reason(fp)) is not dry_run
    assert not store.rejection_reason(fingerprint(entry["title"], "repaired source"))


def test_feed_identity_mismatch_stops_processing(monkeypatch, settings, feed_config):
    monkeypatch.setattr(
        cli,
        "parse_feed",
        Mock(
            return_value=SimpleNamespace(
                entries=[{}],
                feed={"title": "Unrelated page on Facebook", "link": feed_config.source_url},
            )
        ),
    )
    writer = Mock()
    assert cli.process_feed(feed_config, settings, Mock(), writer, Mock(), False, 48, Mock()) == (
        0,
        0,
        1,
    )
    assert writer.mock_calls == []


def image_response(data):
    response = Mock(headers={})
    response.iter_content.return_value = [data]
    return response


def test_download_rejects_thumbnails_and_size_limit(monkeypatch):
    buf = BytesIO()
    Image.new("RGB", (300, 200)).save(buf, "JPEG")
    response = image_response(buf.getvalue())
    monkeypatch.setattr("requests.get", Mock(return_value=response))
    assert download_image("https://example.test/thumbnail.jpg") is None
    response = image_response(b"x" * 2000)
    monkeypatch.setattr("requests.get", Mock(return_value=response))
    assert download_image("https://example.test/large.jpg", max_size_mb=0.001) is None
    response.close.assert_called_once()


def test_download_uses_verified_mime_and_dimensions(monkeypatch, image_bytes):
    monkeypatch.setattr("requests.get", Mock(return_value=image_response(image_bytes)))
    content, filename, mime = download_image("https://example.test/misleading.png")
    assert mime == "image/jpeg" and filename.endswith(".jpg")
    assert Image.open(BytesIO(content)).size == (1200, 800)


def test_five_post_cap_stops_further_candidates(
    monkeypatch,
    settings,
    feed_config,
    article,
):
    from types import SimpleNamespace

    settings.max_posts_per_run = 5
    feed_config.max_per_run = 10
    feed = SimpleNamespace(
        entries=[entry_for(article) | {"link": f"https://example.test/news/{i}"} for i in range(8)],
        feed={"title": "Corinth Library on Facebook", "link": feed_config.source_url},
    )
    monkeypatch.setattr(cli, "parse_feed", Mock(return_value=feed))
    store = Mock()
    store.is_processed.return_value = False
    store.source_seen.return_value = False
    store.rejection_reason.return_value = None

    def successful(*args, budget, **kwargs):
        budget["candidates"] += 1
        return {
            "id": budget["candidates"],
            "link": "https://example.test/article",
            "status": "publish",
        }

    process = Mock(side_effect=successful)
    monkeypatch.setattr(cli, "process_entry", process)
    budget = {"posts": 0, "candidates": 0}
    counts = cli.process_feed(
        feed_config, settings, store, Mock(), Mock(), False, 48, Mock(), budget=budget
    )
    assert counts == (5, 0, 0)
    assert process.call_count == 5
    assert budget == {"posts": 5, "candidates": 5}
