"""Explicitly re-evaluate one current, owned hold through the full publishing pipeline.

Read-only by default. --publish permits replacing that unchanged draft only after all
normal checks pass. Public sources alone go to models; the original WP draft is backed
up locally in --report. Routine scheduled runs never invoke this script.
"""

from __future__ import annotations

import argparse
import json
from html import escape
from pathlib import Path

from rss_to_wp.cli import process_entry
from rss_to_wp.config import get_app_settings, load_feeds_config
from rss_to_wp.content_policy import plain_text
from rss_to_wp.editorial import canonical_url
from rss_to_wp.feeds import parse_feed
from rss_to_wp.feeds.filter import is_within_window, parse_entry_date
from rss_to_wp.images.pexels import stock_credit
from rss_to_wp.rewriter import OpenAIRewriter
from rss_to_wp.roundups import entry_fingerprint
from rss_to_wp.utils import get_logger
from rss_to_wp.wordpress import WordPressClient


class CapturingWriter(OpenAIRewriter):
    def _request(self, model, prompt, content, schema, token_limit):
        result = super()._request(model, prompt, content, schema, token_limit)
        self.record_stage(schema.__name__, result.model_dump())
        return result

    def rewrite(self, *args, **kwargs):
        result = super().rewrite(*args, **kwargs)
        self.approved_image = kwargs["image_bytes"]
        self.approved_context = kwargs["context"]
        return result


class ReadOnlyReview:
    def __init__(self, wp, snapshot, source_url):
        self.wp, self.snapshot, self.source_url = wp, snapshot, source_url
        self.categories = wp.categories

    def check_duplicate_by_source_url(self, source_url):
        if canonical_url(source_url) != self.source_url:
            return self.wp.check_duplicate_by_source_url(source_url)
        self.wp.verify_review_draft(self.snapshot, source_url)
        return False

    def related_candidates(self, *args, **kwargs):
        return self.wp.related_candidates(*args, **kwargs)


def current_entry(config, source_url):
    feed = parse_feed(config.url)
    if (
        feed is None
        or not config.primary_source
        or plain_text(feed.feed.get("title", "")).removesuffix(" on Facebook") != config.source_name
        or canonical_url(feed.feed.get("link", "")) != canonical_url(config.source_url)
    ):
        raise RuntimeError("Official feed identity unavailable or changed")
    matches = [e for e in feed.entries if canonical_url(e.get("link", "")) == source_url]
    if len(matches) != 1:
        raise RuntimeError("Source is not uniquely available in the current feed")
    published = parse_entry_date(matches[0])
    if not published or not is_within_window(published, 48):
        raise RuntimeError("Source is outside the normal 48-hour publication window")
    return matches[0]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--post-id", type=int, required=True)
    parser.add_argument("--source-url", required=True)
    parser.add_argument("--feed-name", required=True)
    parser.add_argument("--report", type=Path, required=True)
    parser.add_argument("--publish", action="store_true")
    args = parser.parse_args()
    if args.report.exists():
        raise RuntimeError("Use a new report path to preserve the original backup")
    settings = get_app_settings()
    settings.wordpress_post_status = "publish"
    source_url = canonical_url(args.source_url)
    configs = [c for c in load_feeds_config("feeds.yaml").feeds if c.name == args.feed_name]
    if len(configs) != 1:
        raise RuntimeError("Source must match one configured official feed")
    config = configs[0]
    wp = WordPressClient(
        settings.wordpress_base_url,
        settings.wordpress_username,
        settings.wordpress_app_password,
        "publish",
        settings.wordpress_author_id,
        settings.wordpress_author_name,
        settings.min_quality_score,
        settings.min_article_words,
    )
    wp.preflight()
    snapshot = wp._request("GET", f"posts/{args.post_id}", params={"context": "edit"})
    wp.verify_review_draft(snapshot, source_url)
    entry = current_entry(config, source_url)
    report = {"site": wp.base_url, "source_url": source_url, "original": snapshot}

    def save():
        args.report.parent.mkdir(parents=True, exist_ok=True)
        args.report.write_text(json.dumps(report, indent=2, ensure_ascii=False), encoding="utf-8")

    save()
    writer = CapturingWriter(
        settings.openai_api_key, settings.openai_model, review_model=settings.openai_review_model
    )

    def record_stage(name, result):
        report.setdefault("model_results", []).append({"stage": name, "result": result})
        save()

    writer.record_stage = record_stage
    try:
        result = process_entry(
            entry,
            config,
            settings,
            writer,
            ReadOnlyReview(wp, snapshot, source_url),
            True,
            get_logger("review-held-draft"),
        )
        report["review"] = result
        save()
        if result.get("intended_status") != "publish" or not result.get("article"):
            print(
                json.dumps(
                    {
                        "id": args.post_id,
                        "published": False,
                        "reason": result.get("reason", "Full publication checks did not pass"),
                    }
                )
            )
            return
        if args.publish:
            fresh = current_entry(config, source_url)
            if entry_fingerprint(fresh, config.source_name) != entry_fingerprint(
                entry, config.source_name
            ):
                raise RuntimeError("Source changed during review; publication stopped")
            wp.verify_review_draft(snapshot, source_url)
            article, credit = result["article"], result["image_credit"]
            caption = escape(article["review"]["image_caption"])
            caption += (
                " " + stock_credit(credit)
                if credit
                else f' Source: <a href="{escape(source_url, quote=True)}">{escape(config.source_name)}</a>.'
            )
            media_id = wp.upload_media(
                writer.approved_image,
                article["slug"] + ".jpg",
                article["review"]["image_alt"],
                caption=caption,
            )
            report["publication"] = wp.create_post(
                article=article,
                source_url=source_url,
                source_name=config.source_name,
                related=writer.approved_context["related_posts"],
                featured_media_id=media_id,
                image_credit=credit,
                replace_review_draft=snapshot,
            )
            save()
        print(
            json.dumps(
                {
                    "id": args.post_id,
                    "passed": True,
                    "publication": {
                        k: report.get("publication", {}).get(k) for k in ("status", "link")
                    },
                }
            )
        )
    except Exception as exc:
        report["error"] = str(exc)
        save()
        raise


if __name__ == "__main__":
    main()
