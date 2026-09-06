# Alcorn County News - RSS to WordPress Automation

Automated RSS feed monitoring, AI-powered article rewriting, and WordPress publishing for [Alcorn News MS](https://alcornnewsms.com/).

## Features

- **RSS Feed Monitoring**: Parse RSS/Atom feeds with robust error handling
- **AI Rewriting**: Convert RSS entries to AP-style articles using the configured OpenAI model (default: gpt-4.1-nano)
- **Smart Deduplication**: SQLite-based tracking ensures no duplicate posts
- **Image Handling**: 
  - Extract images from RSS (media:content, enclosures, HTML)
  - Fallback to Pexels/Unsplash for stock photos
  - Proper attribution in alt text
- **WordPress Publishing**: Full REST API integration with categories and tags
- **Scheduling**: GitHub Actions (every 15 min) or VPS cron/systemd

## Source fidelity and publishing safeguards

The RSS entry is the sole factual source. The writer must preserve its facts,
attribution and uncertainty without fetching linked articles or adding outside
knowledge. Aim for a normal article with paragraphs when the material supports
it, but **there is no minimum word or paragraph count**. A short factual item can
be published as one paragraph; never add filler to reach a length target.

Unavailable, restricted, deleted, login-only and failed-source notices are
rejected before rewriting. All RSS text fields are checked, including a summary
when full content is also present. Generated headlines, excerpts and bodies are
checked again, with a final access-error guard before WordPress requests.

Every rewrite requires a separate model review against the exact RSS title and
text. Unsupported claims, unusable sources, missing verdicts, malformed JSON,
truncated replies and review failures cannot be published. There is no fallback
that salvages unvalidated model output. This adds a second model request per
candidate article. Automated semantic review reduces errors but is not a proof
of factual accuracy; editorial sampling is still appropriate. Source access
notices may conservatively block an actual story quoting such a notice.

Policy skips are logged with a reason and are not marked as published in the
deduplication database, so repaired source entries can be retried within the
normal time window. Sources over 10,000 text characters are skipped for manual
handling rather than silently truncated. Dry runs never mark entries processed.

Run the offline regression checks with `python -m pytest -q` after installing
`pip install -e '.[dev]'`. They use canned RSS and model responses and mock
WordPress; no live post or notification is sent. The scheduled workflow runs
these checks before processing feeds. Inspect `content_rejected`,
`entry_skipped_content_policy` and `openai_rewrite_error` in run logs when an
expected article does not appear.

## Quick Start

### 1. Clone and Install

```bash
git clone https://github.com/wallyrebel/alcorn.git
cd alcorn

# Create virtual environment
python -m venv .venv
source .venv/bin/activate  # Linux/Mac
# .venv\Scripts\activate   # Windows

# Install dependencies
pip install -r requirements.txt
pip install -e .
```

### 2. Configure Environment

```bash
cp .env.example .env
# Edit .env with your API keys
```

**Required variables:**
- `OPENAI_API_KEY` - Your OpenAI API key
- `WORDPRESS_BASE_URL` - Your WordPress site URL
- `WORDPRESS_USERNAME` - WordPress username
- `WORDPRESS_APP_PASSWORD` - [Generate an Application Password](https://make.wordpress.org/core/2020/11/05/application-passwords-integration-guide/)

**Optional variables:**
- `PEXELS_API_KEY` - For fallback images ([Get key](https://www.pexels.com/api/))
- `UNSPLASH_ACCESS_KEY` - For fallback images ([Get key](https://unsplash.com/developers))

### 3. Configure Feeds

Edit `feeds.yaml`:

```yaml
feeds:
  - name: "Local News"
    url: "https://example.com/rss"
    default_category: "News"
    default_tags:
      - "Local"
    max_per_run: 5
```

### 4. Run

```bash
# Full run
python -m rss_to_wp run --config feeds.yaml

# Dry run (no publishing)
python -m rss_to_wp run --config feeds.yaml --dry-run

# Single feed only
python -m rss_to_wp run --config feeds.yaml --single-feed "Local News"

# Check status
python -m rss_to_wp status
```

## CLI Commands

| Command | Description |
|---------|-------------|
| `run` | Process feeds and publish to WordPress |
| `status` | Show processed entry count and recent entries |
| `clear-db` | Clear the deduplication database |

### Run Options

| Option | Description |
|--------|-------------|
| `--config`, `-c` | Path to feeds.yaml (default: feeds.yaml) |
| `--dry-run`, `-n` | Process without publishing |
| `--single-feed`, `-f` | Process only named feed |
| `--hours`, `-h` | Time window in hours (default: 48) |

## Feed Configuration

```yaml
feeds:
  - name: "Feed Name"              # Required: Display name
    url: "https://..."             # Required: RSS/Atom URL
    default_category: "News"       # Optional: WordPress category
    default_tags:                  # Optional: Tags to apply
      - "Tag1"
      - "Tag2"
    max_per_run: 5                 # Optional: Max entries per run (default: 5)
    use_original_title: false      # Optional: Keep original title (default: false)
```

## GitHub Actions Setup

The workflow runs every 15 minutes automatically.

### Required Secrets

Go to **Settings > Secrets and variables > Actions** and add:

| Secret | Required | Description |
|--------|----------|-------------|
| `OPENAI_API_KEY` | ✅ | OpenAI API key |
| `WORDPRESS_BASE_URL` | ✅ | Site URL (e.g., `https://example.com`) |
| `WORDPRESS_USERNAME` | ✅ | WordPress username |
| `WORDPRESS_APP_PASSWORD` | ✅ | Application password |
| `PEXELS_API_KEY` | ❌ | Pexels API key |
| `UNSPLASH_ACCESS_KEY` | ❌ | Unsplash access key |
| `TIMEZONE` | ❌ | Timezone (default: UTC) |

### Manual Trigger

You can manually trigger the workflow from the Actions tab with options for dry-run and single-feed.

## VPS/Cron Deployment

### Using Cron

```bash
# Edit crontab
crontab -e

# Add (runs every 15 minutes)
*/15 * * * * cd /path/to/project && /path/to/.venv/bin/python -m rss_to_wp run --config feeds.yaml >> /var/log/rss-to-wp.log 2>&1
```

### Using Systemd

Create `/etc/systemd/system/rss-to-wp.service`:

```ini
[Unit]
Description=RSS to WordPress Automation
After=network.target

[Service]
Type=oneshot
User=www-data
WorkingDirectory=/path/to/project
EnvironmentFile=/path/to/project/.env
ExecStart=/path/to/.venv/bin/python -m rss_to_wp run --config feeds.yaml

[Install]
WantedBy=multi-user.target
```

Create `/etc/systemd/system/rss-to-wp.timer`:

```ini
[Unit]
Description=Run RSS to WordPress every 15 minutes

[Timer]
OnBootSec=5min
OnUnitActiveSec=15min

[Install]
WantedBy=timers.target
```

Enable:

```bash
sudo systemctl enable rss-to-wp.timer
sudo systemctl start rss-to-wp.timer
```

## Project Structure

```
.
├── src/rss_to_wp/
│   ├── __init__.py
│   ├── __main__.py
│   ├── cli.py              # CLI commands
│   ├── config.py           # Configuration models
│   ├── feeds/              # RSS parsing & filtering
│   ├── images/             # Image extraction & fallbacks
│   ├── rewriter/           # OpenAI AP-style rewriting
│   ├── storage/            # SQLite deduplication
│   ├── utils/              # Logging & HTTP utilities
│   └── wordpress/          # WP REST API client
├── data/                   # Runtime data (gitignored)
│   └── processed.db
├── .github/workflows/
│   └── rss_to_wp.yml
├── feeds.yaml
├── .env.example
├── pyproject.toml
├── requirements.txt
└── README.md
```

## Troubleshooting

### Common Issues

**"Config file not found"**
- Ensure `feeds.yaml` exists in the working directory

**"Error loading settings"**
- Check `.env` file exists and has required variables
- Verify no typos in environment variable names

**"WordPress authentication failed"**
- Verify Application Password is correct (no spaces in password)
- Ensure user has publishing permissions

**"No entries found"**
- Check if RSS feed URL is accessible
- Verify entries are within 48-hour window

### Debug Mode

```bash
LOG_LEVEL=DEBUG python -m rss_to_wp run --config feeds.yaml
```

## License

MIT License

## Local section routing

On `alcornnewsms.com`, new articles with clear Corinth city or local-institution
signals in the original RSS title/body also receive the existing **Corinth MS News**
category. The feed's default category is retained. Ambiguous names and explicit
other-state Corinth references are left alone; generated text cannot trigger routing.
This keeps the Corinth archive and homepage section current without adding facts,
changing old article URLs, or imposing a minimum article length. Other sites are not
affected. Category decisions appear in the run log and dry-run preview.
