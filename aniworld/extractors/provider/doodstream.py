import logging
import random
import re
import time
import warnings
from threading import RLock
from urllib.parse import urljoin, urlparse

import niquests
from urllib3.exceptions import InsecureRequestWarning

try:
    from ...config import DEFAULT_USER_AGENT, GLOBAL_SESSION
    from ...playwright.captcha import is_captcha_page
except ImportError:
    from aniworld.config import DEFAULT_USER_AGENT, GLOBAL_SESSION
    from aniworld.playwright.captcha import is_captcha_page

warnings.simplefilter("ignore", InsecureRequestWarning)
logger = logging.getLogger(__name__)

DOODSTREAM_BASE_URL = "https://dood.li"
RANDOM_STRING_CHARS = "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789"
PASS_MD5_PATTERN = r"""\$\s*\.\s*get\s*\(\s*['"]([^'"]*?/pass_md5/[^'"]*)['"]"""
PASS_MD5_FALLBACK_PATTERN = r"""['"]([^'"]*?/pass_md5/[^'"]*)['"]"""
TOKEN_PATTERN = r"[?&]token=([a-zA-Z0-9_-]+)"
PLAYBACK_USER_AGENT = (
    "Mozilla/5.0 (iPhone; CPU iPhone OS 18_0 like Mac OS X) "
    "AppleWebKit/605.1.15 (KHTML, like Gecko) Version/18.0 "
    "Mobile/15E148 Safari/604.1"
)

_PLAYBACK_HEADERS = {}
_PLAYBACK_HEADERS_LOCK = RLock()


def _get_headers(referer=None):
    return {
        "User-Agent": DEFAULT_USER_AGENT,
        "Referer": referer or f"{DOODSTREAM_BASE_URL}/",
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "en-US,en;q=0.9",
    }


def _generate_random_string(length=10):
    return "".join(random.choices(RANDOM_STRING_CHARS, k=length))


def _extract_pass_md5(html, embed_url):
    match = re.search(PASS_MD5_PATTERN, html or "")
    if not match:
        match = re.search(PASS_MD5_FALLBACK_PATTERN, html or "")
    if not match:
        raise ValueError(f"pass_md5 URL not found in {urlparse(embed_url).netloc}")
    return match.group(1)


def _get_cookie_header(session, url):
    """Return cookies applicable to the final embed host without logging them."""
    parsed_host = (urlparse(url).hostname or "").lower()
    jar = getattr(session, "cookies", None)
    if jar is None:
        return ""
    pairs = []
    try:
        for cookie in jar:
            domain = (getattr(cookie, "domain", "") or "").lstrip(".").lower()
            if not domain or parsed_host == domain or parsed_host.endswith("." + domain):
                pairs.append(f"{cookie.name}={cookie.value}")
    except Exception:
        try:
            pairs = [f"{key}={value}" for key, value in jar.get_dict().items()]
        except Exception:
            return ""
    return "; ".join(dict.fromkeys(pairs))


def get_playback_headers(embed_url):
    """Headers Stremio must send when requesting the resolved Doodstream media."""
    with _PLAYBACK_HEADERS_LOCK:
        return dict(_PLAYBACK_HEADERS.get(embed_url, {}))


def get_direct_link_from_doodstream(embed_url):
    """Resolve a Doodstream embed using one cookie-preserving browser session."""
    if not embed_url:
        raise ValueError("Embed URL cannot be empty")

    request_headers = _get_headers(embed_url)
    session = None
    try:
        from curl_cffi import requests as curl_requests

        session = curl_requests.Session(impersonate="chrome124")
        response = session.get(
            embed_url,
            headers=request_headers,
            allow_redirects=True,
            verify=False,
            timeout=20,
        )
        logger.debug("Doodstream embed fetched with curl_cffi Chrome impersonation")
    except ImportError:
        logger.debug("curl_cffi unavailable; using shared Niquests session")
        session = GLOBAL_SESSION
        response = session.get(embed_url, headers=request_headers, verify=False, timeout=20)
    except Exception as exc:
        logger.warning("Doodstream Chrome-impersonated request failed (%s)", exc)
        raise

    response.raise_for_status()
    final_url = str(getattr(response, "url", embed_url))
    html = response.text or ""
    logger.debug(
        "Doodstream embed response: status=%s final_host=%s html_length=%d",
        response.status_code,
        urlparse(final_url).netloc,
        len(html),
    )
    if is_captcha_page(html, response.status_code):
        logger.warning(
            "Doodstream returned a Cloudflare/CAPTCHA page (host=%s)",
            urlparse(final_url).netloc,
        )
        raise ValueError("Doodstream embed returned a CAPTCHA/interstitial page")

    md5_path = _extract_pass_md5(html, final_url)
    md5_url = urljoin(final_url, md5_path)
    token_match = re.search(TOKEN_PATTERN, html)
    token = token_match.group(1) if token_match else md5_url.rstrip("/").rsplit("/", 1)[-1]
    if not token:
        raise ValueError("Doodstream token could not be determined")

    pass_headers = {
        "User-Agent": request_headers["User-Agent"],
        "Accept": "*/*",
        "Range": "bytes=0-",
        "Referer": final_url,
    }
    md5_response = session.get(
        md5_url,
        headers=pass_headers,
        allow_redirects=True,
        verify=False,
        timeout=20,
    )
    md5_response.raise_for_status()
    video_prefix = (md5_response.text or "").strip()
    if not video_prefix:
        raise ValueError("Empty Doodstream pass_md5 response")

    direct_link = (
        video_prefix
        + _generate_random_string(10)
        + f"?token={token}&expiry={int(time.time() * 1000)}"
    )

    final_origin = f"{urlparse(final_url).scheme}://{urlparse(final_url).netloc}"
    playback_headers = {
        "Referer": final_url,
        "Origin": final_origin,
        "User-Agent": PLAYBACK_USER_AGENT,
        "Accept": "*/*",
    }
    cookie_header = _get_cookie_header(session, final_url)
    if cookie_header:
        playback_headers["Cookie"] = cookie_header

    with _PLAYBACK_HEADERS_LOCK:
        _PLAYBACK_HEADERS[embed_url] = playback_headers
        _PLAYBACK_HEADERS[final_url] = playback_headers

    logger.info("Successfully resolved Doodstream media URL (host=%s)", urlparse(video_prefix).netloc)
    return direct_link


def get_preview_image_link_from_doodstream(embed_url):
    raise NotImplementedError("Preview image extraction is not implemented yet.")
