"""Typed editorial decisions and deterministic publication checks."""

from __future__ import annotations

import hashlib
import re
from difflib import SequenceMatcher
from html import escape
from typing import Literal
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from bs4 import BeautifulSoup
from pydantic import BaseModel, ConfigDict, Field

from rss_to_wp.content_policy import ContentRejectedError, plain_text, require_clean_article
from rss_to_wp.images.pexels import stock_credit

POLICY_VERSION = "quality-v5-clean-drafts"
ALLOWED_CATEGORIES = {
    "alcorn-county-news",
    "corinth-news",
    "local-news",
    "mississippi-news",
    "crime",
    "police-departments",
    "public-service",
    "sports",
    "weather",
}


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)


class ImageReading(StrictModel):
    image_id: int = Field(ge=1, le=3)
    facts: list[str] = Field(max_length=15)
    uncertainties: list[str] = Field(max_length=10)


class SourceAssessment(StrictModel):
    route: Literal["continue", "roundup", "draft", "reject"]
    requires_immediate_attention: bool
    reason: str = Field(min_length=5, max_length=800)
    headline: str = Field(max_length=110)
    summary: str = Field(max_length=2400)
    category_slugs: list[str] = Field(max_length=3)
    tags: list[str] = Field(max_length=5)
    image_readings: list[ImageReading] = Field(max_length=3)
    uncertainties: list[str] = Field(max_length=10)
    omitted_details: list[str] = Field(
        max_length=10,
        description="Nonessential unknowns that can safely be omitted. Not publication blockers.",
    )


class DraftSection(StrictModel):
    source_id: int = Field(ge=1, le=12)
    heading: str = Field(max_length=110)
    paragraphs: list[str] = Field(min_length=1, max_length=6)


class DraftCopy(StrictModel):
    headline: str = Field(min_length=10, max_length=110)
    sections: list[DraftSection] = Field(min_length=1, max_length=4)


class DraftReview(StrictModel):
    faithful: bool
    reader_facing: bool
    issues: list[str] = Field(max_length=10)
    optional_suggestions: list[str] = Field(max_length=10)
    image_id: int = Field(ge=0, le=12)
    image_alt: str = Field(max_length=250)
    image_caption: str = Field(max_length=500)
    image_reason: str = Field(max_length=800)


def validate_draft_copy(copy: DraftCopy, source_ids: list[int]):
    """Drafts may be brief, but never contain source packets or editorial instructions."""
    if sorted(s.source_id for s in copy.sections) != sorted(source_ids):
        raise RuntimeError("Draft copy must cover each source exactly once")
    values = [copy.headline]
    for section in copy.sections:
        values.extend([section.heading, *section.paragraphs])
    for value in values:
        validate_reader_text(value)
    if any(not p.strip() for s in copy.sections for p in s.paragraphs):
        raise RuntimeError("Empty draft paragraph")


def validate_reader_text(value: str):
    if value != plain_text(value) or len(value) > 2500:
        raise RuntimeError("Draft copy must be bounded plain text")
    if value.endswith((",", ":", ";", "...", "…")):
        raise RuntimeError("Draft copy or image metadata is incomplete")
    if re.search(
        r"editorial review|working copy|not approved for publication|"
        r"verify before publish|source publication time|original source graphics|"
        r"source image \d|supplied (?:RSS|text|image)|\[Review\]|"
        r"needs? (?:human|editorial) review|\bRSS\b",
        value,
        re.I,
    ):
        raise RuntimeError("Internal review language cannot appear in draft copy")


def draft_presentation_issues(copy: DraftCopy) -> list[str]:
    """Catch source-packet prose that is factual but not useful article copy."""
    pattern = re.compile(
        r"\b(?:graphic|screenshot|chart)\s+(?:titled|show\w*|display\w*|was titled)|"
        r"\battached tables?\b|\bperiod of record(?: shown|:|\s+\d{4}-)|\bproduct\(s\)|"
        r"\b(?:supportive work environment|competitive pay)\b|"
        r"(?:\d{2,3}(?:°[FC])?\s*/){2,}|\(Source:",
        re.I,
    )
    return [p for s in copy.sections for p in s.paragraphs if pattern.search(p)]


