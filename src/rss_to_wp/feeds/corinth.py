"""Read the Corinth School District's dated original news releases."""

from urllib.parse import urljoin, urlsplit

import feedparser
import requests
from bs4 import BeautifulSoup

from rss_to_wp.feeds.filter import parse_entry_date
from rss_to_wp.utils import get_logger

HOME = "https://www.corinth.k12.ms.us"
NEWS = HOME + "/connect/news"
HEADERS = {"User-Agent": "AlcornNewsRSS/2.0"}
logger = get_logger("feeds.corinth")


def _release(href, post_id):
    parsed = urlsplit(href)
    if (
        parsed.scheme != "https"
        or parsed.netloc != "www.corinth.k12.ms.us"
        or not parsed.path.startswith("/details-page/~board/")
        or "/post/" not in parsed.path
        or not post_id.isdecimal()
    ):
        raise ValueError("Unexpected district release URL or identifier")

    response = requests.get(href, headers=HEADERS, timeout=(5, 15), allow_redirects=False)
    if response.status_code != 200:
        raise ValueError(f"District release returned HTTP {response.status_code}")
    page = BeautifulSoup(response.content, "html.parser")
    article = page.find("article", attrs={"data-post-id": post_id})
    title = article.select_one(".fsTitle") if article else None
    date = article.select_one("time.fsDate[datetime]") if article else None
    if not title or not title.get_text(strip=True) or not date:
        raise ValueError("Release is missing its title, date or matching district identifier")
    published = date["datetime"]
    if parse_entry_date({"published": published}) is None:
        raise ValueError("Release date is invalid")

    body = article.select_one(".fsBody")
    # Document-only cards cannot support a standalone article.
    if body and body.get_text(" ", strip=True):
        return {
            "id": href,
            "link": href,
            "title": title.get_text(" ", strip=True),
            "published": published,
            "summary": str(body),
        }


def district_news(index_html):
    soup = BeautifulSoup(index_html, "html.parser")
    releases = {}
    for card in soup.select("div[data-board-id] article[data-post-id]"):
        link = card.select_one(".fsTitle a.fsPostLink[href]")
        if link:
            releases.setdefault(card["data-post-id"], urljoin(HOME, link["href"]))
    if not releases:
        raise ValueError("Corinth School District news index unavailable")

    entries = []
    checked = 0
    for post_id, href in list(releases.items())[:10]:
        try:
            entry = _release(href, post_id)
        except (requests.RequestException, ValueError) as exc:
            logger.warning("district_release_skipped", post_id=post_id, reason=str(exc))
            continue
        checked += 1
        if entry:
            entries.append(entry)
    if not checked:
        raise ValueError("No district release could be verified")
    return feedparser.FeedParserDict(
        feed={"title": "Corinth School District", "link": HOME}, entries=entries
    )
