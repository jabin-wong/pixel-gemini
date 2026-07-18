"""
Google One automation using Playwright.

Logs into a Gmail account, navigates to Google One, detects the
12-month free Gemini Pro offer, and returns the activation / payment link.
"""

import logging
import time
import re
from urllib.parse import urlparse
from typing import Optional

from playwright.sync_api import sync_playwright, Page, Browser, BrowserContext, TimeoutError as PwTimeout

import config
from device_simulator import DeviceProfile

logger = logging.getLogger(__name__)


# ── Browser factory ──────────────────────────────────────────────────────────

_playwright_instance = None


def _get_playwright():
    global _playwright_instance
    if _playwright_instance is None:
        _playwright_instance = sync_playwright().start()
    return _playwright_instance


def _build_browser(profile: DeviceProfile) -> tuple:
    """Return (browser, context, page) configured for the device profile."""
    pw = _get_playwright()

    browser = pw.chromium.launch(
        headless=config.HEADLESS,
        args=[
            "--no-sandbox",
            "--disable-dev-shm-usage",
            "--disable-gpu",
            "--disable-extensions",
        ],
    )

    context = browser.new_context(
        viewport={"width": 390, "height": 844},
        device_scale_factor=3.0,
        user_agent=profile.user_agent,
        locale="en-US",
        timezone_id="America/New_York",
        permissions=["geolocation"],
        geolocation={"latitude": 40.7128, "longitude": -74.0060},
    )

    page = context.new_page()
    return browser, context, page


# ── Login helper ──────────────────────────────────────────────────────────────

def _gmail_login(page: Page, email: str, password: str) -> bool:
    """
    Perform Gmail / Google account login via Playwright.

    Returns True on success, False on failure, "2fa" if 2FA detected.
    """
    try:
        logger.info("Starting login for %s", email)
        page.goto(config.GMAIL_LOGIN_URL, wait_until="domcontentloaded")
        logger.info("Loaded login page: %s", page.url)
        page.wait_for_timeout(2000)

        # ── Email step ────────────────────────────────────────────────────────
        logger.info("Waiting for email field...")
        email_sel = 'input[type="email"], #identifierId, input[name="identifier"]'
        page.wait_for_selector(email_sel, timeout=config.WEBDRIVER_TIMEOUT * 1000)
        logger.info("Email field found, entering email")
        page.fill(email_sel, email)
        page.wait_for_timeout(500)

        # Press Enter to submit
        logger.info("Pressing Enter to submit email")
        page.press(email_sel, "Enter")
        page.wait_for_timeout(3000)
        logger.info("After Enter, current URL: %s", page.url)

        # Handle rejection
        if "signin/rejected" in page.url:
            logger.info("Google rejected sign-in, trying recovery")
            # Dump page for debugging
            try:
                with open("/tmp/google_rejected_pw.html", "w", encoding="utf-8") as f:
                    f.write(page.content())
                logger.info("Saved rejected page HTML")
            except Exception:
                pass
            # Try multiple approaches to find clickable elements
            clicked_something = False
            for sel in [
                "button",
                "a[href]",
                "[role='button']",
            ]:
                try:
                    elements = page.locator(sel)
                    count = elements.count()
                    for i in range(min(count, 20)):
                        el = elements.nth(i)
                        text = (el.text_content() or "").strip().lower()
                        if text and any(kw in text for kw in ["next", "try again", "verify", "continue", "sign in"]):
                            logger.info("Clicking '%s' (%s)", text[:50], sel)
                            el.click()
                            page.wait_for_timeout(3000)
                            logger.info("After click, URL: %s", page.url)
                            clicked_something = True
                            break
                    if clicked_something:
                        break
                except Exception as e:
                    logger.debug("Selector %s failed: %s", sel, e)

            if not clicked_something:
                logger.warning("No recovery elements found on rejected page")

            if "signin/rejected" in page.url:
                logger.warning("Still rejected after recovery attempts")
                return False

        # If still on identifier, try clicking Next button
        if "signin/identifier" in page.url:
            logger.info("Enter did not advance, trying button click")
            for btn_id in ["identifierNext", "LgbsSe"]:
                try:
                    btn = page.locator(f"#{btn_id}")
                    if btn.is_visible():
                        btn.click()
                        page.wait_for_timeout(3000)
                        logger.info("Clicked %s, URL: %s", btn_id, page.url)
                        break
                except Exception:
                    continue

        # ── Check for 2FA / challenge after email ────────────────────────────
        if _detect_2fa(page):
            logger.info("2FA challenge detected after email for %s", email)
            return "2fa"

        # ── Password step ─────────────────────────────────────────────────────
        pw_sel = 'input[type="password"], input[name="Passwd"]'
        try:
            page.wait_for_selector(pw_sel, timeout=config.WEBDRIVER_TIMEOUT * 1000)
        except PwTimeout:
            logger.error("Password field not found. URL: %s", page.url)
            return False

        logger.info("Password field found, entering password")
        page.fill(pw_sel, password)
        page.wait_for_timeout(500)

        # Submit password
        page.press(pw_sel, "Enter")
        page.wait_for_timeout(3000)

        # Also try clicking passwordNext if Enter didn't work
        if "signin" in page.url and "challenge" not in page.url:
            try:
                btn = page.locator("#passwordNext")
                if btn.is_visible():
                    btn.click()
                    page.wait_for_timeout(3000)
            except Exception:
                pass

        # ── Check for 2FA after password ─────────────────────────────────────
        if _detect_2fa(page):
            logger.info("2FA challenge detected after password for %s", email)
            return "2fa"

        # ── Verify login ──────────────────────────────────────────────────────
        current_url = page.url
        parsed = urlparse(current_url)
        hostname = parsed.hostname or ""
        path = parsed.path or ""

        if hostname == "myaccount.google.com" or (
            hostname.endswith(".google.com") and "/u/" in path
        ):
            logger.info("Login succeeded for %s", email)
            return True

        # Check for error messages
        try:
            error_el = page.locator('[jsname="B34EJ"], [aria-live="assertive"]')
            if error_el.is_visible():
                err_text = error_el.text_content()
                if err_text:
                    logger.warning("Login error: %s", err_text)
                    return False
        except Exception:
            pass

        # If we left the signin page, assume success
        if not (hostname == "accounts.google.com" and path.startswith("/signin")):
            logger.info("Login appeared successful (URL: %s)", current_url)
            return True

        logger.warning("Unexpected URL after login: %s", current_url)
        return False

    except PwTimeout as exc:
        logger.error("Timeout during login (URL: %s): %s", page.url, exc)
        return False
    except Exception as exc:
        logger.error("Error during login: %s", exc)
        return False


