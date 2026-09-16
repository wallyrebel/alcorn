from io import BytesIO
from unittest.mock import Mock

import pytest
from PIL import Image

from rss_to_wp.config import AppSettings, FeedConfig


@pytest.fixture(autouse=True)
def block_network(monkeypatch):
    monkeypatch.setattr(
        "requests.sessions.Session.request", Mock(side_effect=AssertionError("network forbidden"))
    )


@pytest.fixture
def settings():
    return AppSettings(
        _env_file=None,
        openai_api_key="unused",
        wordpress_base_url="https://example.test",
        wordpress_username="unused",
        wordpress_app_password="unused",
        pexels_api_key=None,
    )


@pytest.fixture
def feed_config():
    return FeedConfig(
        name="Library",
        url="https://example.test/rss",
        source_name="Corinth Library",
        source_url="https://example.test/library",
        primary_source=True,
    )


@pytest.fixture
def image_bytes():
    buf = BytesIO()
    Image.new("RGB", (1200, 800), "blue").save(buf, "JPEG")
    return buf.getvalue()


@pytest.fixture
def article():
    # Substantive fixture with distinct paragraphs and exact entity tags.
    paragraphs = [
        "Corinth Library will open a new reading room on September 20, according to a statement from the library. "
        "The room will be available to Alcorn County residents during regular hours. Staff will offer an opening "
        "tour at 10 a.m. and explain how visitors can reserve tables for study sessions and small group meetings.",
        "The renovation includes new tables, chairs and shelves purchased through a library grant. The library "
        "said the room will hold 30 people and include six computers for public use. Residents may reserve a "
        "computer at the front desk. Each session will last one hour, with extensions available when nobody is waiting.",
        "Registration for the opening tour begins September 16 at the front desk. The library said attendance "
        "is free and registration is needed to keep each tour within the room capacity. Visitors who need "
        "assistance can speak to staff before the tour. The library will also distribute a printed schedule "
        "of the new room hours and reservation procedures during the opening event.",
    ]
    return dict(
        publish=True,
        reason="Useful local service announcement",
        headline="Corinth Library to open new reading room Sept. 20",
        excerpt="Corinth Library will open a new reading room on Sept. 20, with public computers and study tables available to residents.",
        body="".join(f"<p>{p}</p>" for p in paragraphs),
        slug="corinth-library-new-reading-room",
        seo_title="Corinth Library to open reading room Sept. 20",
        meta_description="Corinth Library opens a reading room Sept. 20 with study tables and six public computers. Registration for opening tours begins Sept. 16.",
        category_slugs=["corinth-news"],
        tags=["Corinth Library", "Alcorn County"],
        related_post_ids=[],
    )


@pytest.fixture
def review():
    return dict(
        source_usable=True,
        faithful=True,
        newsworthy=True,
        locally_relevant=True,
        sufficiently_reported=True,
        metadata_accurate=True,
        not_duplicate=True,
        image_relevant=True,
        image_alt="Tables and shelves inside a library reading room",
        image_caption="The library reading room with tables and shelves.",
        quality_score=95,
        issues=[],
    )


@pytest.fixture
def context():
    return {
        "categories": [{"id": 221, "slug": "corinth-news", "name": "Corinth News"}],
        "related_posts": [],
        "source_name": "Corinth Library",
    }
