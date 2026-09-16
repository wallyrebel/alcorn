"""Bounded, freshness-checked roundups of short but independently useful briefs."""

from __future__ import annotations

import hashlib
import json
from html import escape
from urllib.parse import urlsplit

import pendulum
from pydantic import Field

from rss_to_wp.content_policy import ContentRejectedError, plain_text, require_usable_source
from rss_to_wp.drafts import save_editorial_draft
from rss_to_wp.editorial import (
    POLICY_VERSION,
    DraftRequiredError,
    SourceAssessment,
    StrictModel,
    assessment_issues,
    canonical_url,
    fingerprint,
    render_content,
    validate_assessment,
)
from rss_to_wp.feeds import get_entry_content, get_entry_title, parse_feed
from rss_to_wp.feeds.filter import is_within_window, parse_entry_date
from rss_to_wp.images import download_image
from rss_to_wp.images.downloader import source_featured_size
from rss_to_wp.images.pexels import PexelsClient, stock_credit
from rss_to_wp.images.rss_extractor import find_rss_images


class RoundupCandidate(StrictModel):
    policy: str
    entry_key: str
    fingerprint: str
    feed_url: str
    source_url: str
    source_name: str
    source_published_at: str
    title: str
    content: str = Field(max_length=18000)
    image_urls: list[str] = Field(max_length=3)
    assessment: SourceAssessment


def entry_fingerprint(entry, source_name):
    identity = "|".join(
        (urlsplit(url).hostname or "") + urlsplit(url).path
        for url in find_rss_images(entry, base_url=entry.get("link", ""))
    )
    return fingerprint(
        get_entry_title(entry),
        get_entry_content(entry) + source_name + str(entry.get("published", "")) + identity,
    )


def load_pool(store, feeds, hours, *, dry_run):
    allowed = {f.url: f for f in feeds}
    pool = {}
    for raw in store.load_roundup_candidates():
        source_url = raw["source_url"]
        try:
            candidate = RoundupCandidate.model_validate(raw)
            published = parse_entry_date({"published": candidate.source_published_at})
            usable = (
                candidate.policy == POLICY_VERSION
                and published is not None
                and is_within_window(published, min(hours, 48))
                and candidate.assessment.route == "roundup"
                and not candidate.assessment.requires_immediate_attention
                and not assessment_issues(candidate.assessment)
            )
        except (ValueError, KeyError):
            usable = False
        if not usable:
            if not dry_run:
                store.remove_roundup(source_url)
            continue
        feed = allowed.get(candidate.feed_url)
        if feed and feed.primary_source and feed.source_name == candidate.source_name:
            pool[source_url] = candidate.model_dump()
    return pool


def _refresh_sources(selected, feeds, hours):
    """Re-read original feeds before using persisted assessments or signed image URLs."""
    configs = {f.url: f for f in feeds}
    fetched = {}
    refreshed = []
    for item in selected:
        config = configs[item["feed_url"]]
        if config.url not in fetched:
            feed = parse_feed(config.url)
            if (
                not config.primary_source
                or feed is None
                or (
                    plain_text(feed.feed.get("title", "")).removesuffix(" on Facebook")
                    != config.source_name
                    or canonical_url(feed.feed.get("link", "")) != canonical_url(config.source_url)
                )
            ):
                raise RuntimeError("Roundup source feed unavailable or identity changed")
            fetched[config.url] = feed
        entry = next(
            (
                e
                for e in fetched[config.url].entries
                if canonical_url(e.get("link", "")) == item["source_url"]
            ),
            None,
        )
        published = parse_entry_date(entry) if entry else None
        if (
            not entry
            or not published
            or not is_within_window(published, min(hours, 48))
            or entry_fingerprint(entry, config.source_name) != item["fingerprint"]
        ):
            return None, item["source_url"]
        try:
            require_usable_source(
                get_entry_title(entry),
                get_entry_content(entry),
                entry.get("summary", ""),
                entry.get("description", ""),
                *[c.get("value", "") for c in entry.get("content", [])],
            )
        except ContentRejectedError as exc:
            if str(exc) != "empty_source" or not find_rss_images(
                entry, base_url=item["source_url"]
            ):
                return None, item["source_url"]
        item = {**item, "image_urls": find_rss_images(entry, base_url=item["source_url"])}
        validate_assessment(
            SourceAssessment.model_validate(item["assessment"]),
            # Categories were checked on admission and are checked again below.
            [{"slug": s} for s in item["assessment"]["category_slugs"]],
            len(item["image_urls"]),
        )
        refreshed.append(item)
    return refreshed, None


