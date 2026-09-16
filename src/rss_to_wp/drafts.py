"""Clean nonpublic article drafts; all editorial diagnostics stay in the run report."""

from __future__ import annotations

import hashlib
from html import escape

from rss_to_wp.editorial import (
    DraftCopy,
    DraftReview,
    draft_presentation_issues,
    validate_draft_copy,
    validate_reader_text,
)
from rss_to_wp.images.downloader import featured_size


def save_editorial_draft(
    *,
    writer,
    wp,
    sources,
    images,
    assessment,
    issues,
    dry_run,
    prepared=None,
    replace_existing=None,
):
    prepared = prepared or writer.prepare_editorial_draft(sources, images)
    copy = DraftCopy.model_validate(prepared["copy"])
    review = DraftReview.model_validate(prepared["review"])
    validate_draft_copy(copy, [s["source_id"] for s in sources])
    if draft_presentation_issues(copy):
        raise RuntimeError("Source-packet prose cannot be saved as article copy")
    if not review.faithful or not review.reader_facing or review.issues:
        raise RuntimeError("Unapproved draft copy cannot be saved")
    if review.image_id > len(images):
        raise RuntimeError("Invalid draft image selection")
    chosen = images[review.image_id - 1] if review.image_id else None
    if chosen:
        if not review.image_alt.strip() or not review.image_caption.strip():
            raise RuntimeError("Draft featured image requires alt text and caption")
        validate_reader_text(review.image_alt)
        validate_reader_text(review.image_caption)
    notes = list(dict.fromkeys(issues))
    if chosen and not featured_size(chosen["bytes"]):
        notes.append(
            "Original image is below the preferred large-preview size; publication still requires visual and image-type checks"
        )
    if not chosen:
        notes.append("Featured image needed: " + review.image_reason)
    audit = {
        "draft_copy": copy.model_dump(),
        "draft_review": review.model_dump(),
        "review_issues": notes,
        "image_url": chosen["url"] if chosen else None,
    }
    if dry_run:
        return {"preview": True, "intended_status": "draft", **audit}
    # A selected image must really be imported, not replaced by a body link.
    media_id = 0
    if chosen:
        source = next(s for s in sources if s["source_id"] == chosen["source_id"])
        marker = hashlib.sha256(source["source_url"].encode()).hexdigest()[:12]
        caption = escape(review.image_caption) + (
            f' Source: <a href="{escape(source["source_url"], quote=True)}">'
            f"{escape(source['source_name'])}</a>."
        )
        media_id = wp.upload_media(
            chosen["bytes"], f"source-{marker}.jpg", review.image_alt, caption=caption
        )
        if not media_id:
            raise RuntimeError("Draft featured image upload failed; source remains retryable")
    first = sources[0]
    post = wp.create_editorial_draft(
        assessment=assessment,
        source_url=first["source_url"],
        source_name=first["source_name"],
        issues=notes,
        source_images=first.get("image_urls", []) if len(sources) == 1 else [],
        source_published_at=first["source_published_at"],
        roundup_sources=sources if len(sources) > 1 else None,
        prepared_copy=copy.model_dump(),
        featured_media_id=media_id,
        image_alt=review.image_alt if chosen else "",
        replace_existing=replace_existing,
    )
    return {**post, **audit}
