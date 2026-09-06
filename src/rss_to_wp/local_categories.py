"""Conservative city routing using original RSS text, never rewritten claims."""

import re
from urllib.parse import urlsplit

from rss_to_wp.content_policy import plain_text


def additional_local_categories(base_url: str, title: str, content: str) -> list[str]:
    """Keep the existing city archive current on Alcorn County News only.

    These are signals in this publication's curated local feeds, not a general
    place-name resolver. Ambiguous mentions stay in the feed's default category.
    """
    if urlsplit(base_url).hostname not in {"alcornnewsms.com", "www.alcornnewsms.com"}:
        return []
    text = plain_text(f"{title} {content}")
    if not re.search(r"\bCorinth\b", text, re.I):
        return []
    # Prefer a missed category over confusing another Corinth with Mississippi.
    if re.search(
        r"\bCorinth\s*,?\s+(?:Texas|TX|New York|NY|Kentucky|KY|Vermont|VT|"
        r"Maine|ME|Greece|Tennessee|TN|Arkansas|AR|North Carolina|NC)\b",
        text,
        re.I,
    ):
        return []
    explicit_city = re.search(r"\bCorinth\s*,?\s+(?:Mississippi|Miss\.?|MS)\b", text, re.I)
    local_institution = re.search(
        r"\bCorinth\s+(?:(?:Police|Fire)\s+Department|City\s+Park|"
        r"School\s+District|High\s+School|Elks\s+Lodge)\b",
        text,
        re.I,
    )
    regional_context = re.search(r"\b(?:Alcorn|Mississippi)\b", text, re.I)
    city_activity = re.search(r"\b(?:in|downtown|city of)\s+Corinth\b", text, re.I)
    if explicit_city or local_institution or (regional_context and city_activity):
        return ["Corinth MS News"]
    return []