def _featured_image(plan, sources, images, writer, settings, context):
    chosen = next((i for i in images if source_featured_size(i["bytes"])), None)
    if chosen:
        source = next(s for s in sources if s["source_id"] == chosen["source_id"])
        context["image_provenance"] = {
            "kind": "source",
            "url": chosen["url"],
            "source_id": source["source_id"],
            "source_name": source["source_name"],
        }
        return chosen["bytes"], chosen["url"], None, source
    if not settings.pexels_api_key:
        raise DraftRequiredError(
            "Roundup needs a clear, relevant original image or reviewed stock illustration"
        )
    # Include every topic in the stock safety check, not just the roundup headline.
    evidence = "\n".join(
        s["title"] + " " + s["content"] + " " + s["assessment"]["summary"] for s in sources
    )
    try:
        stock_plan = writer.plan_stock_image(plan.headline, evidence, context)
        photos = PexelsClient(settings.pexels_api_key).search(stock_plan.query)
        candidates = []
        for photo in photos:
            downloaded = download_image(photo["url"], allowed_hosts={"images.pexels.com"})
            if downloaded:
                candidates.append({"photo": photo, "bytes": downloaded[0]})
        selected = writer.select_stock_image(plan.headline, evidence, stock_plan, candidates)
    except ContentRejectedError as exc:
        raise DraftRequiredError("Roundup featured image: " + str(exc)) from exc
    if not selected:
        raise DraftRequiredError("No suitable Pexels illustration for all roundup topics")
    credit = selected["photo"]
    context["image_provenance"] = {"kind": "pexels_stock", **credit}
    return selected["bytes"], credit["url"], credit, None


