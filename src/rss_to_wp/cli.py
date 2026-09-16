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
from rss_to_wp.drafts import save_editorial_draft
from rss_to_wp.editorial import (
    POLICY_VERSION,
    BriefTooShortError,
    DraftRequiredError,
    assessment_issues,
    canonical_url,
    render_content,
    similar_story,
)
from rss_to_wp.feeds import (
    generate_entry_key,
    get_entry_content,
    get_entry_link,
    get_entry_title,
    parse_feed,
    pick_entries,
)
from rss_to_wp.feeds.filter import parse_entry_date
from rss_to_wp.images import download_image
from rss_to_wp.images.downloader import source_featured_size
from rss_to_wp.images.pexels import PexelsClient, stock_credit
from rss_to_wp.images.rss_extractor import find_rss_images
from rss_to_wp.local_categories import additional_local_categories
from rss_to_wp.rewriter import OpenAIRewriter
from rss_to_wp.roundups import entry_fingerprint, load_pool, process_roundups
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
    roundup_pool = load_pool(store, feeds, hours, dry_run=dry_run)
    budget = {"candidates": 0, "posts": 0, "drafts": 0}
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
                    roundup_pool=roundup_pool,
                )
            except Exception as exc:
                logger.error("feed_failed", feed=feed.name, error=str(exc))
                report["decisions"].append({"feed": feed.name, "error": str(exc)})
                counts = (0, 0, 1)
            totals = [a + b for a, b in zip(totals, counts)]
        roundup_counts = process_roundups(
            roundup_pool,
            feeds,
            settings,
            store,
            writer,
            wp,
            dry_run,
            hours,
            budget,
            report["decisions"],
        )
        totals = [a + b for a, b in zip(totals, roundup_counts)]
    finally:
        report.update(
            processed=totals[0],
            published=budget["posts"],
            drafts=budget.get("drafts", 0),
            skipped=totals[1],
            errors=totals[2],
            budget=budget,
            roundup_waiting=len(roundup_pool),
        )
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
    roundup_pool=None,
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
        title = get_entry_title(entry)
        source_url = get_entry_link(entry) or ""
        decision = {"feed": feed_config.name, "source_url": source_url, "source_title": title}
        fp = entry_fingerprint(entry, feed_config.source_name)
        try:
            key = generate_entry_key(entry, feed_config.url)
            if dedupe_store.is_processed(key) or dedupe_store.source_seen(source_url):
                result = {"skipped": True, "reason": "already_processed"}
            elif dedupe_store.rejection_reason(fp):
                result = {
                    "skipped": True,
                    "reason": "cached_rejection: " + dedupe_store.rejection_reason(fp),
                }
            elif (
                roundup_pool is not None
                and roundup_pool.get(canonical_url(source_url), {}).get("fingerprint") == fp
            ):
                result = {"queued_roundup": True, "reason": "waiting_for_compatible_briefs"}
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
            if result.get("queued_roundup"):
                skipped += 1
                if "candidate" in result:
                    candidate = {**result["candidate"], "entry_key": key, "fingerprint": fp}
                    if roundup_pool is not None:
                        roundup_pool[candidate["source_url"]] = candidate
                    if not dry_run:
                        dedupe_store.queue_roundup(candidate)
            elif result.get("skipped") or result.get("duplicate"):
                skipped += 1
                if not dry_run and result.get("cache_rejection"):
                    dedupe_store.record_rejection(fp, result["reason"])
            else:
                processed += 1
                outcome = result.get("intended_status") if dry_run else result.get("status")
                if outcome == "draft":
                    budget["drafts"] = budget.get("drafts", 0) + 1
                else:
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
    assessment = None
    source_images = []
    context = {}
    audit = {
        "source_words": len(plain_text(content).split()),
        "source_text": plain_text(content)[:18000],
    }

    def queue_brief():
        assessment.route = "roundup"
        audit["assessment"] = assessment.model_dump()
        return {
            "queued_roundup": True,
            "reason": "Useful short brief waiting for a coherent roundup",
            "candidate": {
                "policy": POLICY_VERSION,
                "feed_url": feed_config.url,
                "source_url": link,
                "source_name": feed_config.source_name,
                "source_published_at": context["source_published_at"],
                "title": title,
                "content": plain_text(content),
                "image_urls": [i["url"] for i in source_images],
                "assessment": assessment.model_dump(),
            },
            **audit,
        }

    def hold(reason):
        if budget.get("drafts", 0) >= settings.max_drafts_per_run:
            return {"skipped": True, "reason": "draft_budget_exhausted", **audit}
        issues = list(dict.fromkeys([reason, *assessment_issues(assessment)]))
        draft = save_editorial_draft(
            writer=rewriter,
            wp=wp_client,
            dry_run=dry_run,
            sources=[
                {
                    "source_id": 1,
                    "source_url": link,
                    "source_name": feed_config.source_name,
                    "source_published_at": context["source_published_at"],
                    "title": title,
                    "content": plain_text(content),
                    "assessment": assessment.model_dump(),
                    "image_urls": [i["url"] for i in source_images],
                }
            ],
            images=[{**i, "source_id": 1} for i in source_images],
            assessment=assessment.model_dump(),
            issues=issues,
        )
        return {**draft, "reason": reason, "assessment": assessment.model_dump(), **audit}

    try:
        other_fields = [entry.get("summary", ""), entry.get("description", "")]
        other_fields.extend(item.get("value", "") for item in entry.get("content", []))
        try:
            require_usable_source(title, content, *other_fields)
        except ContentRejectedError as exc:
            if str(exc) != "empty_source" or not find_rss_images(entry):
                raise
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
        image_urls = find_rss_images(entry, base_url=link)
        # Count each evaluated source once, including short and image-only notices.
        budget["candidates"] += 1
        for url in image_urls[:3]:
            downloaded = download_image(url, min_width=200, min_height=200)
            if not downloaded:
                # Never cache a CDN/read failure as proof the source lacks value.
                raise RuntimeError("Source graphic unavailable for assessment; retry later")
            source_images.append({"url": url, "bytes": downloaded[0]})
        context = {
            "source_name": feed_config.source_name,
            "source_url": link,
            "source_published_at": pendulum.instance(published)
            .in_timezone(settings.timezone)
            .isoformat(),
            "current_time": pendulum.now(settings.timezone).isoformat(),
            "categories": wp_client.categories,
            "related_posts": related,
            "source_image_overflow": len(image_urls) > 3,
            "minimum_source_words": settings.min_source_words,
            "source_local_category_ids": additional_local_categories(
                settings.wordpress_base_url, title, content
            ),
        }
        assessment = rewriter.assess_source(title, content, context, source_images)
        audit["assessment"] = assessment.model_dump()
        if len(image_urls) > 3:
            # Do not assert no news value or completeness when not all evidence was read.
            if assessment.route == "reject":
                assessment = assessment.model_copy(
                    update={
                        "route": "draft",
                        "reason": "Additional source graphics need human inspection",
                        "headline": assessment.headline
                        or plain_text(title)[:110]
                        or "Source graphics need review",
                        "summary": assessment.summary
                        or "More than three source graphics were attached. Review the original post before deciding whether this information warrants coverage.",
                        "category_slugs": assessment.category_slugs
                        or [
                            next(
                                (
                                    c["slug"]
                                    for c in wp_client.categories
                                    if c["slug"] == "local-news"
                                ),
                                wp_client.categories[0]["slug"],
                            )
                        ],
                        "tags": [],
                    }
                )
                audit["assessment"] = assessment.model_dump()
            return hold("Additional source graphics exceed the automatic review limit")
        if assessment.route == "reject":
            raise ContentRejectedError("no_real_news_value: " + assessment.reason)
        if assessment.route == "draft" or assessment_issues(assessment):
            return hold(assessment.reason)
        if assessment.route == "roundup":
            if assessment.requires_immediate_attention:
                return hold("Source was not cleared to wait for a roundup")
            return queue_brief()
        context["source_assessment"] = assessment.model_dump()
        image_credit = None
        chosen_source = next((i for i in source_images if source_featured_size(i["bytes"])), None)
        if chosen_source:
            image_bytes, image_url = chosen_source["bytes"], chosen_source["url"]
            context["image_provenance"] = {"kind": "source", "url": image_url}
        else:
            if not settings.pexels_api_key:
                return hold(
                    "A clear, relevant original featured image or reviewed stock illustration is needed"
                )
            evidence_text = (
                plain_text(content)
                + " "
                + " ".join(f for r in assessment.image_readings for f in r.facts)
            )
            try:
                plan = rewriter.plan_stock_image(title, evidence_text, context)
            except ContentRejectedError as exc:
                return hold("Featured image required: " + str(exc))
            photos = PexelsClient(settings.pexels_api_key).search(plan.query)
            candidates = []
            for photo in photos:
                downloaded = download_image(photo["url"], allowed_hosts={"images.pexels.com"})
                if downloaded:
                    candidates.append({"photo": photo, "bytes": downloaded[0]})
            selected = rewriter.select_stock_image(title, evidence_text, plan, candidates)
            if not selected:
                return hold("No suitable Pexels illustration; supply an appropriate featured image")
            image_credit, image_bytes = selected["photo"], selected["bytes"]
            image_url = image_credit["url"]
            context["image_provenance"] = {"kind": "pexels_stock", **image_credit}
        try:
            article = rewriter.rewrite(
                content,
                title,
                feed_config.use_original_title,
                context=context,
                image_bytes=image_bytes,
                source_images=source_images,
                min_source_words=settings.min_source_words,
                min_article_words=settings.min_article_words,
                min_quality_score=settings.min_quality_score,
            )
        except DraftRequiredError as exc:
            evidence_words = len(
                (
                    plain_text(content)
                    + " "
                    + " ".join(
                        fact for reading in assessment.image_readings for fact in reading.facts
                    )
                ).split()
            )
            if (
                isinstance(exc, BriefTooShortError)
                and not assessment.requires_immediate_attention
                and not assessment_issues(assessment)
                and evidence_words < settings.min_article_words
            ):
                assessment.reason = (
                    "Useful complete brief could not meet the standalone article length minimum"
                )
                return queue_brief()
            return hold(str(exc))
        if dry_run:
            return {
                "preview": True,
                "intended_status": settings.wordpress_post_status,
                "article": article,
                "image_url": image_url,
                "image_credit": image_credit,
                "content": render_content(
                    article, link, feed_config.source_name, related, image_credit
                ),
                **audit,
            }
        caption = escape(article["review"]["image_caption"])
        caption += (
            " " + stock_credit(image_credit)
            if image_credit
            else (
                f' Source: <a href="{escape(link, quote=True)}">{escape(feed_config.source_name)}</a>.'
            )
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
            image_credit=image_credit,
        )
        return {**{k: post[k] for k in ("id", "link", "status", "duplicate") if k in post}, **audit}
    except ContentRejectedError as exc:
        logger.info("content_rejected", title=title[:80], reason=str(exc))
        return {"skipped": True, "reason": str(exc), "cache_rejection": True, **audit}


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
