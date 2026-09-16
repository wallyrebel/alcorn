# Alcorn County News: quality-first publishing

RSS is a reporting input, not a publishing quota. Useful sources needing human judgment go to a nonpublic review draft. Content without real news value is rejected. Only fully approved articles publish. Fewer articles, including zero on a run, is a successful outcome.

## Publication requirements

1. The feed must match the explicitly configured primary-source name and URL. Existing feeds were checked against their live feed metadata on September 15, 2026.
2. The source must have a valid publication date within 48 hours and no access-error notices. Future timestamps are rejected. All RSS text fields are inspected. Short captions and image-only official notices proceed to source assessment rather than being rejected on length.
3. Canonical source URLs are checked against local history and WordPress before any model calls. Changed GUIDs, www/mobile host variants and tracking parameters do not defeat deduplication. WordPress failures block processing.
4. GPT-5 mini assesses the source text and up to three original graphics at high detail, before writing. Readable source graphics may be as small as 200 by 200 pixels; the separate featured-image minimum remains 1200 by 600. Every supplied graphic must be accounted for. Failed downloads cause a retryable error, and extra graphics or uncertain text require human review. Image facts are provisional until an independent editor checks them against the original pixels.
5. For eligible sources, GPT-5 mini writes a structured proposal. It must be timely, substantial Corinth/Alcorn news or statewide information that serves local readers. Routine congratulations, thin promotions, unrelated routine stories, old news, and generic forecasts are rejected. Useful short notices, serious regional statements, hiring announcements or unclear facts/local relevance can instead go to draft. The source must support at least 150 words and three distinct paragraphs without padding. These are minimum safeguards, not proof of quality.
6. A separate GPT-5 mini call reviews every claim, source attribution, local relevance, news value, metadata, duplicate coverage, featured image and original source graphics. Every check must pass, there must be no issues, and the score must be at least 90/100. Useful sources that fail factual/completeness/metadata/image checks become review drafts; final-editor rejections of news value or duplicate coverage create no post. Malformed/truncated responses and service failures never approve an article.
7. WordPress stages the article as a draft. It verifies **author ID 1, exact display name Jon R Myers**, existing categories, resolved tags, full-size featured image and alt text, content, excerpt, readable slug, and stored SEOPress title/description/social-image metadata before changing status to publish. Failures leave a nonpublic draft.

The draft uses the exact WordPress author rather than adding another byline inside the article. Categories are selected from approved existing terms; no category is created automatically. Tags are two to five specific entities/topics present in the article. Related coverage is selected from real published articles (recent posts plus a cached source-entity archive search), reviewed for relevance and limited to two links. Zero internal links is appropriate when there is no useful match.

Source and image credit use the verified source name and original post link. Only supplied RSS facts, clearly legible official-source image text and source identity may support reporting; the model cannot invent interviews, quotations, background or local connections. Access to RSS alone does not establish independent verification or image licensing. Feed owners remain responsible for using sources/images they are entitled to republish.

## Publish, draft or reject

- **Publish:** Every editorial, factual, SEO, image and WordPress verification passes. The cap is five public articles per hourly run.
- **Draft:** The source has useful information, but dates, image text, local relevance, reporting depth, metadata or the featured image need work. Up to three nonpublic review drafts can be saved per run, independently of the publication counter.
- **Reject:** No real news value, stale/expired content or duplicate coverage. No WordPress draft or media upload is created.

Review drafts use the exact author and existing categories, carry a `[Review]` title, show the outstanding issues and a clearly labeled working summary, and link to the original source and graphics. They are editorial work items, not padded articles pretending to meet the publication minimum. They have no unapproved featured image or public SEO claim. Source-graphic links can expire; the permanent original post link remains available.

Their distinct `editorial-hold` marker prevents the automated publisher from treating them as resumable publishing drafts, even if the local cache is lost. Later runs never automatically promote, replace or overwrite review drafts. A human must review/edit/publish them in WordPress. The automated `quality-v1` staging drafts used during verified publishing remain a separate path.

Uncertainty stays in the review notes, not asserted as a fact in the working summary. Image-derived details receive high-detail independent verification before public use. Reading errors remain possible; nothing about a source assessment alone authorizes publication. The 80-word evidence floor and 150-word article floor apply to automatic publication, not admission to the review queue.

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

Defaults are in `.env.example`: GPT-5 mini for both stages, six evaluated sources, five published articles and at most three review drafts per hourly run, America/Chicago timezone, and the verified byline. `WORDPRESS_POST_STATUS=draft` keeps fully verified articles as drafts instead of publishing. Set optional `PEXELS_API_KEY` locally and as a GitHub repository secret to enable the controlled fallback; leave it empty to require source images. Legacy Unsplash/email settings are unused.

## Controlled Pexels fallback

