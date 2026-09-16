"""Image download and fallback orchestration."""

from __future__ import annotations

from io import BytesIO
from typing import Optional
from urllib.parse import urlparse

import requests
from PIL import Image, ImageOps

from rss_to_wp.utils import get_logger

logger = get_logger("images.downloader")


def download_image(
    url: str,
    max_size_mb: float = 5.0,
    timeout: tuple[int, int] = (10, 30),
    *,
    allowed_hosts: set[str] | None = None,
    min_width: int = 1200,
    min_height: int = 600,
) -> Optional[tuple[bytes, str, str]]:
    """Download an image from URL.

    Args:
        url: Image URL to download.
        max_size_mb: Maximum file size in MB.
        timeout: Request timeout (connect, read).

    Returns:
        Tuple of (image_bytes, filename, content_type) or None on failure.
    """
    logger.info("downloading_image", url=url)

    try:
        if allowed_hosts and (
            urlparse(url).scheme != "https" or urlparse(url).hostname not in allowed_hosts
        ):
            return None
        response = requests.get(
            url,
            timeout=timeout,
            headers={"User-Agent": "RSS-to-WP-Bot/1.0"},
            stream=True,
            allow_redirects=not bool(allowed_hosts),
        )
        try:
            response.raise_for_status()
        except Exception:
            response.close()
            raise
        if allowed_hosts and response.status_code != 200:
            response.close()
            return None

        # Check content length
        content_length = response.headers.get("Content-Length")
        if content_length and int(content_length) > max_size_mb * 1024 * 1024:
            logger.warning("image_too_large", url=url, size_mb=int(content_length) / (1024 * 1024))
            response.close()
            return None

        # Enforce the limit while streaming, even when Content-Length is absent.
        chunks = []
        size = 0
        try:
            for chunk in response.iter_content(65536):
                size += len(chunk)
                if size > max_size_mb * 1024 * 1024:
                    return None
                chunks.append(chunk)
        finally:
            response.close()
        content = b"".join(chunks)

        # Validate it's actually an image
        try:
            with Image.open(BytesIO(content)) as img:
                img.verify()
            with Image.open(BytesIO(content)) as img:
                img = ImageOps.exif_transpose(img)
                # Do not upscale thumbnails or force crops that lose news context.
                if (
                    img.width < min_width
                    or img.height < min_height
                    or img.width * img.height > 40_000_000
                ):
                    logger.warning("image_dimensions_rejected", width=img.width, height=img.height)
                    return None
                img.thumbnail((2400, 2400))
                output = BytesIO()
                img.convert("RGB").save(output, format="JPEG", quality=88, optimize=True)
                content = output.getvalue()
        except Exception as e:
            logger.warning("invalid_image", url=url, error=str(e))
            return None

        # Determine filename and type
        content_type = "image/jpeg"
        filename = "source-photo.jpg"

        logger.info(
            "image_downloaded",
            url=url,
            size_bytes=len(content),
            content_type=content_type,
        )

        return (content, filename, content_type)

    except requests.exceptions.RequestException as e:
        logger.error("image_download_error", url=url, error=str(e))
        return None
    except Exception as e:
        logger.error("image_download_error", url=url, error=str(e))
        return None


def featured_size(image_bytes: bytes) -> bool:
    with Image.open(BytesIO(image_bytes)) as img:
        return img.width >= 1200 and img.height >= 600
