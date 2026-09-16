# Alcorn County News: quality-first publishing

RSS is a reporting input, not a publishing quota. The default is to skip anything that cannot support a useful, accurate local article. Fewer articles, including zero on a run, is a successful outcome.

## Publication requirements

1. The feed must match the explicitly configured primary-source name and URL. Existing feeds were checked against their live feed metadata on September 15, 2026.
2. The source must have a valid publication date within 48 hours, no access-error notices, and at least 80 words of substantive text. Future timestamps are rejected. All RSS text fields are inspected.
3. Canonical source URLs are checked against local history and WordPress before any model calls. Changed GUIDs, www/mobile host variants and tracking parameters do not defeat deduplication. WordPress failures block processing.
4. A real source image must download successfully, decode correctly, be at least 1200 by 600 pixels, and pass a separate visual review. Images are never upscaled or automatically cropped. Stock-photo fallbacks are not used. A missing or unsuitable image blocks publication.
5. GPT-5 mini writes a structured proposal. It must be timely, substantial Corinth/Alcorn news or statewide information that serves local readers. Routine congratulations, thin promotions, unrelated regional stories, old news, and generic forecasts are rejected. The source must support at least 150 words and three distinct paragraphs without padding. These are minimum safeguards, not proof of quality.
6. A separate GPT-5 mini call reviews every claim, source attribution, local relevance, news value, metadata, duplicate coverage and the actual image. Every check must pass, there must be no issues, and the score must be at least 90/100. Malformed/truncated responses and service failures never approve an article.
7. WordPress stages the article as a draft. It verifies **author ID 1, exact display name Jon R Myers**, existing categories, resolved tags, full-size featured image and alt text, content, excerpt, readable slug, and stored SEOPress title/description/social-image metadata before changing status to publish. Failures leave a nonpublic draft.

The draft uses the exact WordPress author rather than adding another byline inside the article. Categories are selected from approved existing terms; no category is created automatically. Tags are two to five specific entities/topics present in the article. Related coverage is selected from real published articles (recent posts plus a cached source-entity archive search), reviewed for relevance and limited to two links. Zero internal links is appropriate when there is no useful match.

Source and image credit use the verified source name and original post link. Only the supplied RSS facts and source identity may support reporting; the model cannot invent interviews, quotations, background or local connections. Access to RSS alone does not establish independent verification or image licensing. Feed owners remain responsible for using sources/images they are entitled to republish.

## Setup and commands

```bash
python -m venv .venv
# Activate .venv for your shell
python -m pip install -e '.[dev]'
# Copy .env.example to .env, then supply your credentials
python -m rss_to_wp run --dry-run
python -m rss_to_wp run --single-feed "Local Feed 6" --dry-run
python -m rss_to_wp run
python -m rss_to_wp status
python -m pytest -q
python -m ruff check src tests
```

Dry runs read real WordPress author/category/history data and may make paid model calls, but never create posts, upload images, create tags or mark entries processed/rejected. A JSON report contains decisions and any complete previews in `data/dry-run-report.json`. Live decisions go to `data/run-report.json`. Do not use a live run as a preview.

Required environment variables: `OPENAI_API_KEY`, `WORDPRESS_BASE_URL`, `WORDPRESS_USERNAME`, `WORDPRESS_APP_PASSWORD`. The WordPress application password must permit reading/editing the configured author, uploading media, managing tags and editing SEOPress metadata. SEOPress and its registered REST meta fields are required; there is no silent metadata fallback.

Defaults are in `.env.example`: GPT-5 mini for both stages, six model candidates and two successful posts per run, America/Chicago timezone, and the verified byline. `WORDPRESS_POST_STATUS=draft` keeps fully verified articles as drafts instead of publishing. Other legacy stock-image/email settings are no longer used by this pipeline.

## Cost and scheduling

The workflow runs **hourly**, with a single concurrency group covering restore, evaluation, publication and cache save. It rotates feed priority each hour to avoid starving later feeds. The global limits apply across all feeds; there are no regeneration loops or automatic upgrades to expensive models. Each candidate uses at most one 5,000-token writer call and one 3,000-token review call, with one SDK retry for transient API errors. Rejected unchanged source text is cached for 24 hours; changed source text retries immediately. Infrastructure failures are not cached as editorial judgments.

[GPT-5 mini pricing](https://developers.openai.com/api/docs/models/gpt-5-mini) checked September 15, 2026: $0.25 per million input tokens and $2 per million output tokens. For illustration, 7,000 combined input tokens and 3,000 combined output tokens cost about **$0.00775 per candidate**, before extra image input/retries; actual use varies, including reasoning tokens. Per-call usage is logged. Thin/duplicate candidates are screened before paying the model.

GitHub Actions reads `OPENAI_MODEL` and `OPENAI_REVIEW_MODEL` from **repository variables**, defaulting to `gpt-5-mini`. A legacy `OPENAI_MODEL` secret no longer silently selects nano. Existing WordPress/API credentials remain in repository secrets. Manual runs default to dry-run. Tests/lint run before any publishing, and CI also runs on pushes and pull requests.

The SQLite cache is a performance layer, not the sole duplicate barrier. It is saved even after partial failures; WordPress source checks protect against cache loss. A partially staged draft from this exact pipeline/source/author may be resumed. Human drafts are never overwritten. Scheduled runs are serialized, but do not run separate live local/VPS publishers concurrently with GitHub Actions: WordPress core has no atomic unique-source constraint.

## Verification tools and operational limits

- `scripts/audit_site.py`: read-only author/taxonomy/source audit, saved under ignored `data/`.
- `scripts/verify_wordpress_draft.py`: explicitly creates one labeled **nonpublic** test draft, checks stored SEO fields, then trashes only that draft. Never publishes or edits an existing article.
- `scripts/verify_model_gate.py`: uses fictional fixture facts plus a blank test image to exercise rejection by the live model; no WordPress access.

Editorial rejections are normal successful runs; API, identity and publication errors fail the workflow and are recorded. Reports are retained as Actions artifacts for 14 days. No recurring summary email is sent by the new pipeline.

This automation cannot guarantee factual truth or search rankings. It conservatively rejects uncertainty rather than manufacturing reporting. Weak RSS inputs and the 1200-pixel source-image requirement will substantially reduce output. It does not fetch linked pages to fill missing facts or repair existing published posts. Review the audit reports, and improve source material if too few stories qualify; do not lower standards merely to fill a schedule.

SEO behavior follows the site's existing [SEOPress REST integration](https://www.seopress.org/support/guides/get-started-with-the-seopress-rest-api/) and [Google's image guidance](https://developers.google.com/search/docs/appearance/google-discover).