The fallback searches once for a specific subject, downloads at most four landscape candidates from the official Pexels image host, and has GPT-5 mini inspect their actual pixels together. It can choose any candidate or reject them all. The selected image must also pass the final independent article/image review. No random, curated, first-result or generic "news" fallback exists. A useful source lacking an acceptable featured image goes to the review queue; the publisher does not repeatedly regenerate or search to rescue a rejected image.

Stock is limited to honest illustrations of neutral objects for ordinary services, education, agriculture, recycling or routine environment topics. It is blocked for crime, missing people, politics, illness, disasters and breaking incidents. The visual editor rejects people, identifiable buildings/landmarks, brands, readable text and weak matches. A stack of books can illustrate library borrowing rules; an unrelated library interior cannot stand in for a local renovation.

Downloaded dimensions and file size are verified. Stock disclosure and linked photographer/Pexels credit are added to **both the article and media caption**, so a theme hiding featured-image captions cannot hide the credit. Alt text describes the visible subject. The article never presents a stock photo as the actual local event.

Pexels API content is free under its [terms and API guidelines](https://www.pexels.com/api/documentation/); the model review adds a small bounded cost. A September 15, 2026 live selection test used 3,496 input tokens and 429 output tokens across the two fallback calls (about $0.0017 at the documented GPT-5 mini rates). That is a sample, not a price guarantee, and excludes subsequent writing/final review. The test downloaded four candidates and selected the third; no WordPress writes occurred. Automated visual judgment is still imperfect, so uncertainty rejects the image.

## Cost and scheduling

The workflow runs **hourly**, with a single concurrency group covering restore, evaluation, publication and cache save. It rotates feed priority each hour to avoid starving later feeds. The global limits apply across all feeds; there are no regeneration loops or automatic upgrades to expensive models. Each candidate starts with one source-assessment call capped at 3,000 output tokens. Eligible candidates then use at most one 5,000-token writer call and one 3,000-token independent review call (including original image evidence). A Pexels fallback adds at most one 1,200-token suitability/query call and one 2,000-token visual selection call, all using the affordable configured reviewer. There is one SDK retry for transient API errors. Rejected unchanged source text is cached for 24 hours; changed source text retries immediately. Infrastructure failures are not cached as editorial judgments.

[GPT-5 mini pricing](https://developers.openai.com/api/docs/models/gpt-5-mini) checked September 15, 2026: $0.25 per million input tokens and $2 per million output tokens. For illustration, 7,000 combined input tokens and 3,000 combined output tokens cost about **$0.00775 per candidate**, before extra image input/retries; actual use varies, including reasoning tokens. Per-call usage is logged. Already processed and duplicate sources are screened before model calls. New short/image-only sources receive bounded assessment so important notices are not lost to a word-count cutoff.

GitHub Actions reads `OPENAI_MODEL` and `OPENAI_REVIEW_MODEL` from **repository variables**, defaulting to `gpt-5-mini`. A legacy `OPENAI_MODEL` secret no longer silently selects nano. Existing WordPress/API credentials remain in repository secrets. Manual runs default to dry-run. Tests/lint run before any publishing, and CI also runs on pushes and pull requests.

The SQLite cache is a performance layer, not the sole duplicate barrier. It is saved even after partial failures; WordPress source checks protect against cache loss. A partially staged draft from this exact pipeline/source/author may be resumed. Human drafts are never overwritten. Scheduled runs are serialized, but do not run separate live local/VPS publishers concurrently with GitHub Actions: WordPress core has no atomic unique-source constraint.

## Verification tools and operational limits

- `scripts/audit_site.py`: read-only author/taxonomy/source audit, saved under ignored `data/`.
- `scripts/verify_wordpress_draft.py`: explicitly creates one labeled **nonpublic** test draft, checks stored SEO fields, then trashes only that draft. Never publishes or edits an existing article.
- `scripts/verify_pexels.py`: bounded live Pexels search and visual selection using fictional library-service facts; no WordPress access.
- `scripts/verify_model_gate.py`: uses fictional fixture facts plus a blank test image to exercise rejection by the live model; no WordPress access.

Editorial rejections and useful review drafts are normal successful outcomes; API, identity and publication errors fail the workflow and are recorded. Reports are retained as Actions artifacts for 14 days. No recurring summary email is sent by the new pipeline. Reports distinguish `published`, `drafts` and `skipped`, with source word counts, source text snapshots and structured assessment reasons.

This automation cannot guarantee factual truth or search rankings. It conservatively rejects uncertainty rather than manufacturing reporting. Weak RSS inputs and the requirement for a suitable 1200-pixel image will substantially reduce output. It does not fetch linked pages to fill missing facts or repair existing published posts. Review the audit reports, and improve source material if too few stories qualify; do not lower standards merely to fill a schedule.

SEO behavior follows the site's existing [SEOPress REST integration](https://www.seopress.org/support/guides/get-started-with-the-seopress-rest-api/) and [Google's image guidance](https://developers.google.com/search/docs/appearance/google-discover).
