import re
from html import unescape
from urllib.parse import urlparse

try:
    from ...config import GLOBAL_SESSION
except ImportError:
    from aniworld.config import GLOBAL_SESSION

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
    response.raise_for_status()
    return response.text or "", str(getattr(response, "url", normalized_url))


def _extract_video_url(html, embed_url):
    content = unescape((html or "").replace(r"\\/", "/"))
    match = SOURCES_PATTERN.search(content) or FILE_LINK_PATTERN.search(content)
    if not match:
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
