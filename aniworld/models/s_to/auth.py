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
        print("SERIENSTREAM-AUTH: automatic login attempt started", flush=True)

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
                print("SERIENSTREAM-AUTH: login page loaded", flush=True)

                # The site labels this field as an e-mail address; accept both
                # email and text inputs because its markup may vary by release.
                login_form = page.locator('form:has(input[type="password"])').first
                email_field = login_form.locator(
                    'input[type="email"], input[name*="mail" i], '
                    'input[autocomplete="username"], '
                    'input:not([type="hidden"]):not([type="password"])'
                ).first
                password_field = login_form.locator('input[type="password"]').first
                if email_field.count() == 0 or password_field.count() == 0:
                    print("SERIENSTREAM-AUTH: login form unavailable", flush=True)
                    logger.warning(
                        "SerienStream login form was not available; "
                        "the site may require verification."
                    )
                    return False

                email_field.fill(email)
                password_field.fill(password)
                print("SERIENSTREAM-AUTH: login form filled; submitting", flush=True)
                try:
                    form_info = login_form.evaluate(
                        "(form) => ({action: form.getAttribute('action'), method: form.getAttribute('method')})"
                    )
                    print(
                        "SERIENSTREAM-AUTH: form method={} action={}".format(
                            form_info.get("method") or "GET",
                            urlparse(form_info.get("action") or page.url).path,
                        ),
                        flush=True,
                    )
                except Exception:
                    pass

                login_responses = []

                def _record_login_response(response):
                    try:
                        request = response.request
                        if request.method.upper() == "POST" and "serienstream." in response.url:
                            login_responses.append((response.status, urlparse(response.url).path))
                    except Exception:
                        pass

                page.on("response", _record_login_response)

                login_requests = []

                def _record_login_request(request):
                    try:
                        if request.method.upper() == "POST" and "serienstream." in request.url:
                            login_requests.append(urlparse(request.url).path)
                    except Exception:
                        pass

                page.on("request", _record_login_request)

                try:
                    validity = login_form.evaluate(
                        "(form) => ({valid: form.checkValidity(), invalid: Array.from(form.elements).filter(e => !e.checkValidity()).map(e => ({name: e.name, type: e.type, required: e.required, valueMissing: e.validity.valueMissing}))})"
                    )
                    print(
                        "SERIENSTREAM-AUTH: native form validity={} invalid_fields={}".format(
                            validity.get("valid"),
                            [item.get("name") or item.get("type") for item in validity.get("invalid", [])],
                        ),
                        flush=True,
                    )
                    login_form.evaluate(
                        "(form) => { const button = form.querySelector('button[type=submit], input[type=submit]'); if (form.requestSubmit) form.requestSubmit(button || undefined); else form.submit(); }"
                    )
                    print("SERIENSTREAM-AUTH: form.requestSubmit() invoked", flush=True)
                except Exception as submit_exc:
                    print(
                        "SERIENSTREAM-AUTH: requestSubmit failed ({})".format(type(submit_exc).__name__),
                        flush=True,
                    )
                    raise

                try:
                    page.wait_for_function(
                        "() => !/\\/login\\/?$/.test(location.pathname)",
                        timeout=12000,
                    )
                except Exception:
                    pass
                page.wait_for_timeout(500)

                if login_requests:
                    print(
                        "SERIENSTREAM-AUTH: login POST request(s)={}".format(login_requests[-3:]),
                        flush=True,
                    )
                else:
                    print("SERIENSTREAM-AUTH: no SerienStream POST request observed", flush=True)
                if login_responses:
                    print(
                        "SERIENSTREAM-AUTH: login POST response(s)={}".format(login_responses[-3:]),
                        flush=True,
                    )
                else:
                    print("SERIENSTREAM-AUTH: no SerienStream POST response observed", flush=True)
                try:
                    challenge_frames = [
                        urlparse(frame.url).hostname or "unknown"
                        for frame in page.frames
                        if any(token in frame.url.lower() for token in (
                            "challenges.cloudflare.com", "turnstile", "hcaptcha", "recaptcha"
                        ))
                    ]
                    alerts = page.locator(
                        '[role="alert"], .alert, .error, .invalid-feedback, .text-danger'
                    ).all_inner_texts()
                    alerts = [value.strip()[:180] for value in alerts if value.strip()][:5]
                    print(
                        "SERIENSTREAM-AUTH: post-submit url={} title={!r} challenge_frames={} messages={!r}".format(
                            urlparse(page.url).path,
                            page.title()[:100],
                            challenge_frames,
                            alerts,
                        ),
                        flush=True,
                    )
                except Exception as diagnostic_exc:
                    print(
                        "SERIENSTREAM-AUTH: post-submit diagnostics unavailable ({})".format(
                            type(diagnostic_exc).__name__
                        ),
                        flush=True,
                    )

                current_path = urlparse(page.url).path.rstrip("/")
                if current_path == "/login" or page.locator('input[type="password"]').count():
                    print("SERIENSTREAM-AUTH: login not confirmed; still on login page or password field remains", flush=True)
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
                    print("SERIENSTREAM-AUTH: no SerienStream session cookies found", flush=True)
                    logger.warning(
                        "SerienStream login page changed, but no session cookies "
                        "could be transferred to the HTTP session."
                    )
                    return False

                _last_success = time.monotonic()
                print(f"SERIENSTREAM-AUTH: login successful; {copied} session cookies transferred", flush=True)
                logger.info(
                    "SerienStream account login successful; %d session cookie(s) transferred.",
                    copied,
                )
                return True

        except Exception as exc:
            print(f"SERIENSTREAM-AUTH: login attempt failed ({type(exc).__name__})", flush=True)
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
