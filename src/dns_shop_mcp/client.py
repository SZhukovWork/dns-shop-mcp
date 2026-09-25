"""HTTP client for dns-shop.ru.

Access model (see ``qrator``): a short headless-Chromium visit yields the
``qrator_jsid2`` cookie, after that everything is plain ``requests`` with that
cookie and a browser User-Agent:

- SSR pages (search, catalog, product card) — tiles, breadcrumbs, the CSRF
  token and the ``AjaxState`` registry (per-type hashes);
- ``POST /ajax-state/<type>/`` — prices, availability summary, warranty;
- JSON endpoints on www (microdata, characteristics, price history, discounts,
  stores) and on restapi.dns-shop.ru (reviews, city list).

Policies:
- calls are serialised (one lock) and spaced ``DNS_MIN_INTERVAL`` seconds
  apart (default 2 s, never below 1.5 s);
- a Qrator challenge (401) or a 403 gets exactly one fresh browser visit and
  one repeat; 429 gets one pause and one repeat; then a clear error;
- the city is set explicitly (signed ``current_path`` cookie, cached per city)
  and every answer that echoes a city is checked against it;
- answers are sanity-checked (empty AjaxState, wrong product, wrong city,
  unparsable body): one repeat, then an error instead of data.
"""
from __future__ import annotations

import json
import logging
import os
import random
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable
from urllib.parse import quote, urlsplit

import requests

from . import parse, qrator

log = logging.getLogger(__name__)

SITE = "https://www.dns-shop.ru"
RESTAPI = "https://restapi.dns-shop.ru"
SITE_ID = "8c2e120b-8732-48a7-8178-0e04d47962d8"  # dns-shop.ru (sent by the site itself)
OPINION_OBJECT_TYPE = "610d4c8e-37fc-416c-a603-bce518d57c15"  # "product" in the reviews API
DEFAULT_CITY = {"id": "83878977-f329-11dd-9648-00151716f9f5", "name": "Екатеринбург",
                "slug": "ekaterinburg", "region": "Свердловская область"}
KNOWN_CITIES = [
    DEFAULT_CITY,
    {"id": "30b7c1f3-03fb-11dc-95ee-00151716f9f5", "name": "Москва", "slug": "moscow", "region": "Город Москва"},
]

IDLE_REMINT = 15 * 60       # qrator_jsid2 has Max-Age=1000 s and is renewed by every answer
TIMEOUT = 30
RATE_LIMIT_PAUSE = 20.0
CITY_LIST_TTL = 24 * 3600
MIN_INTERVAL_FLOOR = 1.5


class DnsError(RuntimeError):
    """Anything that stops a tool from returning trustworthy data."""


class Blocked(DnsError):
    pass


class RateLimited(DnsError):
    pass


class NotFound(DnsError):
    pass


class CityMismatch(DnsError):
    pass


class Implausible(DnsError):
    pass


def cache_dir() -> Path:
    root = os.environ.get("DNS_CACHE_DIR") or os.path.join(
        os.environ.get("XDG_CACHE_HOME") or os.path.expanduser("~/.cache"), "dns-shop-mcp"
    )
    path = Path(root)
    path.mkdir(parents=True, exist_ok=True)
    try:
        path.chmod(0o700)
    except OSError:
        pass
    return path


@dataclass
class Page:
    url: str
    status: int
    html: str
    registry: dict = field(default_factory=dict)
    city_guid: str | None = None


def _min_interval() -> float:
    try:
        value = float(os.environ.get("DNS_MIN_INTERVAL", "2.0"))
    except ValueError:
        value = 2.0
    return max(MIN_INTERVAL_FLOOR, value)


