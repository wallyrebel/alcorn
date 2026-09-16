"""Integration smoke test: create one nonpublic test draft, verify, then trash it.

Never publishes, uploads media, changes settings, or edits an existing post.
Run explicitly against the configured site: python scripts/verify_wordpress_draft.py
"""

import json
from pathlib import Path

from rss_to_wp.config import get_app_settings
from rss_to_wp.wordpress.client import WordPressClient

settings = get_app_settings()
wp = WordPressClient(
    settings.wordpress_base_url, settings.wordpress_username, settings.wordpress_app_password
)
wp.preflight()
payload = {
    "title": "Publishing pipeline verification draft",
    "content": "<p>Temporary nonpublic integration verification. This is not a news article.</p><!-- rss-to-wp-test -->",
    "excerpt": "Temporary nonpublic integration verification.",
    "status": "draft",
    "author": settings.wordpress_author_id,
    "categories": [221],
}
post_id = None
result = {}
try:
    post = wp._request("POST", "posts", json=payload)
    post_id = post["id"]
    assert post["status"] == "draft"
    wp._seo_request(
        "PUT",
        post_id,
        "title-description-metas",
        json={"title": payload["title"], "description": payload["excerpt"]},
    )
    wp._seo_request(
        "PUT",
        post_id,
        "social-settings",
        json={
            "_seopress_social_fb_title": payload["title"],
            "_seopress_social_fb_desc": payload["excerpt"],
        },
    )
    read = wp._request("GET", f"posts/{post_id}", params={"context": "edit"})
    seo = read.get("meta", {})
    result = {
        "post_id": post_id,
        "status": read["status"],
        "author": read["author"],
        "categories": read["categories"],
        "content_round_trip": wp._raw(read, "content") == payload["content"],
        "seo_title_round_trip": seo.get("_seopress_titles_title") == payload["title"],
        "seo_description_round_trip": seo.get("_seopress_titles_desc") == payload["excerpt"],
        "social_round_trip": seo.get("_seopress_social_fb_title") == payload["title"],
    }
    assert all(
        result[k]
        for k in ("content_round_trip", "seo_title_round_trip", "seo_description_round_trip")
    )
finally:
    if post_id:
        # Trash only the exact draft created by this invocation, never existing content.
        trashed = wp._request("DELETE", f"posts/{post_id}")
        result["cleanup_status"] = trashed.get("status")
    Path("data/wordpress-smoke-report.json").write_text(
        json.dumps(result, indent=2), encoding="utf-8"
    )
    print(json.dumps(result, indent=2))
