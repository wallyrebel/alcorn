"""WordPress publishing that stages and verifies every requirement before publication."""

from __future__ import annotations

import hashlib
from html import escape
from urllib.parse import urlsplit

import requests
from bs4 import BeautifulSoup

from rss_to_wp.content_policy import ContentRejectedError, plain_text
from rss_to_wp.editorial import (
    ALLOWED_CATEGORIES,
    DraftCopy,
    Review,
    RoundupReview,
    SourceAssessment,
    assessment_issues,
    canonical_url,
    render_content,
    require_approved_review,
    require_approved_roundup,
    roundup_body,
    similar_story,
    validate_article,
    validate_assessment,
    validate_draft_copy,
)
from rss_to_wp.images.downloader import publication_dimensions
from rss_to_wp.wordpress.media import wp_upload_media


class WordPressClient:
    def __init__(
        self,
        base_url,
        username,
        password,
        default_status="publish",
        author_id=1,
        author_name="Jon R Myers",
        min_quality_score=90,
        min_article_words=150,
    ):
        self.base_url = base_url.rstrip("/")
        self.username, self.password = username, password
        self.default_status = default_status
        self.author_id, self.author_name = author_id, author_name
        self.min_quality_score, self.min_article_words = min_quality_score, min_article_words
        self.session = requests.Session()
        self.session.auth = (username, password)
        self.session.headers.update({"Accept": "application/json"})
        self.categories = []
        self.recent_posts = []
        self._tag_cache = {}
        self._related_cache = {}

    def _api_url(self, endpoint):
        return f"{self.base_url}/wp-json/wp/v2/{endpoint}"

    def _request(self, method, endpoint, **kwargs):
        # No automatic retries of writes: an ambiguous timeout may have committed.
        response = self.session.request(method, self._api_url(endpoint), timeout=(10, 45), **kwargs)
        response.raise_for_status()
        return response.json()

    def preflight(self):
        user = self._request("GET", f"users/{self.author_id}", params={"context": "edit"})
        if user["name"] != self.author_name:
            raise RuntimeError("Configured author does not match the exact WordPress byline")
        self.categories = self._request("GET", "categories", params={"per_page": 100})
        self.categories = [
            {k: c[k] for k in ("id", "name", "slug")}
            for c in self.categories
            if c["slug"] in ALLOWED_CATEGORIES
        ]
        if not self.categories:
            raise RuntimeError("No approved WordPress categories available")
        self.recent_posts = self._request(
            "GET",
            "posts",
            params={
                "per_page": 100,
                "status": "publish",
                "orderby": "date",
                "order": "desc",
                "_fields": "id,title,content,excerpt,link,date,slug,status,author",
            },
        )
        # SEOPress is mandatory for this site's publishing path.
        response = self.session.get(f"{self.base_url}/wp-json/", timeout=(10, 30))
        response.raise_for_status()
        if "seopress/v1" not in response.json().get("namespaces", []):
            raise RuntimeError("SEOPress API unavailable; publication blocked")

    @staticmethod
    def _raw(post, field):
        value = post.get(field, {})
        return value.get("raw", value.get("rendered", "")) if isinstance(value, dict) else value

    def find_source_posts(self, source_url):
        source_url = canonical_url(source_url)
        parts = urlsplit(source_url)
        # Search the stable host/path substring so legacy www/http/trailing-slash
        # variants are found; compare fully canonicalized hrefs below.
        search = parts.netloc + parts.path
        posts = self._request(
            "GET",
            "posts",
            params={
                "search": search,
                "status": "publish,draft,pending,future,private,trash",
                "context": "edit",
                "per_page": 100,
            },
        )
        if len(posts) == 100:
            raise RuntimeError("Source duplicate search is incomplete; publication blocked")
        matches = []
        for post in posts:
            soup = BeautifulSoup(self._raw(post, "content"), "html.parser")
            for a in soup.find_all("a", href=True):
                try:
                    if canonical_url(a["href"]) == source_url:
                        matches.append(post)
                        break
                except ContentRejectedError:
                    continue
        return matches

    def check_duplicate_by_source_url(self, source_url):
        posts = self.find_source_posts(source_url)
        # Only this tool's own staging drafts may be resumed.
        return any(not self._is_staging_draft(p, source_url) for p in posts)

    def _is_staging_draft(self, post, source_url):
        marker = hashlib.sha256(canonical_url(source_url).encode()).hexdigest()[:12]
        return (
            post["status"] == "draft"
            and post.get("author") == self.author_id
            and f"<!-- rss-to-wp:quality-v1:{marker} -->" in self._raw(post, "content")
        )

    def verify_review_draft(self, snapshot, source_url):
        """Explicit re-review only; never promote a changed, public or human draft."""
        marker = hashlib.sha256(canonical_url(source_url).encode()).hexdigest()[:12]
        current = self._request("GET", f"posts/{snapshot['id']}", params={"context": "edit"})
        if (
            snapshot.get("status") != "draft"
            or current.get("status") != "draft"
            or current.get("author") != self.author_id
            or f"<!-- rss-to-wp:editorial-hold:v1:{marker} -->" not in self._raw(current, "content")
            or not snapshot.get("modified_gmt")
            or current.get("modified_gmt") != snapshot["modified_gmt"]
            or any(
                self._raw(current, key) != self._raw(snapshot, key)
                for key in ("content", "title", "excerpt")
            )
            or any(
                current.get(key) != snapshot.get(key)
                for key in ("author", "slug", "featured_media", "categories", "tags")
            )
        ):
            raise RuntimeError("Review draft changed or is not an owned nonpublic hold; stopped")
        matches = self.find_source_posts(source_url)
        if len(matches) != 1 or matches[0]["id"] != snapshot["id"]:
            raise RuntimeError("Source has conflicting coverage; review draft promotion stopped")
        return current

    def check_duplicate_by_slug(self, slug):
        return bool(
            self._request(
                "GET",
                "posts",
                params={
                    "slug": slug,
                    "status": "publish,draft,pending,future,private",
                    "context": "edit",
                },
            )
        )

    def related_candidates(self, title, content, source_name=""):
        # Search the archive as well as recent stories; entity searches are cached
        # for the run and still require editorial review before a link is included.
        if source_name and source_name not in self._related_cache:
            self._related_cache[source_name] = self._request(
                "GET",
                "posts",
                params={
                    "search": source_name,
                    "status": "publish",
                    "per_page": 10,
                    "_fields": "id,title,content,excerpt,link,date,slug,status,author",
                },
            )
        candidates = {
            p["id"]: p for p in self.recent_posts + self._related_cache.get(source_name, [])
        }
        words = set(plain_text(title + " " + content).lower().split()) - {
            "the",
            "and",
            "for",
            "that",
            "with",
            "from",
            "this",
            "will",
            "have",
            "are",
            "news",
            "mississippi",
            "county",
            "said",
            "was",
            "has",
            "their",
            "been",
            "they",
        }
        ranked = []
        for post in candidates.values():
            if urlsplit(post["link"]).hostname != urlsplit(self.base_url).hostname:
                continue
            post_title = plain_text(self._raw(post, "title"))
            excerpt = plain_text(self._raw(post, "content"))[:1600]
            score = len(words & set((post_title + " " + excerpt).lower().split()))
            if score >= 3:
                ranked.append(
                    (
                        score,
                        {
                            "id": post["id"],
                            "title": post_title,
                            "link": post["link"],
                            "date": post["date"],
                            "excerpt": excerpt,
                        },
                    )
                )
        return [p for _, p in sorted(ranked, key=lambda pair: pair[0], reverse=True)[:8]]

    def get_or_create_tags(self, names):
        ids = []
        for name in names:
            if name.casefold() not in self._tag_cache:
                terms = self._request("GET", "tags", params={"search": name, "per_page": 100})
                matches = [t for t in terms if plain_text(t["name"]).casefold() == name.casefold()]
                if matches:
                    term = matches[0]
                else:
                    try:
                        term = self._request("POST", "tags", json={"name": name})
                    except requests.HTTPError as exc:
                        error = exc.response.json()
                        if error.get("code") != "term_exists" or not error.get("data", {}).get(
                            "term_id"
                        ):
                            raise
                        term = {"id": int(error["data"]["term_id"])}
                self._tag_cache[name.casefold()] = term["id"]
            ids.append(self._tag_cache[name.casefold()])
        if len(ids) != len(set(ids)):
            raise RuntimeError("Tag resolution returned ambiguous terms")
        return ids

    def upload_media(self, image_bytes, filename, alt_text, caption=""):
        return wp_upload_media(
            image_bytes,
            filename,
            alt_text,
            self.base_url,
            self.username,
            self.password,
            self.session,
            caption=caption,
        )

    def create_editorial_draft(
        self,
        *,
        assessment,
        source_url,
        source_name,
        issues,
        source_images,
        source_published_at,
        roundup_sources=None,
        prepared_copy,
        featured_media_id=0,
        image_alt="",
        replace_existing=None,
    ):
        """Human review queue: this path can never set publish or resume an auto draft."""
        assessment = SourceAssessment.model_validate(assessment)
        validate_assessment(assessment, self.categories, len(source_images))
        if assessment.route == "reject":
            raise ContentRejectedError("Rejected sources cannot become drafts")
        source_url = canonical_url(source_url)
        author = self._request("GET", f"users/{self.author_id}")
        if author["name"] != self.author_name:
            raise RuntimeError("Exact draft byline verification failed")
        # Includes human drafts, existing holds and public posts. Never overwrite any.
        sources = roundup_sources or [
            {"source_id": 1, "source_url": source_url, "source_name": source_name}
        ]
        if roundup_sources and not 3 <= len(roundup_sources) <= 4:
            raise RuntimeError("Invalid roundup draft source count")
        target_id = replace_existing["id"] if replace_existing else None
        if any(
            p["id"] != target_id for s in sources for p in self.find_source_posts(s["source_url"])
        ):
            return {"duplicate": True, "reason": "source_already_in_wordpress"}
        marker = hashlib.sha256(source_url.encode()).hexdigest()[:12]
        slug = "editorial-review-" + marker
        if not target_id and self.check_duplicate_by_slug(slug):
            raise RuntimeError("Conflicting review draft slug")
        copy = DraftCopy.model_validate(prepared_copy)
        validate_draft_copy(copy, [s["source_id"] for s in sources])
        content = ""
        for section in copy.sections:
            source = next(s for s in sources if s["source_id"] == section.source_id)
            if len(sources) > 1:
                content += f"<h2>{escape(section.heading)}</h2>"
            content += "".join(f"<p>{escape(p)}</p>" for p in section.paragraphs)
            content += (
                f'<p>Source: <a href="{escape(canonical_url(source["source_url"]), quote=True)}">'
                f"{escape(source['source_name'])}</a>.</p>"
            )
        if featured_media_id:
            media = self._request("GET", f"media/{featured_media_id}")
            details = media.get("media_details", {})
            if (
                not image_alt
                or media.get("alt_text") != image_alt
                or min(details.get("width", 0), details.get("height", 0)) < 200
            ):
                raise RuntimeError("Draft featured image verification failed")
        # This marker intentionally differs from the auto-resumable quality-v1 marker.
        content += f"<!-- rss-to-wp:editorial-hold:v1:{marker} -->"
        payload = {
            "title": copy.headline,
            "content": content,
            "excerpt": "",
            "status": "draft",
            "author": self.author_id,
            "slug": slug,
            "categories": [
                next(c["id"] for c in self.categories if c["slug"] == s)
                for s in assessment.category_slugs
            ],
            "tags": self.get_or_create_tags(
                [t for t in assessment.tags if t.casefold() in plain_text(content).casefold()]
            )
            if assessment.tags
            else [],
            "featured_media": featured_media_id,
        }
        if target_id:
            # Explicit repair only: never touch a public, human, changed or unrelated draft.
            actual = self._request("GET", f"posts/{target_id}", params={"context": "edit"})
            if (
                actual["status"] != "draft"
                or actual.get("author") != self.author_id
                or f"<!-- rss-to-wp:editorial-hold:v1:{marker} -->"
                not in self._raw(actual, "content")
                or actual.get("modified_gmt") != replace_existing.get("modified_gmt")
                or self._raw(actual, "content") != self._raw(replace_existing, "content")
            ):
                raise RuntimeError("Draft changed or is not an owned review hold; repair stopped")
            payload["slug"] = actual["slug"]
        created = self._request(
            "POST", f"posts/{target_id}" if target_id else "posts", json=payload
        )
        verified = self._request("GET", f"posts/{created['id']}", params={"context": "edit"})
        for key in ("status", "author", "slug", "featured_media"):
            if verified.get(key) != payload[key]:
                raise RuntimeError("Editorial draft verification failed: " + key)
        for key in ("categories", "tags"):
            if set(verified.get(key, [])) != set(payload[key]):
                raise RuntimeError("Editorial draft taxonomy verification failed")
        for key in ("title", "content"):
            if self._raw(verified, key).strip() != payload[key].strip():
                raise RuntimeError("Editorial draft content verification failed")
        return {
            "id": verified["id"],
            "status": "draft",
            "link": f"{self.base_url}/wp-admin/post.php?post={verified['id']}&action=edit",
        }

    def _seo_request(self, method, post_id, suffix="", **kwargs):
        url = f"{self.base_url}/wp-json/seopress/v1/posts/{post_id}" + (
            f"/{suffix}" if suffix else ""
        )
        response = self.session.request(method, url, timeout=(10, 30), **kwargs)
        response.raise_for_status()
        data = response.json()
        if method == "PUT" and data.get("code") != "success":
            raise RuntimeError("SEOPress did not confirm metadata write")
        return data

    def create_post(
        self,
        *,
        article,
        source_url,
        source_name,
        related,
        featured_media_id,
        image_credit=None,
        roundup_sources=None,
        roundup_sections=None,
        replace_review_draft=None,
    ):
        if replace_review_draft:
            if roundup_sources:
                raise RuntimeError("Explicit review promotion supports individual stories only")
            self.verify_review_draft(replace_review_draft, source_url)
        core = {k: v for k, v in article.items() if k != "review"}
        validate_article(core, self.categories, related, self.min_article_words)
        if roundup_sources:
            review = RoundupReview.model_validate(article["review"])
            require_approved_roundup(
                review, [s["source_id"] for s in roundup_sources], self.min_quality_score
            )
            if core["body"] != roundup_body(roundup_sections or [], roundup_sources, credits=False):
                raise ContentRejectedError("Roundup sections do not match reviewed article")
            from rss_to_wp.feeds.filter import is_within_window, parse_entry_date

            for source in roundup_sources:
                assessment = SourceAssessment.model_validate(source["assessment"])
                validate_assessment(assessment, self.categories, len(source["image_urls"]))
                if (
                    assessment.route != "roundup"
                    or assessment.requires_immediate_attention
                    or assessment_issues(assessment)
                ):
                    raise ContentRejectedError(
                        "Roundup contains a source not cleared for aggregation"
                    )
                published = parse_entry_date({"published": source["source_published_at"]})
                if not published or not is_within_window(published, 48):
                    raise ContentRejectedError("Roundup source expired before publication")
        else:
            review = Review.model_validate(article["review"])
            require_approved_review(review, self.min_quality_score)
        if not featured_media_id:
            raise ContentRejectedError("featured_image_required")
        source_url = canonical_url(source_url)
        # Repeat author/media verification at the write boundary, never fall back.
        author = self._request("GET", f"users/{self.author_id}")
        if author["name"] != self.author_name:
            raise RuntimeError("Exact byline verification failed")
        media = self._request("GET", f"media/{featured_media_id}")
        details = media.get("media_details", {})
        if (
            not publication_dimensions(
                details.get("width", 0), details.get("height", 0), review.image_kind
            )
            or (review.image_kind == "pexels_stock") != bool(image_credit)
            or media.get("alt_text") != review.image_alt
        ):
            raise RuntimeError("Featured image verification failed")
        for post_id in article["related_post_ids"]:
            actual = self._request("GET", f"posts/{post_id}")
            candidate = next(p for p in related if p["id"] == post_id)
            if actual["status"] != "publish" or actual["link"] != candidate["link"]:
                raise RuntimeError("Internal link is no longer published")
        existing = self.find_source_posts(source_url)
        if roundup_sources:
            # Never overwrite a human hold or a partially staged roundup. All original
            # links are duplicate barriers, even after losing the local queue/cache.
            if existing or any(self.find_source_posts(s["source_url"]) for s in roundup_sources):
                return {"duplicate": True}
        if replace_review_draft:
            if len(existing) != 1 or existing[0]["id"] != replace_review_draft["id"]:
                raise RuntimeError("Source coverage changed during explicit review")
        elif any(not self._is_staging_draft(p, source_url) for p in existing):
            return {"duplicate": True}
        if len(existing) > 1:
            raise RuntimeError("Multiple staging drafts require manual review")
        if any(
            similar_story(article["headline"], self._raw(p, "title")) for p in self.recent_posts
        ):
            return {"duplicate": True}
        tag_ids = self.get_or_create_tags(article["tags"])
        category_ids = [
            next(c["id"] for c in self.categories if c["slug"] == slug)
            for slug in article["category_slugs"]
        ]
        identity = (
            "|".join(sorted(canonical_url(s["source_url"]) for s in roundup_sources))
            if roundup_sources
            else source_url
        )
        marker = hashlib.sha256(identity.encode()).hexdigest()[:12]
        content = render_content(
            article,
            source_url,
            source_name,
            related,
            image_credit,
            roundup_sources=roundup_sources,
            roundup_sections=roundup_sections,
        )
        marker_kind = "roundup-staging-v1" if roundup_sources else "quality-v1"
        content += f"<!-- rss-to-wp:{marker_kind}:{marker} -->"
        payload = {
            "title": article["headline"],
            "content": content,
            "excerpt": article["excerpt"],
            "slug": article["slug"] + "-" + marker,
            "status": "draft",
            "author": self.author_id,
            "categories": category_ids,
            "tags": tag_ids,
            "featured_media": featured_media_id,
            "meta": {
                "_seopress_robots_primary_cat": str(category_ids[0]),
                "_seopress_analysis_target_kw": ", ".join(article["tags"]),
            },
        }
        if existing:
            if replace_review_draft:
                self.verify_review_draft(replace_review_draft, source_url)
                if self.check_duplicate_by_slug(payload["slug"]):
                    raise RuntimeError("Conflicting publication slug; review draft remains held")
            post = self._request("POST", f"posts/{existing[0]['id']}", json=payload)
        else:
            if self.check_duplicate_by_slug(payload["slug"]):
                raise RuntimeError("Conflicting slug; publication blocked")
            post = self._request("POST", "posts", json=payload)
        post_id = post["id"]
        # All following failures leave a nonpublic draft. Never publish first and repair later.
        self._seo_request(
            "PUT",
            post_id,
            "title-description-metas",
            json={"title": article["seo_title"], "description": article["meta_description"]},
        )
        self._seo_request(
            "PUT",
            post_id,
            "social-settings",
            json={
                "_seopress_social_fb_title": article["seo_title"],
                "_seopress_social_fb_desc": article["meta_description"],
                "_seopress_social_fb_img": media["source_url"],
                "_seopress_social_fb_img_attachment_id": str(featured_media_id),
                "_seopress_social_fb_img_width": str(details["width"]),
                "_seopress_social_fb_img_height": str(details["height"]),
                "_seopress_social_twitter_title": article["seo_title"],
                "_seopress_social_twitter_desc": article["meta_description"],
                "_seopress_social_twitter_img": media["source_url"],
            },
        )
        verified = self._request("GET", f"posts/{post_id}", params={"context": "edit"})
        # SEOPress's computed GET response omits data for nonpublic drafts.
        # Read the registered stored fields through WordPress edit context instead.
        seo = verified.get("meta", {})
        if (
            seo.get("_seopress_titles_title") != article["seo_title"]
            or seo.get("_seopress_titles_desc") != article["meta_description"]
            or seo.get("_seopress_social_fb_img") != media["source_url"]
            or seo.get("_seopress_social_twitter_img") != media["source_url"]
            or seo.get("_seopress_robots_primary_cat") != str(category_ids[0])
        ):
            raise RuntimeError(f"SEO verification failed; post {post_id} remains a draft")
        for key in ("author", "featured_media", "status", "slug"):
            if verified.get(key) != payload[key]:
                raise RuntimeError(
                    f"WordPress {key} verification failed; post {post_id} remains a draft"
                )
        for key in ("categories", "tags"):
            if set(verified.get(key, [])) != set(payload[key]):
                raise RuntimeError(f"WordPress {key} verification failed")
        for key in ("content", "title", "excerpt"):
            if self._raw(verified, key).strip() != payload[key].strip():
                raise RuntimeError(f"WordPress altered {key}; publication blocked")
        if self.default_status == "publish":
            post = self._request("POST", f"posts/{post_id}", json={"status": "publish"})
            if post.get("status") != "publish":
                raise RuntimeError("WordPress did not confirm publication")
            self.recent_posts.insert(0, post)
        else:
            post = verified
        return post


def wp_create_post(*args, **kwargs):
    raise RuntimeError("Use the reviewed, staged WordPressClient.create_post pipeline")
