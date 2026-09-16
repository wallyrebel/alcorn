"""Quality-first RSS publishing with bounded cost and auditable decisions."""

from __future__ import annotations

import json
from datetime import datetime, timezone
from html import escape
from pathlib import Path
from typing import Optional

import pendulum
import typer
from dotenv import load_dotenv

from rss_to_wp import __version__
from rss_to_wp.config import (
    AppSettings,
    FeedConfig,
    get_app_settings,
    get_data_dir,
    load_feeds_config,
)
from rss_to_wp.content_policy import ContentRejectedError, plain_text, require_usable_source
from rss_to_wp.editorial import canonical_url, fingerprint, render_content, similar_story
from rss_to_wp.feeds import (
    generate_entry_key,
    get_entry_content,
    get_entry_link,
    get_entry_title,
    parse_feed,
    pick_entries,
)
from rss_to_wp.feeds.filter import parse_entry_date
from rss_to_wp.images import download_image, find_rss_image
from rss_to_wp.local_categories import additional_local_categories
from rss_to_wp.rewriter import OpenAIRewriter
from rss_to_wp.storage import DedupeStore
from rss_to_wp.utils import setup_logging
from rss_to_wp.wordpress import WordPressClient

load_dotenv()
app = typer.Typer(name="rss-to-wp", add_completion=False)


@app.callback()
def main():
    """Publish only articles that pass editorial, image and WordPress verification."""


