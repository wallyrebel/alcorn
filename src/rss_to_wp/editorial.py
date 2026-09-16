"""Typed editorial decisions and deterministic publication checks."""

from __future__ import annotations

import hashlib
import re
from difflib import SequenceMatcher
from html import escape
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from bs4 import BeautifulSoup
from pydantic import BaseModel, ConfigDict, Field

from rss_to_wp.content_policy import ContentRejectedError, plain_text, require_clean_article

POLICY_VERSION = "quality-v1"
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
    return hashlib.sha256((POLICY_VERSION + plain_text(title + " " + content)).encode()).hexdigest()


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


def render_content(article: dict, source_url: str, source_name: str, related: list[dict]) -> str:
    body = article["body"]
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
