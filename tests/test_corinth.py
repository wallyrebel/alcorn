from types import SimpleNamespace

import pytest

from rss_to_wp.feeds import corinth, parser


def card(post_id, href):
    return (
        f'<article data-post-id="{post_id}"><h2 class="fsTitle">'
        f'<a class="fsPostLink" href="{href}">News</a></h2></article>'
    )


def release(post_id, day, text):
    return (
        f'<article data-post-id="{post_id}"><h1 class="fsTitle">Calendar</h1>'
        f'<time class="fsDate" datetime="{day}"></time><div class="fsBody">{text}'
        f'</div></article>'
    )


def test_parse_district_news_matches_each_article_and_continues_after_unavailable_releases(monkeypatch):
    href = "/details-page/~board/district/post/calendar"
    cards = [card(21, href), card(21, href), card(22, href + "-missing"),
             card(23, "https://example.org" + href), card(24, href + "-document")]
    index = '<div data-board-id="2">' + "".join(cards) + "</div>"
    requested = []

    def get(url, **kwargs):
        requested.append(url)
        if url == corinth.NEWS:
            return SimpleNamespace(url=url, content=index.encode(), raise_for_status=lambda: None)
        assert kwargs["allow_redirects"] is False
        assert url.startswith(corinth.HOME + "/")
        pages = {
            href: release(17, "2025-11-01", "Other card") + release(21, "2026-11-10", "Dated story."),
            href + "-document": release(24, "2026-11-05", ""),
        }
        path = url.removeprefix(corinth.HOME)
        return SimpleNamespace(status_code=200 if path in pages else 404, content=pages.get(path, "").encode())

    monkeypatch.setattr(parser.requests, "get", get)
    feed = parser.parse_feed(corinth.NEWS)
    assert feed.feed["link"] == corinth.HOME
    assert len(feed.entries) == 1
    assert feed.entries[0]["published"] == "2026-11-10"
    assert "Dated story" in feed.entries[0]["summary"]
    assert requested.count(corinth.HOME + href) == 1


@pytest.mark.parametrize("body", [
    release(100, "2026-11-10", "Other release."),
    release(21, "undated", "Actual release without a valid date."),
])
def test_district_news_rejects_mismatches_and_missing_dates(monkeypatch, body):
    href = "/details-page/~board/district/post/calendar"
    monkeypatch.setattr(corinth.requests, "get", lambda *a, **kw: SimpleNamespace(status_code=200, content=body))
    with pytest.raises(ValueError, match="No district release"):
        corinth.district_news('<div data-board-id="2">' + card(21, href) + '</div>')