@app.command()
def run(
    config: Path = typer.Option(Path("feeds.yaml"), "--config", "-c"),
    dry_run: bool = typer.Option(False, "--dry-run", "-n"),
    single_feed: Optional[str] = typer.Option(None, "--single-feed", "-f"),
    hours: int = typer.Option(48, "--hours", "-h", min=1, max=72),
):
    """Evaluate feeds; dry runs perform GETs and model calls but no WordPress writes."""
    logger = setup_logging()
    try:
        settings = get_app_settings()
        logger = setup_logging(level=settings.log_level, log_file=settings.log_file)
        feeds = load_feeds_config(config).feeds
        if single_feed:
            feeds = [f for f in feeds if f.name.casefold() == single_feed.casefold()]
            if not feeds:
                raise ValueError("Requested feed not found")
        wp = WordPressClient(
            settings.wordpress_base_url,
            settings.wordpress_username,
            settings.wordpress_app_password,
            settings.wordpress_post_status,
            settings.wordpress_author_id,
            settings.wordpress_author_name,
            settings.min_quality_score,
            settings.min_article_words,
        )
        wp.preflight()
    except Exception as exc:
        logger.error("preflight_failed", error=str(exc))
        raise typer.Exit(1) from exc
    writer = OpenAIRewriter(
        settings.openai_api_key, settings.openai_model, review_model=settings.openai_review_model
    )
    store = DedupeStore()
    budget = {"candidates": 0, "posts": 0}
    report = {
        "version": __version__,
        "dry_run": dry_run,
        "started_at": datetime.now(timezone.utc).isoformat(),
        "writer_model": settings.openai_model,
        "review_model": settings.openai_review_model,
        "decisions": [],
    }
    # Rotate feed priority hourly so a small global budget cannot starve later feeds.
    offset = int(datetime.now(timezone.utc).timestamp() // 3600) % max(len(feeds), 1)
    feeds = feeds[offset:] + feeds[:offset]
    totals = [0, 0, 0]
    try:
        for feed in feeds:
            if (
                budget["candidates"] >= settings.max_candidates_per_run
                or budget["posts"] >= settings.max_posts_per_run
            ):
                break
            try:
                counts = process_feed(
                    feed,
                    settings,
                    store,
                    writer,
                    wp,
                    dry_run,
                    hours,
                    logger,
                    budget=budget,
                    decisions=report["decisions"],
                )
            except Exception as exc:
                logger.error("feed_failed", feed=feed.name, error=str(exc))
                report["decisions"].append({"feed": feed.name, "error": str(exc)})
                counts = (0, 0, 1)
            totals = [a + b for a, b in zip(totals, counts)]
    finally:
        report.update(processed=totals[0], skipped=totals[1], errors=totals[2], budget=budget)
        path = get_data_dir() / ("dry-run-report.json" if dry_run else "run-report.json")
        path.write_text(json.dumps(report, indent=2, ensure_ascii=False), encoding="utf-8")
        logger.info(
            "run_complete",
            report=str(path),
            processed=totals[0],
            skipped=totals[1],
            errors=totals[2],
        )
    # Rejections are healthy. Service/publishing failures must be visible in Actions.
    if totals[2]:
        raise typer.Exit(1)


def process_feed(
    feed_config: FeedConfig,
    settings: AppSettings,
    dedupe_store: DedupeStore,
    rewriter: OpenAIRewriter,
    wp_client: WordPressClient,
    dry_run: bool,
    hours: int,
    logger,
    published_articles=None,
    *,
    budget=None,
    decisions=None,
):
    budget = budget if budget is not None else {"candidates": 0, "posts": 0}
    decisions = decisions if decisions is not None else []
    feed = parse_feed(feed_config.url)
    if feed is None:
        return 0, 0, 1
    if not feed.entries:
        return 0, 0, 0
    # A renamed/replaced feed must not silently inherit the old source attribution.
    actual_source = plain_text(feed.feed.get("title", "")).removesuffix(" on Facebook")
    if (
        not feed_config.primary_source
        or actual_source != feed_config.source_name
        or canonical_url(feed.feed.get("link", "")) != canonical_url(feed_config.source_url)
    ):
        logger.error("feed_identity_mismatch", feed=feed_config.name)
        return 0, 0, 1
    entries = pick_entries(
        feed.entries, max_count=30, hours_window=hours, timezone=settings.timezone
    )
    processed = skipped = errors = attempted = 0
    for entry in entries:
        if (
            attempted >= feed_config.max_per_run
            or budget["candidates"] >= settings.max_candidates_per_run
            or budget["posts"] >= settings.max_posts_per_run
        ):
            break
        title, content = get_entry_title(entry), get_entry_content(entry)
        source_url = get_entry_link(entry) or ""
        decision = {"feed": feed_config.name, "source_url": source_url, "source_title": title}
        fp = fingerprint(title, content + feed_config.source_name + str(entry.get("published", "")))
        try:
            key = generate_entry_key(entry, feed_config.url)
            if dedupe_store.is_processed(key) or dedupe_store.source_seen(source_url):
                result = {"skipped": True, "reason": "already_processed"}
            elif dedupe_store.rejection_reason(fp):
                result = {
                    "skipped": True,
                    "reason": "cached_rejection: " + dedupe_store.rejection_reason(fp),
                }
            else:
                before = budget["candidates"]
                result = process_entry(
                    entry,
                    feed_config,
                    settings,
                    rewriter,
                    wp_client,
                    dry_run,
                    logger,
                    budget=budget,
                )
                attempted += budget["candidates"] - before
            decision.update(result)
            if result.get("skipped") or result.get("duplicate"):
                skipped += 1
                if not dry_run and result.get("cache_rejection"):
                    dedupe_store.record_rejection(fp, result["reason"])
            else:
                processed += 1
                budget["posts"] += 1
                if not dry_run:
                    dedupe_store.mark_processed(
                        key,
                        feed_config.url,
                        title,
                        source_url,
                        result.get("id"),
                        result.get("link"),
                    )
        except Exception as exc:
            errors += 1
            decision.update(error=str(exc))
            logger.error("entry_processing_error", title=title[:80], error=str(exc))
        decisions.append(decision)
    return processed, skipped, errors


def process_entry(
    entry, feed_config, settings, rewriter, wp_client, dry_run, logger, *, budget=None
):
    title, content = get_entry_title(entry), get_entry_content(entry)
    budget = budget if budget is not None else {"candidates": 0, "posts": 0}
    try:
        other_fields = [entry.get("summary", ""), entry.get("description", "")]
        other_fields.extend(item.get("value", "") for item in entry.get("content", []))
        require_usable_source(title, content, *other_fields)
        if len(plain_text(content).split()) < settings.min_source_words:
            raise ContentRejectedError("source_too_thin")
        if not feed_config.source_name or not feed_config.primary_source:
            raise ContentRejectedError("unverified_source_identity")
        link = canonical_url(get_entry_link(entry) or "")
        published = parse_entry_date(entry)
        if not published:
            raise ContentRejectedError("missing_source_date")
        if wp_client.check_duplicate_by_source_url(link):
            return {"duplicate": True, "reason": "source_already_on_wordpress"}
        related = wp_client.related_candidates(title, content, source_name=feed_config.source_name)
        if any(similar_story(title, p["title"]) for p in related):
            return {"duplicate": True, "reason": "story_already_covered"}
        image_url = find_rss_image(entry, base_url=link)
        if not image_url:
            raise ContentRejectedError("source_image_required")
        image_result = download_image(image_url)
        if not image_result:
            # A CDN outage can recover; do not cache it as an editorial rejection.
            return {"skipped": True, "reason": "source_image_unavailable_or_undersized"}
        image_bytes, _, _ = image_result
        budget["candidates"] += 1
        context = {
            "source_name": feed_config.source_name,
            "source_url": link,
            "source_published_at": pendulum.instance(published)
            .in_timezone(settings.timezone)
            .isoformat(),
            "current_time": pendulum.now(settings.timezone).isoformat(),
            "categories": wp_client.categories,
            "related_posts": related,
            "source_local_category_ids": additional_local_categories(
                settings.wordpress_base_url, title, content
            ),
        }
        article = rewriter.rewrite(
            content,
            title,
            feed_config.use_original_title,
            context=context,
            image_bytes=image_bytes,
            min_source_words=settings.min_source_words,
            min_article_words=settings.min_article_words,
            min_quality_score=settings.min_quality_score,
        )
        if dry_run:
            return {
                "preview": True,
                "article": article,
                "image_url": image_url,
                "content": render_content(article, link, feed_config.source_name, related),
            }
        caption = escape(article["review"]["image_caption"])
        caption += (
            f' Source: <a href="{escape(link, quote=True)}">{escape(feed_config.source_name)}</a>.'
        )
        media_id = wp_client.upload_media(
            image_bytes, article["slug"] + ".jpg", article["review"]["image_alt"], caption=caption
        )
        if not media_id:
            raise RuntimeError("Featured image upload/metadata failed; publication blocked")
        post = wp_client.create_post(
            article=article,
            source_url=link,
            source_name=feed_config.source_name,
            related=related,
            featured_media_id=media_id,
        )
        return {k: post[k] for k in ("id", "link", "status", "duplicate") if k in post}
    except ContentRejectedError as exc:
        logger.info("content_rejected", title=title[:80], reason=str(exc))
        return {"skipped": True, "reason": str(exc), "cache_rejection": True}


@app.command()
def status():
    store = DedupeStore()
    typer.echo(f"Processed entries: {store.get_processed_count()}")
    for entry in store.get_recent_entries(limit=10):
        typer.echo(f"{entry['entry_title']} | {entry['wp_post_url']}")


@app.command()
def clear_db(confirm: bool = typer.Option(False, "--yes", "-y")):
    if confirm or typer.confirm("Clear local publishing history and editorial decisions?"):
        typer.echo(f"Cleared {DedupeStore().clear_all()} entries.")


if __name__ == "__main__":
    app()
