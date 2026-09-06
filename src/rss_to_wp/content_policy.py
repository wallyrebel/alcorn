"""Fail-closed checks for inaccessible RSS content and generated error articles."""

from __future__ import annotations

import re
import unicodedata
from html import unescape

from bs4 import BeautifulSoup


class ContentRejectedError(ValueError):
    """An entry must be skipped, rather than published or expanded into filler."""


def plain_text(value: str) -> str:
    soup = BeautifulSoup(unescape(value or ""), "html.parser")
    for element in soup(["script", "style"]):
        element.decompose()
    return re.sub(r"\s+", " ", soup.get_text(" ")).strip()


_ACCESS_ERRORS = re.compile(
    r"\b(?:content|post|page|video|attachment)\s+(?:is\s+|isn['’]?t\s+|is\s+not\s+)?"
    r"(?:currently\s+|temporarily\s+|no\s+longer\s+)?"
    r"(?:unavailable|not\s+available|not\s+accessible|inaccessible|restricted|removed|deleted)\b"
    r"|\b(?:content|post|page|video)\s+isn['’]?t\s+(?:available|accessible)\b"
    r"|\b(?:content|post|page|video)\s+(?:is\s+)?no\s+longer\s+(?:available|accessible)\b"
    r"|\b(?:content|post)\s+(?:may\s+be|is)\s+limited\s+to\s+a\s+small\s+audience\b"
    r"|\baccess\s+to\s+(?:this\s+|the\s+)?content\s+(?:is\s+)?(?:restricted|denied)\b"
    r"|\b(?:content|post|page)\s+(?:has\s+been|was)\s+(?:removed|deleted|restricted)\b"
    r"|\b(?:changed|changes?\s+to)\s+(?:its?\s+|the\s+|their\s+)?privacy\s+settings\b"
    r"|\bshared\s+(?:it|this|the\s+(?:post|content))\s+with\s+(?:only\s+)?a\s+"
    r"(?:small|limited|select)\s+(?:group|audience)\b"
    r"|\b(?:log\s*in|sign\s*in)\s+(?:is\s+required|to\s+(?:continue|view|see|access))\b"
    r"|\b(?:access\s+denied|permission\s+denied|restricted\s+visibility|you\s+must\s+log\s*in|"
    r"verify\s+(?:that\s+)?you\s+are\s+(?:a\s+)?human|checking\s+your\s+browser)\b"
    r"|\b(?:404\s*(?:error|not\s+found)|403\s*forbidden|"
    r"subscribe\s+to\s+(?:continue\s+)?(?:read(?:ing)?|view(?:ing)?))\b",
    re.IGNORECASE,
)


def access_error_reason(*values: str) -> str | None:
    # Check fields separately: a usable title must never hide a failed body.
    for value in values:
        text = unicodedata.normalize("NFKC", plain_text(value))
        invisible = r"[\u200b-\u200f\ufeff]"
        if any(
            _ACCESS_ERRORS.search(re.sub(invisible, replacement, text)) for replacement in ("", " ")
        ):
            return "unavailable_or_restricted_content"
    return None


def require_usable_source(title: str, content: str, *other_fields: str) -> None:
    reason = access_error_reason(title, content, *other_fields)
    if reason:
        raise ContentRejectedError(reason)
    # No word or character minimum: a short factual announcement is valid.
    text = plain_text(content) or plain_text(title)
    if not text or text.lower() in {"untitled", "photos from", "photo", "video"}:
        raise ContentRejectedError("empty_source")


def require_clean_article(article: dict) -> None:
    if not isinstance(article, dict) or any(
        not isinstance(article.get(key), str) for key in ("headline", "excerpt", "body")
    ):
        raise ContentRejectedError("invalid_article_fields")
    if not plain_text(article["headline"]) or not plain_text(article["body"]):
        raise ContentRejectedError("empty_article")
    reason = access_error_reason(article["headline"], article["excerpt"], article["body"])
    if reason:
        raise ContentRejectedError(reason)
    soup = BeautifulSoup(article["body"], "html.parser")
    if not soup.find("p") or any(
        tag.name not in {"p", "strong", "em", "br"} or tag.attrs for tag in soup.find_all()
    ):
        raise ContentRejectedError("invalid_article_html")
