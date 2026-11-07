"""Optional SerienStream account login using container environment variables.

The login is performed in the same Patchright browser profile used by the
CAPTCHA handler. Successful cookies are copied into the shared NIQUESTS
session, which is also injected into later browser contexts.
"""

import os
import re
import time
from threading import Lock
from urllib.parse import urlparse

from ...config import GLOBAL_SESSION, logger

_LOGIN_URL = "https://serienstream.to/login"
_REFRESH_AFTER = 75 * 60  # Refresh before the site's usual ~2-hour session expiry.
_RETRY_AFTER_FAILURE = 5 * 60

_login_lock = Lock()
_last_success = 0.0
_last_attempt = 0.0


def ensure_serienstream_login() -> bool:
    """Ensure a recent SerienStream login exists; never log credentials."""
    global _last_success, _last_attempt

    email = os.getenv("SERIENSTREAM_EMAIL") or os.getenv("SERIENSTREAM_USERNAME")
    password = os.getenv("SERIENSTREAM_PASSWORD")
    if not email and not password:
        return False
    if not email or not password:
        logger.warning(
            "SerienStream login is not configured completely; set "
            "SERIENSTREAM_EMAIL and SERIENSTREAM_PASSWORD."
        )
        return False

    with _login_lock:
        now = time.monotonic()
        if _last_success and now - _last_success < _REFRESH_AFTER:
            return True
        if _last_attempt and now - _last_attempt < _RETRY_AFTER_FAILURE:
            return False
        _last_attempt = now

        handle = None
        try:
            from patchright.sync_api import sync_playwright
            from ...autodeps import _ensure_xvfb
            from ...playwright.captcha import (
                _inject_session_cookies,
                _launch_browser_context,
                _sync_session_user_agent,
            )

            _ensure_xvfb()
            with sync_playwright() as playwright:
                handle = _launch_browser_context(playwright, offscreen=True)
                context = handle.context
                _inject_session_cookies(context, _LOGIN_URL)
                page = context.new_page()
                page.goto(
                    _LOGIN_URL,
                    wait_until="domcontentloaded",
                    timeout=20000,
                )

                email_field = page.locator('input[type="email"]').first
                password_field = page.locator('input[type="password"]').first
                if email_field.count() == 0 or password_field.count() == 0:
                    logger.warning(
                        "SerienStream login form was not available; "
                        "the site may require verification."
                    )
                    return False

                email_field.fill(email)
                password_field.fill(password)

                submit = page.locator('form button[type="submit"], form input[type="submit"]').first
                if submit.count():
                    submit.click(timeout=10000)
                else:
                    page.get_by_role(
                        "button", name=re.compile("anmelden", re.IGNORECASE)
                    ).click(timeout=10000)

                try:
                    page.wait_for_function(
                        "() => !/\\/login\\/?$/.test(location.pathname)",
                        timeout=12000,
                    )
                except Exception:
                    pass
                page.wait_for_timeout(500)

                current_path = urlparse(page.url).path.rstrip("/")
                if current_path == "/login" or page.locator('input[type="password"]').count():
                    logger.warning(
                        "SerienStream login was not confirmed; check the credentials "
                        "or whether the site presented a verification challenge."
                    )
                    return False

                _sync_session_user_agent(page)
                copied = 0
                for cookie in context.cookies():
                    domain = (cookie.get("domain") or "").lstrip(".").lower()
                    if "serienstream." not in domain:
                        continue
                    GLOBAL_SESSION.cookies.set(
                        cookie["name"],
                        cookie["value"],
                        domain=domain,
                        path=cookie.get("path") or "/",
                    )
                    copied += 1

                if copied == 0:
                    logger.warning(
                        "SerienStream login page changed, but no session cookies "
                        "could be transferred to the HTTP session."
                    )
                    return False

                _last_success = time.monotonic()
                logger.info(
                    "SerienStream account login successful; %d session cookie(s) transferred.",
                    copied,
                )
                return True

        except Exception as exc:
            logger.warning(
                "SerienStream automatic login failed (%s); continuing with the existing access path.",
                type(exc).__name__,
            )
            return False
        finally:
            if handle is not None:
                try:
                    handle.close()
                except Exception:
                    pass
