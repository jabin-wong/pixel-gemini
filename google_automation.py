"""
Google One automation using Selenium.

Logs into a Gmail account, navigates to Google One, detects the
12-month free Gemini Pro offer, and returns the activation / payment link.
"""

import logging
import time
import re
from urllib.parse import urlparse
from typing import Optional

from selenium import webdriver
from selenium.common.exceptions import (
    NoSuchElementException,
    TimeoutException,
    WebDriverException,
)
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.chrome.service import Service
from selenium.webdriver.common.by import By
from selenium.webdriver.common.keys import Keys
from selenium.webdriver.support import expected_conditions as EC
from selenium.webdriver.support.ui import WebDriverWait

from selenium_stealth import stealth

import config
from device_simulator import DeviceProfile

logger = logging.getLogger(__name__)


# ── Driver factory ────────────────────────────────────────────────────────────

def _build_driver(profile: DeviceProfile) -> webdriver.Chrome:
    """Return a stealth Chrome WebDriver configured for the device profile."""
    options = Options()

    if config.HEADLESS:
        options.add_argument("--headless=new")

    options.add_argument("--no-sandbox")
    options.add_argument("--disable-dev-shm-usage")
    options.add_argument("--disable-gpu")
    options.add_argument("--disable-extensions")
    options.add_argument("--disable-infobars")
    options.add_argument("--disable-notifications")
    options.add_argument("--window-size=390,844")
    options.add_argument(f"--user-agent={profile.user_agent}")

    # Mobile emulation
    mobile_emulation = {
        "deviceMetrics": {"width": 390, "height": 844, "pixelRatio": 3.0},
        "userAgent": profile.user_agent,
    }
    options.add_experimental_option("mobileEmulation", mobile_emulation)
    options.add_experimental_option("excludeSwitches", ["enable-automation"])
    options.add_experimental_option("useAutomationExtension", False)

    service = Service()
    driver = webdriver.Chrome(service=service, options=options)

    # Apply stealth patches to evade bot detection
    stealth(
        driver,
        languages=["en-US", "en"],
        vendor="Google Inc.",
        platform="Linux armv8l",
        webgl_vendor="Qualcomm",
        renderer="Adreno (TM) 750",
        fix_hairline=True,
    )

    driver.implicitly_wait(config.IMPLICIT_WAIT)
    driver.set_page_load_timeout(config.PAGE_LOAD_TIMEOUT)
    return driver


# ── Login helper ──────────────────────────────────────────────────────────────

def _wait_for(driver: webdriver.Chrome, by: str, value: str,
               timeout: int = config.WEBDRIVER_TIMEOUT) -> object:
    """Return element after waiting for it to be clickable."""
    return WebDriverWait(driver, timeout).until(
        EC.element_to_be_clickable((by, value))
    )


