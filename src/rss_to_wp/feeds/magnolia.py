"""Original public news releases from the community hospital in Corinth."""

from datetime import datetime, timezone
from urllib.parse import urlsplit

import feedparser
from bs4 import BeautifulSoup

HOME = "https://www.mrhc.org"
NEWS = HOME + "/wp-json/wp/v2/posts?categories=1&per_page=20&_fields=id,date_gmt,status,link,title,content,categories"


def releases(rows):
    if not isinstance(rows, list):
        raise ValueError("Magnolia Regional news archive unavailable")
    entries = []
    for row in rows:
        link = row["link"]
        parsed = urlsplit(link)
        if (
            parsed.scheme != "https"
            or parsed.netloc != "www.mrhc.org"
            or not parsed.path.startswith("/blog/news/")
            or row["status"] != "publish"
            or row["content"].get("protected")
            or 1 not in row["categories"]
        ):
            continue
        stamp = datetime.fromisoformat(row["date_gmt"]).replace(tzinfo=timezone.utc)
        title = BeautifulSoup(row["title"]["rendered"], "html.parser").get_text(" ", strip=True)
        body = BeautifulSoup(row["content"]["rendered"], "html.parser")
        for tag in body.select("script,style,figure,img,iframe"):
            tag.decompose()
        text = body.get_text(" ", strip=True)
        if title and text:
            entries.append({"id": link, "link": link, "published": stamp.isoformat(),
                            "title": title, "summary": text})
    return feedparser.FeedParserDict(
        feed={"title": "Magnolia Regional Health Center", "link": HOME}, entries=entries
    )