class DraftRequiredError(ContentRejectedError):
    """A useful source needs a human decision, never automatic publication."""


class BriefTooShortError(DraftRequiredError):
    """A concrete length failure, distinct from factual or editorial uncertainty."""


def validate_assessment(assessment: SourceAssessment, categories: list[dict], image_count: int):
    ids = [r.image_id for r in assessment.image_readings]
    if sorted(ids) != list(range(1, image_count + 1)):
        raise RuntimeError("Source image assessment incomplete; no editorial decision saved")
    if assessment.route == "reject":
        return
    if not assessment.headline or not assessment.summary:
        raise RuntimeError("Useful source assessment needs a headline and working summary")
    allowed = {c["slug"] for c in categories} & ALLOWED_CATEGORIES
    if not assessment.category_slugs or not set(assessment.category_slugs) <= allowed:
        raise RuntimeError("Invalid source assessment categories")
    if len(set(assessment.category_slugs)) != len(assessment.category_slugs):
        raise RuntimeError("Duplicate source assessment categories")
    if len({t.casefold() for t in assessment.tags}) != len(assessment.tags):
        raise RuntimeError("Duplicate source assessment tags")
    for tag in assessment.tags:
        if (
            not 3 <= len(tag) <= 60
            or tag != plain_text(tag)
            or tag.casefold() not in assessment.summary.casefold()
        ):
            raise RuntimeError("Unsupported source assessment tag")


def assessment_issues(assessment: SourceAssessment) -> list[str]:
    return list(
        dict.fromkeys(
            assessment.uncertainties
            + [issue for reading in assessment.image_readings for issue in reading.uncertainties]
        )
    )


class ArticleMetadata(StrictModel):
    publish: bool
    reason: str
    headline: str = Field(max_length=110)
    excerpt: str = Field(max_length=250, description="One or two short factual preview sentences.")
    slug: str = Field(max_length=90)
    seo_title: str = Field(max_length=70)
    meta_description: str = Field(
        max_length=165,
        description="A factual search summary; aim 135-150 characters, never exceed 165.",
    )
    category_slugs: list[str] = Field(max_length=3)
    tags: list[str] = Field(max_length=5)
    related_post_ids: list[int] = Field(max_length=2)


class Proposal(ArticleMetadata):
    paragraphs: list[str] = Field(
        description="Three or more distinct factual paragraphs as plain text. No HTML, Markdown, headline or byline. Empty if publish=false."
    )


class Article(ArticleMetadata):
    body: str


class Review(StrictModel):
    source_usable: bool
    faithful: bool
    newsworthy: bool
    locally_relevant: bool
    sufficiently_reported: bool
    metadata_accurate: bool
    not_duplicate: bool
    image_relevant: bool
    image_alt: str
    image_caption: str
    quality_score: int
    issues: list[str]


class RoundupPlan(StrictModel):
    source_ids: list[int] = Field(max_length=4)
    headline: str = Field(max_length=110)
    reason: str = Field(max_length=800)


class RoundupSection(StrictModel):
    source_id: int
    heading: str = Field(min_length=8, max_length=110)
    paragraphs: list[str] = Field(min_length=1, max_length=3)


class RoundupProposal(StrictModel):
    publish: bool
    reason: str
    headline: str = Field(max_length=110)
    slug: str = Field(max_length=90)
    category_slugs: list[str] = Field(max_length=3)
    tags: list[str] = Field(max_length=5)
    related_post_ids: list[int] = Field(max_length=2)
    sections: list[RoundupSection] = Field(max_length=4)


class RoundupSEO(StrictModel):
    # Leave generation room to finish a sentence; validate_article enforces the
    # real length limits before independent review and again at the write boundary.
    excerpt_options: list[str] = Field(min_length=1, max_length=3)
    seo_title_options: list[str] = Field(min_length=1, max_length=3)
    meta_description_options: list[str] = Field(min_length=1, max_length=3)