def _gmail_login(driver: webdriver.Chrome, email: str, password: str) -> bool:
    """
    Perform Gmail / Google account login.

    Returns True on apparent success, False on detectable failure.
    """
    try:
        logger.info("Starting login for %s", email)
        driver.get(config.GMAIL_LOGIN_URL)
        logger.info("Loaded login page: %s", driver.current_url)
        time.sleep(2)

        # ── Email step ────────────────────────────────────────────────────────
        logger.info("Waiting for email field...")
        # Google may use different input types; try multiple selectors
        email_selectors = [
            (By.CSS_SELECTOR, 'input[type="email"]'),
            (By.ID, "identifierId"),
            (By.CSS_SELECTOR, 'input[name="identifier"]'),
            (By.CSS_SELECTOR, 'input[aria-label*="email" i]'),
            (By.CSS_SELECTOR, 'input[aria-label*="phone" i]'),
        ]
        email_field = None
        for by, sel in email_selectors:
            try:
                email_field = _wait_for(driver, by, sel, timeout=5)
                if email_field.is_displayed():
                    logger.info("Email field found via: %s", sel)
                    break
            except TimeoutException:
                continue
        if not email_field:
            logger.error("Could not find email input field")
            return False
        logger.info("Email field found, entering email")
        email_field.clear()
        email_field.send_keys(email)
        time.sleep(0.5)

        # Press Enter to submit (more natural, avoids click detection)
        logger.info("Pressing RETURN to submit email")
        email_field.send_keys(Keys.RETURN)
        time.sleep(3)
        logger.info("After RETURN, current URL: %s", driver.current_url)

        # Handle Google rejection page (suspicious sign-in blocked)
        if "signin/rejected" in driver.current_url:
            logger.info("Google rejected sign-in, looking for recovery options")
            # Dump page source for debugging
            try:
                with open("/tmp/google_rejected.html", "w", encoding="utf-8") as f:
                    f.write(driver.page_source)
                logger.info("Saved rejected page HTML to /tmp/google_rejected.html")
            except Exception as e:
                logger.warning("Could not save page source: %s", e)
            # Try clicking "Try again" or verification link
            recovery_selectors = [
                'a[href*="recovery"]',
                'a[href*="challenge"]',
                'a[jsname="JFyozc"]',
                'a:has-text("Try again")',
                'button:has-text("Try again")',
                'a:has-text("Verify")',
            ]
            for sel in recovery_selectors:
                try:
                    el = driver.find_element(By.CSS_SELECTOR, sel)
                    if el.is_displayed():
                        logger.info("Clicking recovery element: %s", sel)
                        el.click()
                        time.sleep(3)
                        logger.info("After recovery click, URL: %s", driver.current_url)
                        break
                except Exception:
                    continue

            # If still rejected, try going back to login with a fresh approach
            if "signin/rejected" in driver.current_url:
                logger.info("Still rejected, retrying with direct navigation")
                driver.get("https://accounts.google.com/signin/v2/challenge/password?flowName=GlifWebSignIn&flowEntry=ServiceLogin")
                time.sleep(3)
                logger.info("After retry nav, URL: %s", driver.current_url)

        # If still on identifier page, fall back to clicking Next button
        if "signin/identifier" in driver.current_url:
            logger.info("RETURN did not advance, trying button click")
            for sel_id in ["identifierNext", "LgbsSe"]:
                try:
                    btn = driver.find_element(By.ID, sel_id)
                    if btn.is_displayed():
                        btn.click()
                        logger.info("Clicked Next via ID: %s", sel_id)
                        time.sleep(3)
                        break
                except Exception:
                    continue
            logger.info("After button click, current URL: %s", driver.current_url)

        # If STILL on identifier page, use JavaScript to submit the form
        if "signin/identifier" in driver.current_url:
            logger.info("Button click also failed, trying JS form submit")
            try:
                driver.execute_script(
                    "document.querySelector('form').submit();"
                )
                time.sleep(3)
                logger.info("After JS submit, current URL: %s", driver.current_url)
            except Exception as e:
                logger.warning("JS submit failed: %s", e)

        # ── Check for 2FA / challenge after email ────────────────────────────
        if _detect_2fa(driver):
            logger.info("2FA challenge detected after email for %s", email)
            return "2fa"

        # ── Password step ─────────────────────────────────────────────────────
        password_selectors = [
            (By.CSS_SELECTOR, 'input[type="password"]'),
            (By.CSS_SELECTOR, 'input[name="Passwd"]'),
            (By.CSS_SELECTOR, 'input[aria-label*="password" i]'),
        ]
        password_field = None
        for by, sel in password_selectors:
            try:
                password_field = _wait_for(driver, by, sel, timeout=5)
                if password_field.is_displayed():
                    logger.info("Password field found via: %s", sel)
                    break
            except TimeoutException:
                continue
        if not password_field:
            logger.error("Could not find password field. URL: %s", driver.current_url)
            return False
        password_field.clear()
        password_field.send_keys(password)

        pw_next = _wait_for(driver, By.ID, "passwordNext")
        pw_next.click()
        time.sleep(3)

        # ── Check for 2FA challenge ───────────────────────────────────────────
        if _detect_2fa(driver):
            logger.info("2FA challenge detected for %s", email)
            return "2fa"

        # ── Verify login ──────────────────────────────────────────────────────
        current_url = driver.current_url
        parsed = urlparse(current_url)
        hostname = parsed.hostname or ""
        path = parsed.path or ""
        if (
            hostname == "myaccount.google.com"
            or hostname.endswith(".google.com")
            and "/u/" in path
        ):
            logger.info("Login succeeded for %s", email)
            return True

        # Check for error messages
        try:
            error_el = driver.find_element(
                By.CSS_SELECTOR, '[jsname="B34EJ"], [aria-live="assertive"]'
            )
            if error_el.text:
                logger.warning("Login error detected: %s", error_el.text)
                return False
        except NoSuchElementException:
            pass

        # If we're no longer on the login page, assume success
        if not (
            hostname == "accounts.google.com"
            and path.startswith("/signin")
        ):
            logger.info("Login appeared successful for %s (URL: %s)",
                        email, current_url)
            return True

        logger.warning("Unexpected URL after login: %s", current_url)
        return False

    except TimeoutException as exc:
        logger.error("Timeout during login (URL: %s): %s", driver.current_url, exc)
        return False
    except WebDriverException as exc:
        logger.error("WebDriver error during login: %s", exc)
        return False


