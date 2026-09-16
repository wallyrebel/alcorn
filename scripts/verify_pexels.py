"""Live Pexels search and visual selection on fictional library-service fixture.

Reads Pexels and calls the configured affordable reviewer. No WordPress access/writes.
Does not download original files to disk; actual downloaded pixels are reviewed in memory.
"""

import json
from datetime import datetime, timezone
from pathlib import Path

from rss_to_wp.config import get_app_settings
from rss_to_wp.images import download_image
from rss_to_wp.images.pexels import PexelsClient, stock_credit
from rss_to_wp.rewriter.openai_client import OpenAIRewriter

settings = get_app_settings()
if not settings.pexels_api_key:
    raise SystemExit("No PEXELS_API_KEY configured")
writer = OpenAIRewriter(
    settings.openai_api_key, settings.openai_model, review_model=settings.openai_review_model
)
title = "Corinth Library expands book borrowing limits for county residents"
source = """Corinth Library announced today that Alcorn County residents can now borrow up to
12 books at a time, an increase from the previous limit of eight. The change takes effect
immediately for adult library cards and applies to the general lending collection. Library
staff said the loan period remains three weeks and readers can renew a book once when no
other reader has placed a hold. Residents can renew at the circulation desk or through their
online account. Books on hold for another reader must be returned by their original due date.
The library said children's cards will continue to have an eight-book limit and that reference
books cannot be borrowed. Residents applying for a new card should bring proof of their Alcorn
County address to the circulation desk during regular opening hours. Existing cardholders do
not need to apply again to receive the higher borrowing limit. The library will provide printed
copies of the revised borrowing rules at the circulation desk beginning today."""
now = datetime.now(timezone.utc).isoformat()
context = {"source_name": "Corinth Library", "source_published_at": now, "current_time": now}
plan = writer.plan_stock_image(title, source, context)
photos = PexelsClient(settings.pexels_api_key).search(plan.query)
candidates = []
for photo in photos:
    result = download_image(photo["url"], allowed_hosts={"images.pexels.com"})
    if result:
        candidates.append({"photo": photo, "bytes": result[0]})
selected = writer.select_stock_image(title, source, plan, candidates)
report = {
    "fictional_fixture": True,
    "wordpress_writes": 0,
    "plan": plan.model_dump(),
    "search_candidates": len(photos),
    "usable_downloads": len(candidates),
    "selected": selected["photo"] if selected else None,
    "credit": stock_credit(selected["photo"]) if selected else None,
}
Path("data").mkdir(exist_ok=True)
Path("data/pexels-smoke-report.json").write_text(json.dumps(report, indent=2), encoding="utf-8")
print(json.dumps(report, indent=2))
