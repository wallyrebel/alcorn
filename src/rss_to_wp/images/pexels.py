"""Bounded Pexels search; results are candidates, never automatic approval."""

from __future__ import annotations

import re
from html import escape
from urllib.parse import urlsplit

import requests
from pydantic import BaseModel, ConfigDict, Field, field_validator

from rss_to_wp.utils import get_logger

logger = get_logger("images.pexels")
STOCK_NOTICE = "Stock illustration; does not depict the people, place or event in this report."


class StockPhoto(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    photo_id: int = Field(gt=0)
    url: str
    photo_url: str
    photographer: str = Field(min_length=1, max_length=150)
    photographer_url: str
    description: str = Field(max_length=1000)
    width: int = Field(ge=1200)
    height: int = Field(ge=600)

    @field_validator("url", "photo_url", "photographer_url")
    @classmethod
    def trusted_url(cls, value, info):
        parts = urlsplit(value)
        hosts = (
            {"images.pexels.com"} if info.field_name == "url" else {"www.pexels.com", "pexels.com"}
        )
        if (
            parts.scheme != "https"
            or parts.hostname not in hosts
            or parts.username
            or parts.password
            or parts.port not in {None, 443}
        ):
            raise ValueError("Untrusted Pexels URL")
        prefix = {"url": "/photos/", "photo_url": "/photo/", "photographer_url": "/@"}
        if not parts.path.startswith(prefix[info.field_name]):
            raise ValueError("Unexpected Pexels URL path")
        return value


def stock_credit(photo: dict) -> str:
    photo = StockPhoto.model_validate(photo)
    return (
        STOCK_NOTICE + " Photo by "
        f'<a href="{escape(photo.photographer_url, quote=True)}" rel="noopener">'
        f"{escape(photo.photographer)}</a> on "
        f'<a href="{escape(photo.photo_url, quote=True)}" rel="noopener">Pexels</a>.'
    )


def stock_topic_blocked(title: str, text: str) -> bool:
    """Hard exclusions supplement (never replace) the editor's contextual decision."""
    return bool(
        re.search(
            r"\b(?:arrest\w*|suspect\w*|victim\w*|crime\w*|criminal\w*|police|sheriff\w*|"
            r"shooting\w*|homicide\w*|murder\w*|missing|abduct\w*|assault\w*|fatal\w*|"
            r"dead|death\w*|killed|disaster\w*|tornado\w*|hurricane\w*|wildfire\w*|"
            r"flood\w*|evacuat\w*|warning\w*|election\w*|politic\w*|ballot\w*|"
            r"candidate\w*|campaign\w*|abuse\w*|addict\w*|disease\w*|patient\w*|"
            r"cancer|overdose\w*|suicid\w*|sex\w*)\b",
            title + " " + text,
            re.I,
        )
    )


class PexelsClient:
    BASE_URL = "https://api.pexels.com/v1"

    def __init__(self, api_key: str):
        self.session = requests.Session()
        self.session.headers.update({"Authorization": api_key})

    def search(self, query: str) -> list[dict]:
        """One request, at most four landscape candidates. No curated/random fallback."""
        if not re.fullmatch(r"[A-Za-z][A-Za-z -]{4,79}", query) or not 2 <= len(query.split()) <= 6:
            raise ValueError("Pexels requires a specific 2-6 word subject query")
        response = self.session.get(
            f"{self.BASE_URL}/search",
            params={"query": query, "per_page": 4, "orientation": "landscape", "locale": "en-US"},
            timeout=(10, 30),
            allow_redirects=False,
        )
        try:
            response.raise_for_status()
            if response.status_code != 200:
                raise RuntimeError("Unexpected Pexels response; no image approved")
            data = response.json()
        finally:
            response.close()
        if not isinstance(data, dict) or not isinstance(data.get("photos"), list):
            raise RuntimeError("Malformed Pexels search response")
        candidates, seen = [], set()
        for photo in data["photos"][:4]:
            try:
                candidate = StockPhoto(
                    photo_id=photo["id"],
                    url=photo["src"][
                        "large2x"
                        if photo["width"] >= 1880 and photo["height"] >= 1300
                        else "original"
                    ],
                    photo_url=photo["url"],
                    photographer=photo["photographer"],
                    photographer_url=photo["photographer_url"],
                    description=photo.get("alt", ""),
                    width=photo["width"],
                    height=photo["height"],
                )
                if candidate.width <= candidate.height or candidate.photo_id in seen:
                    continue
                seen.add(candidate.photo_id)
                candidates.append(candidate.model_dump())
            except (KeyError, TypeError, ValueError):
                logger.info("pexels_candidate_invalid")
        return candidates
