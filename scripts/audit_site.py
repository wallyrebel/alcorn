"""Read-only audit. Never print credentials or write to WordPress."""

import json
from pathlib import Path

import feedparser
import requests
import yaml
from bs4 import BeautifulSoup
from dotenv import dotenv_values

env = dotenv_values(".env")
base = env["WORDPRESS_BASE_URL"].rstrip("/")
session = requests.Session()
session.auth = (env["WORDPRESS_USERNAME"], env["WORDPRESS_APP_PASSWORD"])
report = {"base_url": base, "configured_model": env.get("OPENAI_MODEL")}
for endpoint, params in [
    ("users/me", {"context": "edit"}),
    ("users", {"per_page": 100, "_fields": "id,name,slug"}),
    ("categories", {"per_page": 100, "_fields": "id,name,slug,count"}),
    (
        "posts",
        {
            "per_page": 30,
            "_fields": "id,title,content,excerpt,author,categories,tags,featured_media,link,date,slug",
        },
    ),
]:
    r = session.get(f"{base}/wp-json/wp/v2/{endpoint}", params=params, timeout=(10, 30))
    if r.ok:
        value = r.json()
        if endpoint == "users/me":
            value = {k: value.get(k) for k in ("id", "name", "slug", "roles")}
        report[endpoint] = value
    else:
        report[endpoint] = {"status": r.status_code}
index = session.get(f"{base}/wp-json/", timeout=(10, 30))
if index.ok:
    report["namespaces"] = index.json().get("namespaces", [])
page = session.get(base, timeout=(10, 30))
soup = BeautifulSoup(page.text, "html.parser")
report["seo_meta"] = [
    str(m)
    for m in soup.select(
        'meta[name="generator"], meta[name="description"], meta[name="robots"], script[type="application/ld+json"]'
    )
][:8]
report["feeds"] = []
for item in yaml.safe_load(Path("feeds.yaml").read_text())["feeds"]:
    r = requests.get(item["url"], timeout=(10, 30))
    feed = feedparser.parse(r.content)
    report["feeds"].append(
        {
            "name": item["name"],
            "title": feed.feed.get("title"),
            "link": feed.feed.get("link"),
            "entries": feed.entries[:3],
        }
    )
out = Path("data/site-audit.json")
out.parent.mkdir(exist_ok=True)
out.write_text(json.dumps(report, indent=2, default=str), encoding="utf-8")
print(
    json.dumps(
        {k: v for k, v in report.items() if k not in ("posts", "feeds", "seo_meta")}, indent=2
    )
)
print("Saved read-only article/feed samples to data/site-audit.json")