def process_roundups(pool, feeds, settings, store, writer, wp, dry_run, hours, budget, decisions):
    """At most one grouping/writing attempt per run, sharing the public/draft caps."""
    if budget["posts"] >= settings.max_posts_per_run or not pool:
        return 0, 0, 0
    decision = {"kind": "roundup", "source_urls": []}
    try:
        eligible = []
        items = list(pool.items())
        if len(items) > 12:
            offset = (int(pendulum.now("UTC").timestamp() // 3600) * 12) % len(items)
            items = items[offset:] + items[:offset]
        for url, candidate in items[:12]:
            if store.source_seen(url) or wp.check_duplicate_by_source_url(url):
                if not dry_run:
                    store.remove_roundup(url)
                pool.pop(url)
                continue
            assessment = SourceAssessment.model_validate(candidate["assessment"])
            if (
                assessment.route != "roundup"
                or assessment.requires_immediate_attention
                or assessment_issues(assessment)
            ):
                raise RuntimeError("Source was not cleared for the roundup queue")
            validate_assessment(
                assessment,
                wp.categories,
                len(candidate["image_urls"]),
            )
            eligible.append(candidate)
            if len(eligible) == 12:
                break
        if len(eligible) < 3:
            decision.update(reason="waiting_for_three_compatible_briefs", queued=len(eligible))
            return 0, 0, 0
        attempt = (
            "roundup:"
            + hashlib.sha256(
                json.dumps(sorted(s["fingerprint"] for s in eligible)).encode()
            ).hexdigest()
        )
        if store.rejection_reason(attempt):
            decision.update(reason="unchanged_roundup_pool_cooling_down")
            return 0, 0, 0
        candidates = [{**s, "source_id": i} for i, s in enumerate(eligible, 1)]
        plan = writer.plan_roundup(
            [
                {k: s[k] for k in ("source_id", "source_name", "source_published_at", "assessment")}
                for s in candidates
            ],
            pendulum.now(settings.timezone).isoformat(),
        )
        if not plan.source_ids:
            decision.update(reason="no_coherent_roundup: " + plan.reason)
            if not dry_run:
                store.record_rejection(attempt, decision["reason"])
            return 0, 0, 0
        # Also validate when a custom writer implementation is used.
        if not 3 <= len(set(plan.source_ids)) == len(plan.source_ids) <= 4 or not set(
            plan.source_ids
        ) <= {s["source_id"] for s in candidates}:
            raise RuntimeError("Invalid roundup source selection")
        selected = [next(s for s in candidates if s["source_id"] == i) for i in plan.source_ids]
        decision["source_urls"] = [s["source_url"] for s in selected]
        sources, changed = _refresh_sources(selected, feeds, hours)
        if changed:
            pool.pop(changed)
            if not dry_run:
                store.remove_roundup(changed)
            decision.update(
                reason="source_changed_or_expired_before_roundup", changed_source=changed
            )
            return 0, 0, 0
        images, related = [], {}
        for source in sources:
            for image_id, url in enumerate(source["image_urls"], 1):
                downloaded = download_image(url, min_width=200, min_height=200)
                if not downloaded:
                    raise RuntimeError("Roundup source image unavailable; retry later")
                images.append(
                    {
                        "source_id": source["source_id"],
                        "image_id": image_id,
                        "url": url,
                        "bytes": downloaded[0],
                    }
                )
            for post in wp.related_candidates(
                source["title"], source["content"], source_name=source["source_name"]
            ):
                related[post["id"]] = post
        context = {
            "categories": wp.categories,
            "related_posts": list(related.values())[:16],
            "current_time": pendulum.now(settings.timezone).isoformat(),
        }
        outcome = _create_roundup(
            sources, plan, images, context, settings, writer, wp, dry_run, budget
        )
        decision.update(outcome)
        if outcome.get("duplicate") or outcome.get("skipped"):
            return 0, 1, 0
        status = outcome.get("intended_status") if dry_run else outcome["status"]
        budget["drafts" if status == "draft" else "posts"] = (
            budget.get("drafts" if status == "draft" else "posts", 0) + 1
        )
        # Each original is consumed only after the ONE resulting post is confirmed.
        for source in sources:
            if not dry_run:
                store.mark_processed(
                    source["entry_key"],
                    source["feed_url"],
                    source["title"],
                    source["source_url"],
                    outcome["id"],
                    outcome["link"],
                )
                store.remove_roundup(source["source_url"])
            pool.pop(source["source_url"], None)
        return 1, 0, 0
    except Exception as exc:
        decision["error"] = str(exc)
        return 0, 0, 1
    finally:
        decisions.append(decision)


def _create_roundup(sources, plan, images, context, settings, writer, wp, dry_run, budget):
    try:
        image_bytes, image_url, credit, image_source = _featured_image(
            plan, sources, images, writer, settings, context
        )
        article, sections = writer.rewrite_roundup(
            sources,
            plan,
            context=context,
            image_bytes=image_bytes,
            source_images=images,
            min_article_words=settings.min_article_words,
            min_quality_score=settings.min_quality_score,
        )
    except ContentRejectedError as exc:
        if budget.get("drafts", 0) >= settings.max_drafts_per_run:
            return {"skipped": True, "reason": "draft_budget_exhausted"}
        categories = list(
            dict.fromkeys(c for s in sources for c in s["assessment"]["category_slugs"])
        )[:3]
        assessment = SourceAssessment(
            route="draft",
            requires_immediate_attention=False,
            reason=str(exc)[:800],
            headline=plan.headline,
            summary="These short briefs need editorial review before they can form a publishable roundup.",
            category_slugs=categories,
            tags=[],
            image_readings=[],
            uncertainties=[],
            omitted_details=[],
        )
        return {
            **save_editorial_draft(
                writer=writer,
                wp=wp,
                dry_run=dry_run,
                sources=sources,
                images=images,
                assessment=assessment.model_dump(),
                issues=[str(exc)],
            ),
            "reason": str(exc),
        }
    if (
        settings.wordpress_post_status == "draft"
        and budget.get("drafts", 0) >= settings.max_drafts_per_run
    ):
        return {"skipped": True, "reason": "draft_budget_exhausted"}
    first = sources[0]
    if dry_run:
        return {
            "preview": True,
            "intended_status": settings.wordpress_post_status,
            "article": article,
            "sections": sections,
            "sources": sources,
            "content": render_content(
                article,
                first["source_url"],
                first["source_name"],
                context["related_posts"],
                credit,
                roundup_sources=sources,
                roundup_sections=sections,
            ),
        }
    caption = escape(article["review"]["image_caption"])
    caption += (
        " " + stock_credit(credit)
        if credit
        else f' Source: <a href="{escape(image_source["source_url"], quote=True)}">{escape(image_source["source_name"])}</a>.'
    )
    media_id = wp.upload_media(
        image_bytes, article["slug"] + ".jpg", article["review"]["image_alt"], caption=caption
    )
    if not media_id:
        raise RuntimeError("Roundup featured image upload failed; publication blocked")
    post = wp.create_post(
        article=article,
        source_url=first["source_url"],
        source_name=first["source_name"],
        related=context["related_posts"],
        featured_media_id=media_id,
        image_credit=credit,
        roundup_sources=sources,
        roundup_sections=sections,
    )
    return {k: post[k] for k in ("id", "link", "status", "duplicate") if k in post}