def _detect_2fa(page: Page) -> bool:
    """Check if the current page is a Google 2-Step Verification challenge."""
    current_url = page.url
    if any(kw in current_url.lower() for kw in ("challenge", "verify", "two-step", "rejected")):
        return True

    two_fa_selectors = [
        'input[type="tel"]',
        "#idvPin",
        "#idvAnyphoneverify",
        '[data-challengetype]',
    ]
    for sel in two_fa_selectors:
        try:
            if page.locator(sel).is_visible():
                return True
        except Exception:
            continue

    try:
        body_text = page.locator("body").text_content().lower()
        two_fa_phrases = [
            "2-step verification",
            "verify it's you",
            "verification code",
            "enter the code",
            "confirm it's you",
        ]
        if any(phrase in body_text for phrase in two_fa_phrases):
            return True
    except Exception:
        pass

    return False


def submit_2fa_code(page: Page, code: str) -> bool:
    """
    Submit a 2FA verification code on the current Google challenge page.
    Returns True if login appears successful, False otherwise.
    """
    try:
        input_sel = 'input[type="tel"], #idvPin'
        page.wait_for_selector(input_sel, timeout=10000)
        page.fill(input_sel, code)
        page.wait_for_timeout(500)

        # Click submit button
        for btn_sel in ["#idvPreregisteredPhoneNext", 'button[type="submit"]']:
            try:
                btn = page.locator(btn_sel)
                if btn.is_visible():
                    btn.click()
                    break
            except Exception:
                continue

        page.wait_for_timeout(3000)

        current_url = page.url
        parsed = urlparse(current_url)
        hostname = parsed.hostname or ""

        if hostname == "myaccount.google.com" or hostname.endswith(".google.com"):
            logger.info("2FA login succeeded")
            return True

        if "challenge" not in current_url.lower():
            logger.info("2FA login appeared successful (URL: %s)", current_url)
            return True

        return False

    except Exception as exc:
        logger.error("Error submitting 2FA code: %s", exc)
        return False


# ── Offer detection ───────────────────────────────────────────────────────────

def _extract_payment_link(page: Page) -> Optional[str]:
    """Scan the current page for a Gemini Pro offer / activation link."""
    keywords = config.GEMINI_OFFER_KEYWORDS

    all_links = page.locator("a")
    count = all_links.count()
    for i in range(min(count, 200)):
        try:
            link = all_links.nth(i)
            text = (link.text_content() or "").lower()
            href = link.get_attribute("href") or ""
            if any(kw in text for kw in keywords) and href:
                logger.info("Found offer link via text: %s", href)
                return href
        except Exception:
            continue

    url_patterns = re.compile(
        r"(gemini|upgrade|activate|offer|redeem|trial|checkout)",
        re.IGNORECASE,
    )
    for i in range(min(count, 200)):
        try:
            href = all_links.nth(i).get_attribute("href") or ""
            if url_patterns.search(href):
                logger.info("Found offer link via URL: %s", href)
                return href
        except Exception:
            continue

    return None


