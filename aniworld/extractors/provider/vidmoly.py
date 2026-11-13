import logging
import re
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
JS_REDIRECT_PATTERN = re.compile(
    r"""(?:window\.)?location\.(?:replace|assign)\(\s*['"]([^'"]+)['"]""",
    re.IGNORECASE,
)


def _normalize_embed_url(embed_url):
    """Normalize legacy aliases, but preserve .biz which AniWorld currently uses."""
    return re.sub(
        r"(?i)^https?://(?:www\.)?vidmoly\.(?:me|net|org)(?=[:/])",
        "https://vidmoly.to",
        embed_url.strip(),
    )


def _origin_referer(embed_url):
    parsed = urlparse(embed_url)
    if parsed.scheme and parsed.netloc:
        return f"{parsed.scheme}://{parsed.netloc}/"
    return VIDMOLY_BASE_URL


def _get_headers(embed_url):
    return {
        "Referer": _origin_referer(embed_url),
        "User-Agent": VIDMOLY_USER_AGENT,
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        # niquests currently exposes Vidmoly's Brotli response as undecoded
        # binary text. Request an identity representation so the player HTML
        # can be parsed reliably.
        "Accept-Encoding": "identity",
    }


def get_playback_headers(embed_url=None):
    """Headers required by Vidmoly when Stremio requests the HLS manifest."""
    return {
        "Referer": _origin_referer(embed_url) if embed_url else VIDMOLY_BASE_URL,
        "User-Agent": VIDMOLY_USER_AGENT,
        "Accept": "*/*",
    }


def _get_html_with_browser(embed_url):
    """Execute Vidmoly's JavaScript redirect and return the final document."""
    from patchright.sync_api import sync_playwright
    from ...autodeps import _ensure_xvfb
    from ...playwright.captcha import (
        _inject_session_cookies,
        _launch_browser_context,
    )

    _ensure_xvfb()
    with sync_playwright() as playwright:
        handle = _launch_browser_context(playwright, offscreen=True)
        try:
            context = handle.context
            _inject_session_cookies(context, embed_url)
            page = context.new_page()
            page.goto(
                embed_url,
                wait_until="domcontentloaded",
                timeout=30000,
            )

            # Vidmoly currently serves a tiny "Loading..." document which
            # redirects to the real embed page through location.replace().
            # Let Chromium execute that script and settle any follow-up page.
            for _ in range(10):
                page.wait_for_timeout(500)
                try:
                    title = page.title()
                except Exception:
                    title = ""
                if title and title.lower() != "loading...":
                    break

            final_url = page.url
            html = page.content()
            logger.info(
                "Vidmoly browser navigation: final_host=%s html_length=%d title=%r",
                urlparse(final_url).netloc,
                len(html or ""),
                title[:80],
            )
            return html, final_url
        finally:
            handle.close()


def _get_html(embed_url):
    if not embed_url:
        raise ValueError("Embed URL cannot be empty")
    normalized_url = _normalize_embed_url(embed_url)
    response = GLOBAL_SESSION.get(
        normalized_url,
        headers=_get_headers(normalized_url),
        allow_redirects=True,
        timeout=20,
    )
    final_url = str(getattr(response, "url", normalized_url))
    html = response.text or ""
    response_headers = getattr(response, "headers", {}) or {}
    content_type = response_headers.get("Content-Type", "unknown")
    raw_content = getattr(response, "content", b"") or b""
    raw_prefix = raw_content[:16].hex(" ")
    content_encoding = response_headers.get("Content-Encoding", "identity")
    content_length = response_headers.get("Content-Length", "unknown")
    transfer_encoding = response_headers.get("Transfer-Encoding", "unknown")
    response_encoding = getattr(response, "encoding", None)
    logger.info(
        "Vidmoly embed response: status=%s final_host=%s text_length=%d raw_length=%d "
        "content_type=%s content_encoding=%s content_length=%s transfer_encoding=%s "
        "response_encoding=%s raw_prefix_hex=%s",
        getattr(response, "status_code", "unknown"),
        urlparse(final_url).netloc,
        len(html),
        len(raw_content),
        content_type.split(";", 1)[0],
        content_encoding,
        content_length,
        transfer_encoding,
        response_encoding,
        raw_prefix,
    )
    if getattr(response, "status_code", 0) >= 400:
        logger.warning(
            "Vidmoly embed request returned HTTP %s (host=%s)",
            response.status_code,
            urlparse(final_url).netloc,
        )
    response.raise_for_status()

    # Do not try to parse the 495-byte JavaScript redirect stub as a player.
    # Use Chromium only for this client-side redirect path; normal embeds keep
    # the faster shared HTTP-session path.
    if JS_REDIRECT_PATTERN.search(html):
        try:
            return _get_html_with_browser(normalized_url)
        except Exception as exc:
            logger.warning(
                "Vidmoly browser redirect follow-up failed (%s); keeping HTTP response",
                type(exc).__name__,
            )

    return html, final_url


def _safe_visible_text(html):
    content = unescape(html or "")
    content = re.sub(
        r"<(script|style)\b[^>]*>.*?</\1>",
        " ",
        content,
        flags=re.IGNORECASE | re.DOTALL,
    )
    content = re.sub(r"<[^>]+>", " ", content)
    content = re.sub(r"\s+", " ", content).strip()
    # Remove complete URLs, including JWTs and other query-string credentials.
    content = re.sub(r"""https?://[^'"\s]+""", "[url]", content)
    content = re.sub(
        r"(?i)\b(token|key|auth|session|js|ch)=\S+",
        r"\1=[redacted]",
        content,
    )
    return content


def _extract_video_url(html, embed_url):
    content = unescape((html or "").replace(r"\\/", "/"))
    match = SOURCES_PATTERN.search(content) or FILE_LINK_PATTERN.search(content)
    if not match:
        title_match = re.search(
            r"<title[^>]*>(.*?)</title>", content, re.IGNORECASE | re.DOTALL
        )
        title = (
            re.sub(r"\s+", " ", unescape(title_match.group(1))).strip()
            if title_match
            else "(no title)"
        )
        visible_text = _safe_visible_text(content)
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
                "js_redirect": bool(JS_REDIRECT_PATTERN.search(content)),
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
