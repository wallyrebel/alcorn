"""One live visual-review test: fictional fixture article plus a blank image.

No WordPress calls. Expected result: the editor rejects the unsuitable image.
"""

import base64
import json
import runpy
from pathlib import Path

from rss_to_wp.config import get_app_settings
from rss_to_wp.content_policy import plain_text
from rss_to_wp.editorial import Review
from rss_to_wp.rewriter.openai_client import SOURCE_REVIEW_PROMPT, OpenAIRewriter

fixtures = runpy.run_path("tests/conftest.py")
article = fixtures["article"].__wrapped__()
image_bytes = fixtures["image_bytes"].__wrapped__()
context = fixtures["context"].__wrapped__()
evidence = {
    **context,
    "rss_title": article["headline"],
    "rss_content": plain_text(article["body"]),
    "source_published_at": "2026-09-15T09:00:00-05:00",
    "current_time": "2026-09-15T10:00:00-05:00",
    "article": article,
}
settings = get_app_settings()
writer = OpenAIRewriter(
    settings.openai_api_key, settings.openai_model, review_model=settings.openai_review_model
)
review = writer._request(
    settings.openai_review_model,
    SOURCE_REVIEW_PROMPT,
    [
        {"type": "text", "text": json.dumps(evidence)},
        {
            "type": "image_url",
            "image_url": {
                "url": "data:image/jpeg;base64," + base64.b64encode(image_bytes).decode(),
                "detail": "low",
            },
        },
    ],
    Review,
    3000,
)
result = review.model_dump()
Path("data/model-smoke-report.json").write_text(json.dumps(result, indent=2), encoding="utf-8")
print(json.dumps(result, indent=2))
assert review.image_relevant is False and review.issues, "Blank image incorrectly approved"