class DnsClient:
    def __init__(self, http: requests.Session | None = None,
                 minter: Callable[[], qrator.BrowserSession] | None = None,
                 sleep: Callable[[float], None] = time.sleep,
                 state_dir: Path | None = None) -> None:
        self.lock = threading.RLock()
        self._proxy = os.environ.get("DNS_PROXY") or None
        self._headless = os.environ.get("DNS_HEADLESS", "1") != "0"
        self._min_interval = _min_interval()
        self._sleep = sleep
        self._minter = minter or (lambda: qrator.mint(proxy=self._proxy, headless=self._headless))
        self._dir = state_dir or cache_dir()
        self._state_file = self._dir / "session.json"
        self._http = http or requests.Session()
        if self._proxy and http is None:
            self._http.proxies.update({"http": self._proxy, "https": self._proxy})
        self._last_call = 0.0
        self.requests_made = 0
        self.mints = 0
        self._state = self._load_state()
        self._restore_cookies()
        self._city: str | None = None     # city GUID the cookie jar currently carries
        self._cities: list[dict] | None = None

    # ---- persisted session ----------------------------------------------------

    def _load_state(self) -> dict:
        try:
            data = json.loads(self._state_file.read_text())
            if isinstance(data, dict):
                return data
        except (OSError, ValueError):
            pass
        return {}

    def _save_state(self) -> None:
        self._state["cookies"] = [
            {"name": c.name, "value": c.value, "domain": c.domain, "path": c.path}
            for c in self._http.cookies if c.domain.endswith("dns-shop.ru") and c.name not in ("current_path", "city_path")
        ]
        try:
            tmp = self._state_file.with_suffix(".tmp")
            tmp.write_text(json.dumps(self._state))
            tmp.chmod(0o600)
            tmp.replace(self._state_file)
        except OSError as e:
            log.warning("Could not persist the DNS session: %s", e)

    def _restore_cookies(self) -> None:
        for c in self._state.get("cookies") or []:
            self._http.cookies.set(c["name"], c["value"], domain=c["domain"], path=c.get("path") or "/")
        if self._state.get("user_agent"):
            self._http.headers["User-Agent"] = self._state["user_agent"]
        self._http.headers.setdefault("Accept-Language", "ru-RU,ru;q=0.9")

    def _session_alive(self) -> bool:
        if not self._state.get("user_agent") or not self._http.cookies.get(qrator.COOKIE, domain=".dns-shop.ru"):
            return False
        return time.time() - float(self._state.get("last_used") or 0) < IDLE_REMINT

    def _mint(self) -> None:
        log.info("Passing DNS's Qrator check in headless Chromium")
        try:
            browser = self._minter()
        except qrator.QratorError as e:
            raise Blocked(str(e)) from e
        self.mints += 1
        self._state["user_agent"] = browser.user_agent
        self._state["minted_at"] = browser.minted_at
        self._state["last_used"] = time.time()
        self._http.headers["User-Agent"] = browser.user_agent
        self._http.cookies.set(qrator.COOKIE, browser.jsid2, domain=".dns-shop.ru", path="/")
        self._save_state()

    def _pace(self) -> None:
        wait = self._min_interval + random.uniform(0, 0.4) - (time.monotonic() - self._last_call)
        if wait > 0:
            self._sleep(wait)

    # ---- one HTTP exchange -------------------------------------------------------

    def send(self, method: str, url: str, *, headers: dict | None = None,
             allow_redirects: bool = True, **kwargs: Any) -> requests.Response:
        """A paced request with Qrator/403/429/5xx handling (one recovery each), no content checks."""
        with self.lock:
            if not self._session_alive():
                self._mint()
            reminted = throttled = failed = False
            path = urlsplit(url).path
            while True:
                self._pace()
                try:
                    resp = self._http.request(method, url, headers=headers, timeout=TIMEOUT,
                                              allow_redirects=allow_redirects, **kwargs)
                except requests.RequestException as e:
                    if failed:
                        raise DnsError(f"Network error talking to DNS ({path}): {e}") from e
                    failed = True
                    log.warning("Network error on %s, retrying once: %s", path, e)
                    self._sleep(3)
                    continue
                finally:
                    self._last_call = time.monotonic()
                    self.requests_made += 1
                self.requests_made += len(resp.history)  # followed redirects are requests too
                status = resp.status_code
                log.debug("%s %s -> %s (%d B)", method, path, status, len(resp.content or b""))
                if _is_challenge(resp):
                    if reminted:
                        raise Blocked(
                            "DNS's anti-bot (Qrator) keeps challenging even a freshly issued cookie. "
                            "Usually the IP is blocked (VPN, foreign or datacenter address) or the "
                            "challenge changed. Switch the VPN off / use a Russian residential IP."
                        )
                    log.info("Qrator challenge on %s, passing it again", path)
                    self._mint()
                    reminted = True
                    continue
                if status == 403:
                    if reminted:
                        raise Blocked(
                            f"DNS answered HTTP 403 for {path} even after a fresh anti-bot cookie. "
                            "Qrator blocks this client: IP (VPN, foreign, datacenter) or User-Agent. "
                            "Wait, switch the VPN off or set DNS_PROXY; do not retry in a loop."
                        )
                    log.info("HTTP 403 on %s, passing Qrator again once", path)
                    self._mint()
                    reminted = True
                    continue
                if status == 429:
                    if throttled:
                        raise RateLimited(
                            "DNS is rate limiting this IP (HTTP 429). Wait a few minutes before the "
                            "next request; retrying in a loop makes it worse."
                        )
                    throttled = True
                    self._sleep(RATE_LIMIT_PAUSE)
                    continue
                if status >= 500:
                    if failed:
                        raise DnsError(f"DNS answered HTTP {status} for {path}; try again later")
                    failed = True
                    self._sleep(3)
                    continue
                self._state["last_used"] = time.time()
                self._save_state()
                return resp

    def _json(self, resp: requests.Response) -> Any:
        try:
            return resp.json()
        except ValueError:
            return None

    def checked(self, fetch: Callable[[], Any], problem: Callable[[Any], str | None], what: str) -> Any:
        """Fetch, sanity-check, repeat once on a problem, then refuse to pass it on."""
        data = fetch()
        issue = problem(data)
        if issue is None:
            return data
        log.warning("Implausible DNS answer for %s (%s); repeating once", what, issue)
        self._sleep(2)
        data = fetch()
        issue = problem(data)
        if issue is None:
            return data
        raise Implausible(f"DNS returned an implausible answer for {what} ({issue}) twice; "
                          "not passing it on as data. Try again later.")

    # ---- city ------------------------------------------------------------------------

    def cities(self) -> list[dict]:
        """DNS's city directory (cached on disk for a day)."""
        if self._cities is not None:
            return self._cities
        cache = self._dir / "cities.json"
        try:
            if time.time() - cache.stat().st_mtime < CITY_LIST_TTL:
                self._cities = json.loads(cache.read_text())
                return self._cities
        except (OSError, ValueError):
            pass
        resp = self.send("GET", f"{RESTAPI}/v1/get-city-list", headers=self._rest_headers())
        cities = parse.city_list(self._json(resp))
        if len(cities) < 100:
            raise Implausible("DNS's city list came back empty or truncated")
        self._cities = cities
        try:
            cache.write_text(json.dumps(cities, ensure_ascii=False))
        except OSError:
            pass
        return cities

    def resolve_city(self, query: str | None) -> tuple[dict, str]:
        """City dict + where the choice came from."""
        if query:
            source, value = "parameter", query
        elif os.environ.get("DNS_CITY"):
            source, value = "DNS_CITY environment variable", os.environ["DNS_CITY"]
        else:
            return dict(DEFAULT_CITY), "default (Екатеринбург)"
        found = parse.find_city(KNOWN_CITIES, value) or parse.find_city(self.cities(), value)
        if not found:
            raise NotFound(f"DNS has no city matching {value!r}. Pass the city name as on dns-shop.ru "
                           "(e.g. 'Екатеринбург'), 'Name, Region' or the city GUID.")
        if len(found) > 1:
            options = "; ".join(f"{c['name']}, {c['region']} ({c['id']})" for c in found[:8])
            raise NotFound(f"Several DNS cities are called {value!r}: {options}. "
                           "Pass 'Name, Region' or the GUID.")
        return dict(found[0]), source

    def use_city(self, guid: str, force: bool = False) -> None:
        """Make the cookie jar carry this city (cached signed cookie, or set it on the site)."""
        with self.lock:
            if self._city == guid and not force:
                return
            cached = (self._state.get("city_cookies") or {}).get(guid)
            if cached and not force:
                for c in cached:
                    self._http.cookies.set(c["name"], c["value"], domain=c["domain"], path=c.get("path") or "/")
                self._city = guid
                return
            self.send("POST", f"{SITE}/city-service/set-city/", json={"cityId": guid},
                      headers={"X-Requested-With": "XMLHttpRequest", "Referer": SITE + "/"})
            info = parse.current_path_city(self._cookie("current_path"))
            if not info or (info.get("city") or "").lower() != guid:
                raise CityMismatch(
                    f"DNS did not switch the session to city {guid} (cookie says "
                    f"{(info or {}).get('cityName') or 'nothing'}); not returning prices for a wrong city."
                )
            keep = [{"name": c.name, "value": c.value, "domain": c.domain, "path": c.path}
                    for c in self._http.cookies if c.name in ("current_path", "city_path")]
            self._state.setdefault("city_cookies", {})[guid] = keep
            self._save_state()
            self._city = guid

    def _cookie(self, name: str) -> str | None:
        for c in self._http.cookies:
            if c.name == name:
                return c.value
        return None

    # ---- pages -------------------------------------------------------------------------

    def page(self, url: str, city: str, allow_redirects: bool = True) -> Page:
        """GET an SSR page for `city`; its city echo must match (one re-set of the city otherwise)."""
        with self.lock:
            for attempt in range(2):
                self.use_city(city, force=attempt > 0)
                resp = self.send("GET", url, allow_redirects=allow_redirects,
                                 headers={"Accept": "text/html,application/xhtml+xml"})
                page = self._page_from(resp)
                if page.status in (301, 302, 404, 210) or page.city_guid == city:
                    return page
                if page.city_guid is None and not parse.ajax_registry(page.html):
                    if attempt == 0:
                        log.warning("DNS page %s came back without its usual markup; repeating once", url)
                        self._sleep(2)
                        continue
                    raise Implausible(f"DNS page {urlsplit(url).path} came back without its usual markup "
                                      "(truncated or an error page)")
                log.warning("Page rendered for city %s instead of %s; setting the city again", page.city_guid, city)
            raise CityMismatch(
                f"DNS keeps rendering pages for city {page.city_guid} instead of {city}; "
                "not returning data for the wrong city."
            )

    def adopt_page(self, resp: requests.Response, city: str) -> Page:
        """Use an already fetched page; fetch it again only if it was rendered for another city."""
        with self.lock:
            page = self._page_from(resp)
            if page.city_guid == city:
                return page
            log.warning("Page rendered for city %s instead of %s; fetching it again", page.city_guid, city)
            return self.page(resp.url, city)

    def _page_from(self, resp: requests.Response) -> Page:
        html = resp.text if resp.status_code not in (301, 302) else ""
        page = Page(url=resp.url, status=resp.status_code, html=html)
        if html:
            page.registry = parse.ajax_registry(html)
            page.city_guid = parse.page_city_guid(html)
            token = parse.csrf_token(html)
            if token:
                self._state["csrf"] = token
                self._state["csrf_page"] = resp.url
            hashes = self._state.setdefault("hashes", {})
            for kind, reg in page.registry.items():
                if reg.get("hash"):
                    hashes[kind] = reg["hash"]
            if page.registry:
                self._state["hash_page"] = resp.url
            self._save_state()
        return page

    def search_redirect(self, query: str, category: str | None, params: dict) -> requests.Response:
        """The raw first answer of /search/ (not following redirects)."""
        return self.send("GET", self.search_url(query, category, params), allow_redirects=False,
                         headers={"Accept": "text/html,application/xhtml+xml"})

    @staticmethod
    def search_url(query: str, category: str | None, params: dict) -> str:
        url = f"{SITE}/search/?q={quote(query)}"
        if category:
            url += f"&category={quote(category)}"
        for key, value in params.items():
            if value not in (None, ""):
                url += f"&{key}={quote(str(value), safe='-')}"
        return url

    # ---- XHR helpers -------------------------------------------------------------------

    def _xhr_headers(self, referer: str | None) -> dict:
        return {
            "X-CSRF-Token": self._state.get("csrf") or "",
            "X-Requested-With": "XMLHttpRequest",
            "Accept": "application/json, text/javascript, */*; q=0.01",
            "Referer": referer or self._state.get("csrf_page") or SITE + "/",
        }

    def _rest_headers(self, referer: str | None = None) -> dict:
        h = self._xhr_headers(referer)
        h["Origin"] = SITE
        return h

    def _ensure_tokens(self, city: str, fallback_page: str | None) -> None:
        if self._state.get("csrf") and self._state.get("hashes"):
            return
        self.page(fallback_page or f"{SITE}/", city)

    def _site_json(self, method: str, url: str, city: str, referer: str | None,
                   fallback_page: str | None = None, **kwargs: Any) -> Any:
        """XHR to www.dns-shop.ru with CSRF; an expired token is refreshed once from a page."""
        with self.lock:
            self.use_city(city)
            self._ensure_tokens(city, fallback_page)
            resp = self.send(method, url, headers=self._xhr_headers(referer), **kwargs)
            if resp.status_code == 400 and "переданные данные" in (resp.text or ""):
                log.info("CSRF token rejected; refreshing it from a page")
                self.page(fallback_page or referer or f"{SITE}/", city)
                resp = self.send(method, url, headers=self._xhr_headers(referer), **kwargs)
            if resp.status_code == 404:
                return None
            if resp.status_code != 200:
                raise DnsError(f"DNS answered HTTP {resp.status_code} for {urlsplit(url).path}")
            return self._json(resp)

    # ---- AjaxState ---------------------------------------------------------------------

    def ajax_state(self, kind: str, containers: list[dict], city: str, referer: str | None,
                   hash_page: str | None) -> dict[str, dict]:
        """POST /ajax-state/<kind>/ -> {container id: state data}.

        The hash is bound to the state type, not to the containers, so one hash
        from any page serves arbitrary products. A stale or foreign hash is not
        an error on DNS's side: it answers `result: true` with no states — which
        must never be read as "no price". Then the hash is refreshed from a page
        and the call repeated once.
        """
        if not containers:
            return {}
        with self.lock:
            problem = "no answer"
            for attempt in range(2):
                self.use_city(city)
                hashes = self._state.get("hashes") or {}
                control = None
                if not hashes.get(kind) or not self._state.get("csrf") or attempt > 0:
                    page = self._token_page(kind, city, [hash_page, referer, self._state.get("hash_page")])
                    control = _control_container(page.registry[kind].get("containers") or [])
                body = {"type": kind, "hash": self._state["hashes"][kind],
                        "containers": containers + ([control] if control else [])}
                data = self._site_json("POST", f"{SITE}/ajax-state/{kind}/", city, referer, hash_page,
                                       data={"data": json.dumps(body, ensure_ascii=False)})
                if not isinstance(data, dict) or not data.get("result"):
                    problem = "result is not true" if isinstance(data, dict) else "body is not JSON"
                else:
                    states = {s.get("id"): s.get("data") or {}
                              for s in ((data.get("data") or {}).get("states")) or [] if isinstance(s, dict)}
                    control_answered = states.pop(_CONTROL_ID, None) is not None
                    if states:
                        return states
                    if control_answered:
                        # The page's own product answered with this fresh token, so the
                        # requested products are simply unknown to DNS.
                        return {}
                    problem = "empty state list"
                log.warning("AjaxState %s: %s (attempt %d)", kind, problem, attempt + 1)
            raise Implausible(
                f"DNS answered the '{kind}' request with {problem} twice (a stale page token). "
                "This is not 'no price' — DNS did not answer; try again in a minute."
            )

    def has_token(self, kind: str) -> bool:
        return bool((self._state.get("hashes") or {}).get(kind) and self._state.get("csrf"))

    def _token_page(self, kind: str, city: str, candidates: list[str | None]) -> Page:
        """The first page (of the given ones) that registers `kind`: it carries a fresh token."""
        tried = []
        for url in dict.fromkeys(u for u in candidates if u):
            page = self.page(url, city)
            if kind in page.registry:
                return page
            tried.append(urlsplit(url).path)
        raise DnsError(f"No DNS page offered a '{kind}' token (tried {', '.join(tried) or 'none'}); "
                       "cannot request it")

    def buy(self, ids: list[str], city: str, referer: str | None, hash_page: str | None,
            card: bool = False) -> dict[str, dict]:
        """Prices for product codes or GUIDs -> {requested id: product-buy state}.

        Products DNS does not know are simply absent from the answer.
        """
        wanted = list(dict.fromkeys(ids))
        params: Any = {"isCard": True} if card else []
        containers = [{"id": f"as-p{i}", "data": {"id": i_id, "params": params}} for i, i_id in enumerate(wanted)]
        states = self.ajax_state("product-buy", containers, city, referer, hash_page)
        return {wanted[int(cid[4:])]: data for cid, data in states.items()
                if isinstance(cid, str) and cid.startswith("as-p") and cid[4:].isdigit()}

    def avails(self, guids: list[str], city: str, referer: str | None, hash_page: str | None) -> dict[str, str]:
        """Availability widget HTML per product GUID (also echoes the city)."""
        wanted = list(dict.fromkeys(guids))
        containers = [{"id": f"as-a{i}", "data": {"id": g, "checkPrice": True, "checkNearestCity": True}}
                      for i, g in enumerate(wanted)]
        states = self.ajax_state("avails-container", containers, city, referer, hash_page)
        out = {}
        for cid, data in states.items():
            if isinstance(cid, str) and cid.startswith("as-a") and cid[4:].isdigit():
                out[wanted[int(cid[4:])]] = data.get("html") or ""
        for fragment in out.values():
            echoed = parse.avails_summary(fragment)["city_id"]
            if echoed and echoed != city:
                raise CityMismatch(f"DNS computed availability for city {echoed}, not {city}; "
                                   "not returning data for the wrong city.")
        return out

    def warranty(self, guid: str, city: str, referer: str, hash_page: str) -> dict | None:
        states = self.ajax_state("product-warranty", [{"id": "as-w0", "data": {"id": guid}}], city, referer, hash_page)
        return states.get("as-w0")

    # ---- JSON endpoints ------------------------------------------------------------------

    def microdata(self, guid: str, city: str, referer: str | None = None) -> dict:
        def fetch():
            return self._site_json("GET", f"{SITE}/product/microdata/{guid}/", city, referer)

        def problem(d):
            if d is None:
                return None  # 404: unknown product, handled by the caller
            if not isinstance(d, dict) or not d.get("result") or not isinstance(d.get("data"), dict):
                return "no schema.org payload"
            url = (d["data"].get("offers") or {}).get("url") or ""
            if parse.guid_to_hex16(guid) not in url:
                return "schema.org payload is for another product"
            return None
        return self.checked(fetch, problem, f"product {guid} (microdata)")

    def pwa_product(self, guid: str, city: str, referer: str | None = None) -> dict:
        def fetch():
            return self._site_json("GET", f"{SITE}/pwa/pwa/get-product/?id={guid}", city, referer)

        def problem(d):
            if not isinstance(d, dict) or not isinstance(d.get("data"), dict):
                return "no product payload"
            if (d["data"].get("guid") or "").lower() != guid:
                return "characteristics are for another product"
            return None
        return self.checked(fetch, problem, f"product {guid} (characteristics)")

    def min_price_discounts(self, guid: str, city: str, referer: str | None) -> dict:
        def fetch():
            return self._site_json("POST", f"{SITE}/catalog/product/get-min-price-discounts/?id={guid}", city, referer)
        return self.checked(fetch, lambda d: None if isinstance(d, dict) and d.get("result") else "no discount payload",
                             f"product {guid} (discounts)")

    def price_history(self, guid: str, city: str, referer: str | None) -> dict:
        def fetch():
            return self._site_json("POST", f"{SITE}/catalog/product/get-product-price-history/", city, referer,
                                   files={"productId": (None, guid)})
        return self.checked(fetch, lambda d: None if isinstance(d, dict) and d.get("result") else "no history payload",
                             f"product {guid} (price history)")

    def service_rating(self, guid: str, city: str, referer: str | None) -> dict | None:
        url = f"{SITE}/product/service-rating/{parse.guid_to_hex16(guid)}/x/"
        data = self._site_json("GET", url, city, referer)
        return data if isinstance(data, dict) and data.get("result") else None

    def multicard(self, code: str, city: str, referer: str | None) -> dict | None:
        data = self._site_json("GET", f"{SITE}/load-multicard/{code}/", city, referer)
        if not isinstance(data, dict) or not isinstance(data.get("data"), dict):
            return None
        codes = {str(p.get("id")) for p in data["data"].get("products") or []}
        if codes and str(code) not in codes:
            raise Implausible(f"DNS returned the variants of another product for {code}")
        return data

    def search_filters(self, query: str, category: str, city: str, referer: str) -> dict | None:
        url = f"{SITE}/catalog/search/filters/?q={quote(query)}&category={quote(category)}"
        data = self._site_json("GET", url, city, referer)
        # This endpoint answers `result: false` even with a complete filter panel.
        blocks = ((data or {}).get("data") or {}).get("blocks") if isinstance(data, dict) else None
        return data if isinstance(blocks, dict) else None

    def shops(self, guid: str, city: str, referer: str | None) -> dict:
        body = {"siteId": SITE_ID, "cityId": city, "products": [{"id": guid, "count": 1}],
                "limit": 100, "offset": 0, "dl": False}

        def fetch():
            with self.lock:
                self.use_city(city)
                resp = self.send("POST", f"{SITE}/pickup/v1/shops-avail-by-partition", json=body,
                                 headers={**self._xhr_headers(referer), "Content-Type": "application/json"})
                if resp.status_code != 200:
                    raise DnsError(f"DNS answered HTTP {resp.status_code} for the store availability")
                return self._json(resp)

        def problem(d):
            if not isinstance(d, dict) or "shops" not in d:
                return "no store list"
            foreign = {(s.get("cityId") or "").lower() for s in d.get("shops") or []} - {city}
            if foreign:
                return f"stores of another city ({', '.join(sorted(foreign))})"
            return None
        return self.checked(fetch, problem, f"product {guid} (stores)")

    def opinions(self, guid: str, city: str, *, offset: int, limit: int, own_only: bool,
                 sort: int = 0, real_buyers: bool = False, photos: bool = False,
                 grades: list[int] | None = None, search: str | None = None,
                 referer: str | None = None) -> dict:
        """Reviews and their statistics (restapi.dns-shop.ru)."""
        form: list[tuple[str, Any]] = [
            ("cityId", city), ("objectTypeId", OPINION_OBJECT_TYPE), ("objectId", guid),
            ("isRealBuyer", "1" if real_buyers else "0"), ("hasPhotos", "1" if photos else "0"),
            ("hasVideo", "0"), ("sort", str(sort)), ("offset", str(offset)), ("limit", str(limit)),
            ("onlyObjectOpinions", "1" if own_only else "0"),
            # The site asks for the statistics block (init=1) unless a star filter is set.
            ("init", "0" if grades else "1"),
        ]
        for g in grades or []:
            form.append(("grades[]", str(g)))
        if search:
            form.append(("search", search))
        files = [(k, (None, v)) for k, v in form]

        def fetch():
            with self.lock:
                if not self._state.get("csrf"):
                    self._ensure_tokens(city, referer)
                resp = self.send("POST", f"{RESTAPI}/v1/opinion/get-product-opinions", files=files,
                                 headers=self._rest_headers(referer))
                if resp.status_code != 200:
                    raise DnsError(f"DNS reviews API answered HTTP {resp.status_code}")
                return self._json(resp)

        def problem(d):
            if not isinstance(d, dict) or not isinstance(d.get("data"), dict):
                return "no reviews payload"
            ops = d["data"].get("opinions")
            if not isinstance(ops, list):
                return "no review list"
            if own_only and any((o.get("productId") or "").lower() not in ("", guid) for o in ops):
                return "reviews of another product in a product-only request"
            return None
        return self.checked(fetch, problem, f"product {guid} (reviews)")


_CONTROL_ID = "as-control"


def _control_container(registered: list[dict]) -> dict | None:
    """The page's own first container, as a known-good control for a fresh token.

    The page registers `{"product": guid}` where the site's script sends `{"id": guid}`.
    """
    if not registered or not isinstance(registered[0], dict):
        return None
    data = dict(registered[0].get("data") or {})
    if "product" in data and "id" not in data:
        data["id"] = data.pop("product")
    return {"id": _CONTROL_ID, "data": data} if data.get("id") else None


def _is_challenge(resp: requests.Response) -> bool:
    if resp.status_code != 401:
        return False
    server = (resp.headers.get("server") or "").lower()
    body = resp.text[:5000] if resp.content else ""
    return "qrator" in server or "__qrator" in body or "qrator_jsr" in (resp.headers.get("set-cookie") or "")
