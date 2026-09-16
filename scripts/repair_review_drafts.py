"""Prepare explicit repairs of owned review drafts; --apply keeps every post nonpublic.

No automatic discovery or scheduled repair. The saved plan is also the original-content
backup and private editorial report. Human edits after preparation stop the repair.
"""

from __future__ import annotations

import argparse
import hashlib
import json
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from rss_to_wp.config import get_app_settings, load_feeds_config
from rss_to_wp.content_policy import plain_text
from rss_to_wp.drafts import save_editorial_draft
from rss_to_wp.editorial import assessment_issues, canonical_url
from rss_to_wp.feeds import get_entry_content, get_entry_title, parse_feed
from rss_to_wp.feeds.filter import parse_entry_date
from rss_to_wp.images import download_image
from rss_to_wp.images.rss_extractor import find_rss_images
from rss_to_wp.rewriter import OpenAIRewriter
from rss_to_wp.wordpress import WordPressClient


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--post-id", type=int, action="append", default=[])
    parser.add_argument("--plan", type=Path, required=True)
    parser.add_argument("--apply", action="store_true")
    parser.add_argument(
        "--trash-rejected",
        action="store_true",
        help="With --apply, move only explicitly rejected owned drafts to recoverable Trash",
    )
    args = parser.parse_args()
    settings = get_app_settings()

    def wordpress():
        return WordPressClient(
            settings.wordpress_base_url,
            settings.wordpress_username,
            settings.wordpress_app_password,
            "draft",
            settings.wordpress_author_id,
            settings.wordpress_author_name,
        )

    wp = wordpress()
    wp.preflight()
    if args.apply:
        plan = json.loads(args.plan.read_text(encoding="utf-8"))
        if plan["site"] != wp.base_url:
            raise RuntimeError("Repair plan belongs to a different site")
        for item in plan["items"]:
            rejected = item.get("source", {}).get("assessment", {}).get("route") == "reject"
            trash = args.trash_rejected and rejected
            if item.get("applied") or (item.get("error") and not trash):
                continue
            previous = item["original"]
            current = wp._request("GET", f"posts/{previous['id']}", params={"context": "edit"})
            marker = hashlib.sha256(item["source"]["source_url"].encode()).hexdigest()[:12]
            if (
                current["status"] != "draft"
                or current["author"] != wp.author_id
                or current["modified_gmt"] != previous["modified_gmt"]
                or wp._raw(current, "content") != wp._raw(previous, "content")
                or f"<!-- rss-to-wp:editorial-hold:v1:{marker} -->"
                not in wp._raw(current, "content")
            ):
                raise RuntimeError(f"Draft {previous['id']} changed; repair stopped")
            if trash:
                result = wp._request("DELETE", f"posts/{previous['id']}", params={"force": False})
                if result.get("status") != "trash":
                    raise RuntimeError("Rejected draft was not moved to Trash")
                result = {"id": previous["id"], "status": "trash"}
            else:
                images = [{**i, "bytes": Path(i["path"]).read_bytes()} for i in item["images"]]
                result = save_editorial_draft(
                    writer=None,
                    wp=wp,
                    sources=[item["source"]],
                    images=images,
                    assessment=item["source"]["assessment"],
                    issues=item["review_issues"],
                    dry_run=False,
                    prepared=item["prepared"],
                    replace_existing=current,
                )
            item["applied"] = result
            args.plan.write_text(json.dumps(plan, indent=2), encoding="utf-8")
            print(
                json.dumps(
                    {
                        "id": previous["id"],
                        "status": result.get("status"),
                        "image": result.get("image_url") is not None,
                    }
                ),
                flush=True,
            )
        return
    if not 1 <= len(args.post_id) <= 12 or len(set(args.post_id)) != len(args.post_id):
        raise RuntimeError("Specify 1-12 distinct draft IDs for an explicit repair")
    entries = {}
    for config in load_feeds_config("feeds.yaml").feeds:
        feed = parse_feed(config.url)
        if feed is None:
            continue
        if (
            plain_text(feed.feed.get("title", "")).removesuffix(" on Facebook")
            != config.source_name
            or canonical_url(feed.feed.get("link", "")) != canonical_url(config.source_url)
            or not config.primary_source
        ):
            raise RuntimeError("Feed identity changed")
        for entry in feed.entries:
            entries[canonical_url(entry.get("link", ""))] = (config, entry)
    assets = args.plan.parent / (args.plan.stem + "-images")
    assets.mkdir(parents=True, exist_ok=True)

    def prepare(post_id):
        try:
            client = wordpress()
            original = client._request("GET", f"posts/{post_id}", params={"context": "edit"})
            raw = client._raw(original, "content")
            if original["status"] != "draft" or original["author"] != client.author_id:
                raise RuntimeError("Only owned nonpublic review drafts may be repaired")
            match = next(
                (
                    url
                    for url in entries
                    if f"<!-- rss-to-wp:editorial-hold:v1:{hashlib.sha256(url.encode()).hexdigest()[:12]} -->"
                    in raw
                ),
                None,
            )
            if not match:
                raise RuntimeError("No matching review marker/current source")
            config, entry = entries[match]
            urls = find_rss_images(entry, base_url=match)
            images, saved = [], []
            for index, url in enumerate(urls[:3], 1):
                downloaded = download_image(url, min_width=200, min_height=200)
                if not downloaded:
                    raise RuntimeError("Original image unavailable")
                path = assets / f"{post_id}-{index}.jpg"
                path.write_bytes(downloaded[0])
                saved.append({"source_id": 1, "url": url, "path": str(path.resolve())})
                images.append({"source_id": 1, "url": url, "bytes": downloaded[0]})
            writer = OpenAIRewriter(
                settings.openai_api_key,
                settings.openai_model,
                review_model=settings.openai_review_model,
            )
            context = {
                "categories": wp.categories,
                "related_posts": [],
                "source_name": config.source_name,
                "source_url": match,
                "source_published_at": parse_entry_date(entry).isoformat(),
                "source_image_overflow": len(urls) > 3,
            }
            from datetime import datetime, timezone

            context["current_time"] = datetime.now(timezone.utc).isoformat()
            assessment = writer.assess_source(
                get_entry_title(entry), get_entry_content(entry), context, images
            )
            source = {
                "source_id": 1,
                "source_url": match,
                "source_name": config.source_name,
                "source_published_at": context["source_published_at"],
                "title": get_entry_title(entry),
                "content": plain_text(get_entry_content(entry)),
                "assessment": assessment.model_dump(),
                "image_urls": urls[:3],
            }
            if assessment.route == "reject":
                return {
                    "id": post_id,
                    "original": original,
                    "source": source,
                    "error": "No longer merits a draft: " + assessment.reason,
                }
            prepared = writer.prepare_editorial_draft([source], images)
            print(
                json.dumps(
                    {
                        "id": post_id,
                        "route": assessment.route,
                        "headline": prepared["copy"]["headline"],
                        "image_id": prepared["review"]["image_id"],
                    }
                ),
                flush=True,
            )
            return {
                "original": original,
                "source": source,
                "images": saved,
                "prepared": prepared,
                "review_issues": [assessment.reason, *assessment_issues(assessment)],
            }
        except Exception as exc:
            print(json.dumps({"id": post_id, "error": str(exc)}), flush=True)
            return {"id": post_id, "error": str(exc)}

    with ThreadPoolExecutor(max_workers=2) as pool:
        items = list(pool.map(prepare, args.post_id))
    plan = {"site": wp.base_url, "items": items}
    args.plan.write_text(json.dumps(plan, indent=2), encoding="utf-8")
    print(
        json.dumps(
            {
                "prepared": sum("prepared" in i for i in items),
                "held": sum("error" in i for i in items),
                "plan": str(args.plan),
            }
        ),
        flush=True,
    )


if __name__ == "__main__":
    main()