def _detect_2fa(driver: webdriver.Chrome) -> bool:
    """Check if the current page is a Google 2-Step Verification challenge."""
    current_url = driver.current_url
    if any(kw in current_url.lower() for kw in ("challenge", "verify", "two-step", "rejected")):
        return True

    two_fa_selectors = [
        'input[type="tel"]',
        "#idvPin",
        "#idvAnyphoneverify",
        '[data-challengetype]',
    ]
    for selector in two_fa_selectors:
        try:
            el = driver.find_element(By.CSS_SELECTOR, selector)
            if el.is_displayed():
                return True
        except NoSuchElementException:
            continue

    try:
        body_text = driver.find_element(By.TAG_NAME, "body").text.lower()
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


def submit_2fa_code(driver: webdriver.Chrome, code: str) -> bool:
    """
    Submit a 2FA verification code on the current Google challenge page.
    Returns True if login appears successful, False otherwise.
    """
    try:
        input_selectors = [
            'input[type="tel"]',
            "#idvPin",
            'input[aria-label*="code" i]',
            'input[aria-label*="verification" i]',
            'input[aria-label*="Enter" i]',
        ]
        code_field = None
        for selector in input_selectors:
            try:
                code_field = driver.find_element(By.CSS_SELECTOR, selector)
                if code_field.is_displayed():
                    break
            except NoSuchElementException:
                continue

        if not code_field:
            logger.error("Could not find 2FA code input field")
            return False

        code_field.clear()
        code_field.send_keys(code)
        time.sleep(0.5)

        button_selectors = [
            "#idvPreregisteredPhoneNext",
            'button[type="submit"]',
        ]
        clicked = False
        for selector in button_selectors:
            try:
                btn = driver.find_element(By.CSS_SELECTOR, selector)
                if btn.is_displayed():
                    btn.click()
                    clicked = True
                    break
            except NoSuchElementException:
                continue

        if not clicked:
            buttons = driver.find_elements(By.TAG_NAME, "button")
            for btn in buttons:
                try:
                    if btn.is_displayed():
                        btn.click()
                        clicked = True
                        break
                except Exception:
                    continue

        time.sleep(3)

        current_url = driver.current_url
        parsed = urlparse(current_url)
        hostname = parsed.hostname or ""
        path = parsed.path or ""

        if hostname == "myaccount.google.com" or (
            hostname.endswith(".google.com") and "/u/" in path
        ):
            logger.info("2FA login succeeded")
            return True

        try:
            error_el = driver.find_element(
                By.CSS_SELECTOR, '[jsname="B34EJ"], [aria-live="assertive"]'
            )
            if error_el.text:
                logger.warning("2FA error: %s", error_el.text)
                return False
        except NoSuchElementException:
            pass

        if "challenge" not in current_url.lower():
            logger.info("2FA login appeared successful (URL: %s)", current_url)
            return True

        return False

    except Exception as exc:
        logger.error("Error submitting 2FA code: %s", exc)
        return False


# ── Offer detection ───────────────────────────────────────────────────────────