class BriefReview(StrictModel):
    source_id: int
    faithful: bool
    current: bool
    valuable: bool
    properly_attributed: bool
    not_duplicate: bool


class RoundupReview(Review):
    coherent: bool
    briefs: list[BriefReview] = Field(min_length=3, max_length=4)


def require_approved_roundup(review: RoundupReview, source_ids: list[int], minimum: int):
    require_approved_review(review, minimum)
    if (
        not review.coherent
        or sorted(b.source_id for b in review.briefs) != sorted(source_ids)
        or any(
            not all((b.faithful, b.current, b.valuable, b.properly_attributed, b.not_duplicate))
            for b in review.briefs
        )
    ):
        raise DraftRequiredError("Roundup coherence or individual brief verification failed")


def roundup_body(sections: list[dict], sources: list[dict], *, credits: bool = True) -> str:
    """Render exactly one attributed section per selected original source."""
    parsed = [RoundupSection.model_validate(s) for s in sections]
    source_map = {s["source_id"]: s for s in sources}
    urls = [canonical_url(s["source_url"]) for s in sources]
    if (
        not 3 <= len(sources) <= 4
        or len(set(urls)) != len(sources)
        or len(source_map) != len(sources)
        or sorted(s.source_id for s in parsed) != sorted(source_map)
    ):
        raise DraftRequiredError("Roundup must cover three or four distinct sources exactly once")
    body = ""
    headings = set()
    for section in parsed:
        if section.heading in headings or any(
            p != plain_text(p) or not p.strip() for p in [section.heading, *section.paragraphs]
        ):
            raise DraftRequiredError("Invalid or repeated roundup section")
        headings.add(section.heading)
        body += f"<h2>{escape(section.heading)}</h2>"
        body += "".join(f"<p>{escape(p)}</p>" for p in section.paragraphs)
        if credits:
            source = source_map[section.source_id]
            body += (
                f'<p><em>Source: <a href="{escape(canonical_url(source["source_url"]), quote=True)}" '
                f'rel="noopener">{escape(source["source_name"])}</a>.</em></p>'
            )
    return body


class StockPlan(StrictModel):
    eligible: bool
    query: str
    reason: str


class StockSelection(StrictModel):
    photo_id: int
    relevant: bool
    safe_illustration: bool
    quality_score: int
    issues: list[str]


def canonical_url(url: str) -> str:
    parts = urlsplit(url.strip())
    if parts.scheme not in {"http", "https"} or not parts.hostname or parts.username:
        raise ContentRejectedError("invalid_source_url")
    host = parts.hostname.lower().removeprefix("www.")
    if host in {"m.facebook.com", "web.facebook.com"}:
        host = "facebook.com"
    query = [
        (k, v)
        for k, v in parse_qsl(parts.query, keep_blank_values=True)
        if not k.lower().startswith("utm_")
        and k.lower() not in {"fbclid", "gclid", "ref", "refsrc", "mibextid", "__tn__", "__cft__"}
    ]
    return urlunsplit(("https", host, parts.path.rstrip("/"), urlencode(sorted(query)), ""))


def fingerprint(title: str, content: str) -> str:
    # Signed CDN query strings rotate; image paths identify changed source graphics.
    images = [
        urlsplit(img.get("src", "")).path
        for img in BeautifulSoup(content, "html.parser").find_all("img")
    ]
    return hashlib.sha256(
        (POLICY_VERSION + plain_text(title + " " + content) + "|".join(images)).encode()
    ).hexdigest()


def similar_story(left: str, right: str) -> bool:
    def normalized(value):
        return " ".join(re.findall(r"\w+", plain_text(value).casefold()))

    left, right = normalized(left), normalized(right)
    # Generic Facebook titles must not collapse unrelated posts.
    if min(len(left.split()), len(right.split())) < 7:
        return False
    return SequenceMatcher(None, left, right, autojunk=False).ratio() >= 0.88


