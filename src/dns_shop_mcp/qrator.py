"""Passing DNS's Qrator JavaScript challenge with a short headless-Chromium visit.

Every dynamic page of dns-shop.ru (and restapi.dns-shop.ru) answers a client
without the ``qrator_jsid2`` cookie with HTTP 401 and a proof-of-work script.
A real browser solves it in one to three seconds; after that plain HTTP with
that single cookie and a browser User-Agent is accepted (a ``python-requests``
User-Agent gets 403 even with a valid cookie).

So the browser is only a key cutter: it opens the home page with images,
fonts, media and third-party hosts blocked, waits for ``qrator_jsid2`` and is
closed again. The cookie lives 1000 s (``Max-Age``) and every answer of the
site renews it, so an active session keeps it; after a longer pause a new visit
is needed.

The old headless shell (Playwright's default) passes the challenge and is the
lightest; the "new" headless mode (``channel="chromium"``) is tried once if the
shell fails.
"""
from __future__ import annotations

import logging
import subprocess
import sys
import time
from dataclasses import dataclass, field
from urllib.parse import unquote, urlparse, urlsplit

log = logging.getLogger(__name__)

HOME_URL = "https://www.dns-shop.ru/"
COOKIE = "qrator_jsid2"
_BLOCKED_TYPES = ("image", "media", "font")


class QratorError(RuntimeError):
    """The browser could not obtain the Qrator cookie (blocked IP, no browser, changed challenge)."""


@dataclass
class BrowserSession:
    user_agent: str
    jsid2: str
    minted_at: float = field(default_factory=time.time)


def proxy_settings(url: str | None) -> dict | None:
    """DNS_PROXY URL -> Playwright proxy dict (Chromium wants credentials separately)."""
    if not url:
        return None
    parsed = urlparse(url if "://" in url else "http://" + url)
    server = f"{parsed.scheme}://{parsed.hostname}" + (f":{parsed.port}" if parsed.port else "")
    out: dict[str, str] = {"server": server}
    if parsed.username:
        out["username"] = unquote(parsed.username)
        out["password"] = unquote(parsed.password or "")
    return out


def mint(proxy: str | None = None, headless: bool = True, timeout: float = 45.0) -> BrowserSession:
    """Open the home page in Chromium until Qrator issues ``qrator_jsid2``."""
    errors: list[str] = []
    for channel in (None, "chromium"):  # headless shell first, then the new headless mode
        try:
            return _mint_with_install(proxy, headless, timeout, channel)
        except QratorError as e:
            errors.append(f"{channel or 'headless shell'}: {e}")
            log.warning("Qrator challenge not passed with %s: %s", channel or "headless shell", e)
    raise QratorError(
        "DNS (Qrator) did not let the browser through: " + "; ".join(errors) + ". "
        "Qrator blocks foreign, VPN and datacenter IPs — switch the VPN off, use a Russian "
        "residential IP or set DNS_PROXY. If this persists from a home IP, the challenge may "
        "have changed."
    )


class _BrowserMissing(Exception):
    pass


def _mint_with_install(proxy, headless, timeout, channel) -> BrowserSession:
    try:
        return _mint(proxy, headless, timeout, channel)
    except _BrowserMissing:
        _install_chromium()
        return _mint(proxy, headless, timeout, channel)


def _mint(proxy: str | None, headless: bool, timeout: float, channel: str | None) -> BrowserSession:
    from playwright.sync_api import Error as PlaywrightError
    from playwright.sync_api import sync_playwright

    started = time.monotonic()
    with sync_playwright() as pw:
        kwargs: dict = {
            "headless": headless,
            "proxy": proxy_settings(proxy),
            "args": ["--disable-blink-features=AutomationControlled"],
        }
        if channel:
            kwargs["channel"] = channel
        try:
            browser = pw.chromium.launch(**kwargs)
        except PlaywrightError as e:
            if "Executable doesn't exist" in str(e):
                raise _BrowserMissing() from e
            raise QratorError(f"could not start Chromium: {e}") from e
        try:
            probe = browser.new_page()
            user_agent = probe.evaluate("navigator.userAgent").replace("HeadlessChrome", "Chrome")
            probe.close()
            ctx = browser.new_context(user_agent=user_agent, locale="ru-RU",
                                      viewport={"width": 1366, "height": 850})
            # Keep the visit light: no pictures, fonts or media, nothing from other hosts.
            ctx.route("**/*", _light_route)
            page = ctx.new_page()
            try:
                page.goto(HOME_URL, wait_until="domcontentloaded", timeout=int(timeout * 1000))
            except PlaywrightError as e:
                # The challenge reloads the page by itself; a navigation race here
                # is harmless as long as the cookie appears below.
                log.debug("Home page navigation: %s", e)
            jsid2 = None
            while time.monotonic() - started < timeout:
                jsid2 = next((c["value"] for c in ctx.cookies() if c["name"] == COOKIE and c["value"]), None)
                if jsid2:
                    break
                page.wait_for_timeout(300)
        finally:
            browser.close()
    if not jsid2:
        raise QratorError(f"no {COOKIE} cookie within {timeout:.0f} s")
    log.info("Qrator challenge passed in %.1f s (%s)", time.monotonic() - started, channel or "headless shell")
    return BrowserSession(user_agent=user_agent, jsid2=jsid2)


def _light_route(route) -> None:
    request = route.request
    host = urlsplit(request.url).hostname or ""
    if request.resource_type in _BLOCKED_TYPES or not (host == "dns-shop.ru" or host.endswith(".dns-shop.ru")):
        route.abort()
    else:
        route.continue_()


def _install_chromium() -> None:
    """First run under uvx has no browser yet: fetch Playwright's Chromium.

    Output goes to stderr — stdout belongs to the MCP stdio transport.
    """
    log.warning("Playwright Chromium is not installed; installing it (one-time, ~150 MB download)")
    result = subprocess.run(
        [sys.executable, "-m", "playwright", "install", "chromium"],
        stdout=sys.stderr, stderr=sys.stderr, check=False,
    )
    if result.returncode != 0:
        raise QratorError(
            "Chromium is required to pass DNS's Qrator check and could not be installed "
            "automatically. Run: python -m playwright install chromium"
        )