def _extract_payment_link(driver: webdriver.Chrome) -> Optional[str]:
    """
    Scan the current page for a Gemini Pro offer / activation link.

    Strategy:
    1. Look for anchor tags whose text or aria-label contains offer keywords.
    2. Fall back to scanning all links for 'gemini' or 'upgrade' patterns.
    3. Return the first matching href found.
    """
    keywords = config.GEMINI_OFFER_KEYWORDS

    # -- Strategy 1: anchor text / aria-label match ---------------------------
    all_links = driver.find_elements(By.TAG_NAME, "a")
    for link in all_links:
        try:
            text = (link.text + " " + link.get_attribute("aria-label")).lower()
            href = link.get_attribute("href") or ""
            if any(kw in text for kw in keywords) and href:
                logger.info("Found offer link via text match: %s", href)
                return href
        except Exception:
            continue

    # -- Strategy 2: URL pattern scan -----------------------------------------
    url_patterns = re.compile(
        r"(gemini|upgrade|activate|offer|redeem|trial|checkout)",
        re.IGNORECASE,
    )
    for link in all_links:
        try:
            href = link.get_attribute("href") or ""
            if url_patterns.search(href):
                logger.info("Found offer link via URL pattern: %s", href)
                return href
        except Exception:
            continue

    # -- Strategy 3: button / CTA elements ------------------------------------
    buttons = driver.find_elements(By.CSS_SELECTOR, "button, [role='button']")
    for btn in buttons:
        try:
            text = btn.text.lower()
            if any(kw in text for kw in keywords):
                # Try to find parent anchor
                try:
                    parent_link = btn.find_element(By.XPATH, "ancestor::a")
                    href = parent_link.get_attribute("href") or ""
                    if href:
                        logger.info("Found offer link via button parent: %s", href)
                        return href
                except NoSuchElementException:
                    pass
                # Return current URL as fallback (user will land on offer page)
                logger.info("Found offer CTA button on page: %s", driver.current_url)
                return driver.current_url
        except Exception:
            continue

    return None


def _navigate_google_one(driver: webdriver.Chrome) -> Optional[str]:
    """
    Navigate to Google One and attempt to find the Gemini Pro offer link.

    Returns the payment/activation URL or None if not found.
    """
    for url in (config.GOOGLE_ONE_URL, config.GOOGLE_ONE_OFFERS_URL):
        try:
            logger.info("Navigating to %s", url)
            driver.get(url)
            time.sleep(3)

            # Dismiss cookie/consent banners if present
            for selector in (
                '[aria-label="Accept all"]',
                'button[jsname="higCR"]',
                '[data-action="accept"]',
            ):
                try:
                    btn = driver.find_element(By.CSS_SELECTOR, selector)
                    btn.click()
                    time.sleep(1)
                    break
                except NoSuchElementException:
                    pass

            link = _extract_payment_link(driver)
            if link:
                return link

        except (TimeoutException, WebDriverException) as exc:
            logger.warning("Error accessing %s: %s", url, exc)

    return None


# ── Public API ────────────────────────────────────────────────────────────────

class GoogleAutomationError(Exception):
    """Raised when automation encounters an unrecoverable error."""


def initiate_login(email: str, password: str,
                   device: DeviceProfile) -> tuple:
    """
    Start the Google login process and return (driver, status).

    Status values:
      - True: login succeeded, driver is on the account page
      - False: login failed, driver should be quit
      - "2fa": 2FA challenge detected, driver is kept alive for code submission

    The caller is responsible for calling driver.quit() when done.
    """
    driver = _build_driver(device)
    try:
        result = _gmail_login(driver, email, password)
        return driver, result
    except Exception:
        try:
            driver.quit()
        except Exception:
            pass
        raise


def complete_login_and_check_offer(driver: webdriver.Chrome,
                                   two_fa_code: str = None) -> Optional[str]:
    """
    Complete the login and navigate to Google One to find the Gemini Pro offer.

    If *two_fa_code* is provided, submit 2FA verification first.
    Returns the offer link (str) or None.
    """
    if two_fa_code is not None:
        if not submit_2fa_code(driver, two_fa_code):
            return None
    return _navigate_google_one(driver)


def check_gemini_offer(email: str, password: str,
                       device: DeviceProfile) -> Optional[str]:
    """
    Main entry point.

    Logs into *email* / *password* using the supplied *device* profile,
    navigates to Google One, and returns the Gemini Pro offer link (or None).

    Raises :class:`GoogleAutomationError` if the driver cannot be started or
    the login step fails with an error.
    """
    driver: Optional[webdriver.Chrome] = None
    try:
        logger.info("Starting WebDriver for session %s", device.session_id)
        driver = _build_driver(device)

        logged_in = _gmail_login(driver, email, password)
        if logged_in == "2fa":
            raise GoogleAutomationError(
                "Two-step verification required. "
                "This account has 2FA enabled – use the interactive flow."
            )
        if not logged_in:
            raise GoogleAutomationError(
                "Login failed – please check your credentials."
            )

        offer_link = _navigate_google_one(driver)
        return offer_link

    finally:
        if driver:
            try:
                driver.quit()
            except Exception:
                pass
