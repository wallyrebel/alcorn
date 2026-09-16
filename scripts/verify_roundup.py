"""Live model/Pexels check using FICTIONAL briefs. Never accesses WordPress.

Exercises source assessment, grouping, structured writing and independent visual/factual
review. Reports failures rather than retrying/regenerating a better-looking outcome.
"""

import json
from pathlib import Path

import pendulum

from rss_to_wp.config import get_app_settings
from rss_to_wp.content_policy import ContentRejectedError
from rss_to_wp.editorial import assessment_issues, render_content
from rss_to_wp.rewriter.openai_client import OpenAIRewriter
from rss_to_wp.roundups import _featured_image

settings = get_app_settings()
writer = OpenAIRewriter(
    settings.openai_api_key, settings.openai_model, review_model=settings.openai_review_model
)
now = pendulum.now(settings.timezone)
date = now.add(days=3).format("MMMM D, YYYY")
categories = [
    {"id": 221, "slug": "corinth-news", "name": "Corinth News"},
    {"id": 224, "slug": "public-service", "name": "Public Service"},
]
fixtures = [
    (
        "Library raises adult borrowing limit",
        f"""Corinth Library announced that beginning {date},
adult cardholders may borrow 12 books at a time instead of eight. The new limit applies to the
general lending collection. Children's cards retain the eight-book limit. Reference books remain
available for use inside the library only. Existing adult cardholders do not need to replace
their cards. The library will provide printed borrowing rules at its circulation desk.""",
    ),
    (
        "Library adds online renewal option",
        f"""Corinth Library announced that beginning {date},
Alcorn County cardholders can renew eligible books through their online library accounts.
Each book may be renewed once for three weeks if no other reader has placed a hold on it.
Books with pending holds must be returned on their original due date. Renewal at the
circulation desk will remain available. The library will provide printed renewal instructions at the desk.""",
    ),
    (
        "Library extends pickup time for book holds",
        f"""Corinth Library announced that beginning {date},
reserved books will be held for pickup for seven days after notification, up from five days.
Cardholders can collect their reserved books at the circulation desk during regular opening hours.
Unclaimed books will return to the lending collection or move to the next reader waiting.
The library said the new pickup period applies to all cardholders, including children's cards.""",
    ),
]
report = {"fictional_fixture": True, "wordpress_writes": 0, "sources": []}
path = Path("data/roundup-smoke-report.json")
path.parent.mkdir(exist_ok=True)
try:
    for i, (title, text) in enumerate(fixtures, 1):
        context = {
            "source_name": "Corinth Library",
            "source_url": f"https://example.test/fictional-brief/{i}",
            "source_published_at": now.isoformat(),
            "current_time": now.isoformat(),
            "categories": categories,
            "related_posts": [],
            "source_image_overflow": False,
        }
        assessment = writer.assess_source(title, text, context, [])
        report["sources"].append(
            {
                "source_id": i,
                **context,
                "title": title,
                "content": text,
                "image_urls": [],
                "assessment": assessment.model_dump(),
            }
        )
        print(json.dumps({"source": i, "route": assessment.route, "reason": assessment.reason}))
        assert assessment.route == "roundup" and not assessment_issues(assessment), (
            "Brief was not safely eligible for pooling"
        )
    sources = report["sources"]
    plan = writer.plan_roundup(sources, now.isoformat())
    report["plan"] = plan.model_dump()
    assert set(plan.source_ids) == {1, 2, 3}, "Model did not select the three compatible briefs"
    context = {"categories": categories, "related_posts": [], "current_time": now.isoformat()}
    image, url, credit, _ = _featured_image(plan, sources, [], writer, settings, context)
    report["image_credit"] = credit
    article, sections = writer.rewrite_roundup(
        sources, plan, context=context, image_bytes=image, source_images=[]
    )
    report.update(
        article=article,
        sections=sections,
        rendered_content=render_content(
            article,
            sources[0]["source_url"],
            "Corinth Library",
            [],
            credit,
            roundup_sources=sources,
            roundup_sections=sections,
        ),
        passed=True,
    )
    print(
        json.dumps(
            {
                "passed": True,
                "headline": article["headline"],
                "quality_score": article["review"]["quality_score"],
                "sections": len(sections),
                "wordpress_writes": 0,
            }
        )
    )
except ContentRejectedError as exc:
    # A rejected proposal is a healthy review-draft outcome in the real pipeline.
    # In particular, a skeptical reviewer may correctly decline these fictional URLs.
    report.update(completed=True, passed=False, intended_status="draft", reason=str(exc))
    print(
        json.dumps(
            {
                "completed": True,
                "publication_checks_passed": False,
                "intended_status": "draft",
                "reason": str(exc),
                "wordpress_writes": 0,
            }
        )
    )
except Exception as exc:
    report.update(passed=False, error=str(exc))
    raise
finally:
    path.write_text(json.dumps(report, indent=2), encoding="utf-8")
