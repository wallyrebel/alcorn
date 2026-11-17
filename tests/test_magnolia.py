from rss_to_wp.feeds import magnolia


def test_preserves_original_date_and_excludes_protected_and_offsite_releases():
    item = {"link": magnolia.HOME + "/blog/news/patient-entrance/", "status": "publish",
            "categories": [1], "title": {"rendered": "Patient entrance"},
            "content": {"rendered": "<p>The hospital opened its new entrance.</p><figure>Photo credit</figure>"},
            "date_gmt": "2026-11-03T18:14:27", "modified_gmt": "2026-11-17T18:14:27"}
    feed = magnolia.releases([item, {**item, "status": "draft"}, {**item, "categories": [101]},
                              {**item, "link": "https://example.com/blog/news/claim/"},
                              {**item, "content": {"rendered": "Restricted", "protected": True}}])
    assert len(feed.entries) == 1
    assert feed.entries[0]["published"] == "2026-11-03T18:14:27+00:00"
    assert feed.entries[0]["summary"] == "The hospital opened its new entrance."