def _navigate_google_one(page: Page) -> Optional[str]:
    """Navigate to Google One and find the Gemini Pro offer link."""
    for url in (config.GOOGLE_ONE_URL, config.GOOGLE_ONE_OFFERS_URL):
        try:
            logger.info("Navigating to %s", url)
            page.goto(url, wait_until="domcontentloaded")
            page.wait_for_timeout(3000)

            # Dismiss cookie banners
            for sel in ('[aria-label="Accept all"]', 'button[jsname="higCR"]'):
                try:
                    btn = page.locator(sel)
                    if btn.is_visible():
                        btn.click()
                        page.wait_for_timeout(1000)
                        break
                except Exception:
                    pass

            link = _extract_payment_link(page)
            if link:
                return link

        except Exception as exc:
            logger.warning("Error accessing %s: %s", url, exc)

    return None


# ── Public API ────────────────────────────────────────────────────────────────

class GoogleAutomationError(Exception):
    """Raised when automation encounters an unrecoverable error."""


import json


def load_cookies_and_check_offer(device: DeviceProfile,
                                 cookies_json: str) -> Optional[str]:
    """
    Load cookies from JSON, navigate to Google One, and find the Gemini offer.

    The cookies should be a JSON array of cookie objects with at least
    'name', 'value', and 'domain' fields (EditThisCookie format).
    Returns the offer link or None.
    """
    browser = None
    try:
        cookies = json.loads(cookies_json)
        if not isinstance(cookies, list):
            raise GoogleAutomationError("Cookies must be a JSON array")

        logger.info("Loading %d cookies and checking Google One", len(cookies))
        browser, context, page = _build_browser(device)

        # First navigate to Google to set the domain context
        page.goto("https://accounts.google.com", wait_until="domcontentloaded")
        page.wait_for_timeout(1000)

        # Add cookies
        for cookie in cookies:
            try:
                c = {
                    "name": cookie["name"],
                    "value": cookie["value"],
                    "domain": cookie.get("domain", ".google.com"),
                    "path": cookie.get("path", "/"),
                }
                if "expirationDate" in cookie:
                    c["expires"] = cookie["expirationDate"]
                if "httpOnly" in cookie:
                    c["httpOnly"] = cookie["httpOnly"]
                if "secure" in cookie:
                    c["secure"] = cookie["secure"]
                page.context.add_cookies([c])
            except Exception as e:
                logger.warning("Skipping cookie %s: %s", cookie.get("name", "?"), e)

        logger.info("Cookies loaded, navigating to Google One")
        offer_link = _navigate_google_one(page)
        return offer_link

    except json.JSONDecodeError as e:
        raise GoogleAutomationError(f"Invalid JSON: {e}")
    finally:
        if browser:
            try:
                browser.close()
            except Exception:
                pass


def initiate_login(email: str, password: str,
                   device: DeviceProfile) -> tuple:
    """
    Start the Google login process and return (browser, context, page, status).

    Status values:
      - True: login succeeded
      - False: login failed
      - "2fa": 2FA challenge detected

    The caller is responsible for calling browser.close() when done.
    """
    browser, context, page = _build_browser(device)
    try:
        result = _gmail_login(page, email, password)
        return browser, context, page, result
    except Exception:
        try:
            browser.close()
        except Exception:
            pass
        raise


def complete_login_and_check_offer(page: Page,
                                   two_fa_code: str = None) -> Optional[str]:
    """
    Complete the login and navigate to Google One to find the Gemini Pro offer.

    If *two_fa_code* is provided, submit 2FA verification first.
    Returns the offer link (str) or None.
    """
    if two_fa_code is not None:
        if not submit_2fa_code(page, two_fa_code):
            return None
    return _navigate_google_one(page)


def check_gemini_offer(email: str, password: str,
                       device: DeviceProfile) -> Optional[str]:
    """
    Main entry point (non-interactive).

    Raises :class:`GoogleAutomationError` on failure.
    """
    browser = None
    try:
        logger.info("Starting browser for session %s", device.session_id)
        browser, context, page = _build_browser(device)

        logged_in = _gmail_login(page, email, password)
        if logged_in == "2fa":
            raise GoogleAutomationError(
                "Two-step verification required. "
                "This account has 2FA enabled – use the interactive flow."
            )
        if not logged_in:
            raise GoogleAutomationError(
                "Login failed – please check your credentials."
            )

        offer_link = _navigate_google_one(page)
        return offer_link

    finally:
        if browser:
            try:
                browser.close()
            except Exception:
                pass