def validate_article(
    article: dict, categories: list[dict], related: list[dict], min_words: int
) -> None:
    parsed = Article.model_validate(article)
    if not parsed.publish:
        raise ContentRejectedError("editor_declined: " + parsed.reason)
    require_clean_article(article)
    for key in ("headline", "excerpt", "seo_title", "meta_description"):
        if article[key] != plain_text(article[key]):
            raise ContentRejectedError("markup_in_metadata")
    if not 25 <= len(parsed.headline) <= 110 or not 25 <= len(parsed.seo_title) <= 70:
        raise ContentRejectedError("headline_length")
    if not 110 <= len(parsed.meta_description) <= 165 or not 60 <= len(parsed.excerpt) <= 300:
        raise ContentRejectedError("description_length")
    if not re.fullmatch(r"[a-z0-9]+(?:-[a-z0-9]+)*", parsed.slug) or len(parsed.slug) > 90:
        raise ContentRejectedError("invalid_slug")
    text = plain_text(parsed.body)
    if not min_words <= len(text.split()) <= 900:
        raise ContentRejectedError("insufficient_or_excessive_article_length")
    if re.search(
        r"\b(?:as an ai|in conclusion|stay tuned|only time will tell|by Jon R Myers)\b", text, re.I
    ):
        raise ContentRejectedError("boilerplate_or_embedded_byline")
    paragraphs = [
        plain_text(str(p)) for p in BeautifulSoup(parsed.body, "html.parser").find_all("p")
    ]
    if len(paragraphs) < 3 or len(set(paragraphs)) != len(paragraphs):
        raise ContentRejectedError("thin_or_repetitive_article")
    allowed = {c["slug"] for c in categories} & ALLOWED_CATEGORIES
    if not 1 <= len(parsed.category_slugs) <= 3 or not set(parsed.category_slugs) <= allowed:
        raise ContentRejectedError("invalid_categories")
    if not 2 <= len(parsed.tags) <= 5 or len({t.casefold() for t in parsed.tags}) != len(
        parsed.tags
    ):
        raise ContentRejectedError("invalid_tags")
    for tag in parsed.tags:
        if (
            not 3 <= len(tag) <= 60
            or tag != plain_text(tag)
            or tag.casefold() not in text.casefold()
        ):
            raise ContentRejectedError("tag_not_supported_in_article")
    if len(parsed.related_post_ids) > 2 or not set(parsed.related_post_ids) <= {
        p["id"] for p in related
    }:
        raise ContentRejectedError("unverified_internal_link")


def require_approved_review(review: Review, minimum: int) -> None:
    checks = (
        review.source_usable,
        review.faithful,
        review.newsworthy,
        review.locally_relevant,
        review.sufficiently_reported,
        review.metadata_accurate,
        review.not_duplicate,
        review.image_relevant,
    )
    if not all(checks) or review.issues or not minimum <= review.quality_score <= 100:
        raise ContentRejectedError("editorial_review_failed: " + "; ".join(review.issues))
    if not 15 <= len(review.image_alt) <= 180 or not 10 <= len(review.image_caption) <= 300:
        raise ContentRejectedError("invalid_image_description")


def render_content(
    article: dict,
    source_url: str,
    source_name: str,
    related: list[dict],
    image_credit: dict | None = None,
    *,
    roundup_sources: list[dict] | None = None,
    roundup_sections: list[dict] | None = None,
) -> str:
    body = (
        roundup_body(roundup_sections or [], roundup_sources)
        if roundup_sources
        else article["body"]
    )
    if image_credit:
        # Themes do not always render featured-image captions; disclosure must be visible.
        body = f"<p><em>{stock_credit(image_credit)}</em></p>" + body
    if not roundup_sources:
        body += f'<p><em>Source: <a href="{escape(source_url, quote=True)}" rel="noopener">{escape(source_name)}</a>.</em></p>'
    chosen = {p["id"]: p for p in related}
    links = []
    for post_id in dict.fromkeys(article["related_post_ids"]):
        post = chosen[post_id]
        links.append(
            f'<li><a href="{escape(post["link"], quote=True)}">{escape(post["title"])}</a></li>'
        )
    if links:
        body += "<h2>Related coverage</h2><ul>" + "".join(links) + "</ul>"
    return body
