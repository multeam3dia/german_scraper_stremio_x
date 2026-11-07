import logging
import re
from html import unescape
from html import unescape
from urllib.parse import urlparse

try:
    from ...config import GLOBAL_SESSION
except ImportError:
    from aniworld.config import GLOBAL_SESSION

logger = logging.getLogger(__name__)

VIDMOLY_BASE_URL = "https://vidmoly.to/"
VIDMOLY_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/91.0.4472.124 Safari/537.36"
)

SOURCES_PATTERN = re.compile(
    r"""sources\s*:\s*\[\s*\{\s*file\s*:\s*['"]([^'"]+)['"]""",
    re.IGNORECASE,
)
FILE_LINK_PATTERN = re.compile(
    r"""file\s*:\s*['"]([^'"]+?\.m3u8[^'"]*)['"]""",
    re.IGNORECASE,
)
PREVIEW_IMAGE_PATTERN = re.compile(
    r"""image\s*:\s*['"]([^'"]+\.(?:jpg|jpeg|png|webp)(?:\?[^'"]*)?)['"]""",
    re.IGNORECASE,
)


def _normalize_embed_url(embed_url):
    """Use Vidmoly's working .to host for its known domain aliases."""
    return re.sub(
        r"(?i)^https?://(?:www\.)?vidmoly\.(?:me|net|org|biz)(?=[:/])",
        "https://vidmoly.to",
        embed_url.strip(),
    )


def _get_headers():
    return {
        "Referer": VIDMOLY_BASE_URL,
        "User-Agent": VIDMOLY_USER_AGENT,
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    }


def get_playback_headers(embed_url=None):
    """Headers required by Vidmoly when Stremio requests the HLS manifest."""
    return {
        "Referer": VIDMOLY_BASE_URL,
        "User-Agent": VIDMOLY_USER_AGENT,
        "Accept": "*/*",
    }


def _get_html(embed_url):
    if not embed_url:
        raise ValueError("Embed URL cannot be empty")
    normalized_url = _normalize_embed_url(embed_url)
    response = GLOBAL_SESSION.get(
        normalized_url,
        headers=_get_headers(),
        allow_redirects=True,
        timeout=20,
    )
    final_url = str(getattr(response, "url", normalized_url))
    html = response.text or ""
    content_type = getattr(response, "headers", {}).get("Content-Type", "unknown")
    logger.info(
        "Vidmoly embed response: status=%s final_host=%s html_length=%d content_type=%s",
        getattr(response, "status_code", "unknown"),
        urlparse(final_url).netloc,
        len(html),
        content_type.split(";", 1)[0],
    )
    if getattr(response, "status_code", 0) >= 400:
        logger.warning(
            "Vidmoly embed request returned HTTP %s (host=%s)",
            response.status_code,
            urlparse(final_url).netloc,
        )
    response.raise_for_status()
    return html, final_url


def _extract_video_url(html, embed_url):
    content = unescape((html or "").replace(r"\\/", "/"))
    match = SOURCES_PATTERN.search(content) or FILE_LINK_PATTERN.search(content)
    if not match:
        title_match = re.search(r"<title[^>]*>(.*?)</title>", content, re.IGNORECASE | re.DOTALL)
        title = re.sub(r"\\s+", " ", unescape(title_match.group(1))).strip() if title_match else "(no title)"
        visible_text = re.sub(r"<(script|style)\\b[^>]*>.*?</\\1>", " ", content, flags=re.IGNORECASE | re.DOTALL)
        visible_text = re.sub(r"<[^>]+>", " ", visible_text)
        visible_text = re.sub(r"\\s+", " ", unescape(visible_text)).strip()
        visible_text = re.sub(r"https?://\\S+", "[url]", visible_text)
        visible_text = re.sub(r"(?i)(token|key|auth|session)=\\S+", r"\\1=[redacted]", visible_text)
        logger.warning(
            "Vidmoly response summary: host=%s title=%r text=%r",
            urlparse(embed_url).netloc,
            title[:100],
            visible_text[:180],
        )
        logger.warning(
            "Vidmoly HLS source not found: host=%s html_length=%d markers=%s",
            urlparse(embed_url).netloc,
            len(content),
            {
                "sources": "sources" in content.lower(),
                "file": "file" in content.lower(),
                "m3u8": ".m3u8" in content.lower(),
                "jwplayer": "jwplayer" in content.lower(),
                "player": "player" in content.lower(),
            },
        )
        raise ValueError(
            f"Vidmoly HLS source not found (host={urlparse(embed_url).netloc})"
        )
    return match.group(1)


def get_direct_link_from_vidmoly(embed_url):
    html, final_url = _get_html(embed_url)
    return _extract_video_url(html, final_url)


def get_preview_image_link_from_vidmoly(embed_url):
    html, final_url = _get_html(embed_url)
    match = PREVIEW_IMAGE_PATTERN.search(unescape(html))
    if not match:
        raise ValueError(
            f"Vidmoly preview image not found (host={urlparse(final_url).netloc})"
        )
    return match.group(1)
