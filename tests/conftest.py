"""Offline test helpers: recorded dns-shop.ru answers and a fake HTTP transport that serves them."""
from __future__ import annotations

import json
import re
from pathlib import Path
from urllib.parse import quote, unquote, urlsplit

import pytest
import requests
from requests.cookies import RequestsCookieJar
from requests.structures import CaseInsensitiveDict

from dns_shop_mcp import qrator

FIXTURES = Path(__file__).parent / "fixtures"
EKB = "83878977-f329-11dd-9648-00151716f9f5"
MSK = "30b7c1f3-03fb-11dc-95ee-00151716f9f5"
PHILIPS = "9d624585-5873-11ef-9250-0050569d5825"
IPHONE = "7924fcf7-6f27-11ef-9271-0050569d0a4d"
SITE = "https://www.dns-shop.ru"
FAKE_PII = ("Фейков", "fake@example.com", "900 000-00", "Фейкоград", "FAKE-FINGERPRINT",
            "Фейковый", "fake.manager@example.com", "/profile/fake-user/")


def load(name: str):
    text = (FIXTURES / name).read_text()
    return json.loads(text) if name.endswith(".json") else text


def response(status: int, url: str, body: str | bytes = b"", headers: dict | None = None,
             history: list | None = None) -> requests.Response:
    r = requests.Response()
    r.status_code = status
    r.url = url
    r._content = body.encode() if isinstance(body, str) else body
    r.headers = CaseInsensitiveDict(headers or {})
    r.encoding = "utf-8"
    r.history = history or []
    return r


def json_response(url: str, data, status: int = 200) -> requests.Response:
    return response(status, url, json.dumps(data, ensure_ascii=False), {"Content-Type": "application/json"})


def city_cookie_value(guid: str, name: str) -> str:
    return quote("0" * 64 + 'a:2:{i:0;s:12:"current_path";i:1;s:90:"' +
                 json.dumps({"city": guid, "cityName": name, "method": "manual"}, ensure_ascii=False) + '";}')


class FakeHTTP:
    """Stands in for requests.Session: routes (method, regex) -> handler(fake, method, url, **kw)."""

    def __init__(self, routes: list | None = None, city_answer: str = EKB):
        self.cookies = RequestsCookieJar()
        self.headers: dict = {}
        self.proxies: dict = {}
        self.calls: list[tuple[str, str]] = []
        self.routes = routes or []
        self.city_answer = city_answer

    def request(self, method, url, headers=None, timeout=None, allow_redirects=True, **kw):
        self.calls.append((method, url))
        if method == "POST" and "/city-service/set-city/" in url:
            name = "Москва" if self.city_answer == MSK else "Екатеринбург"
            self.cookies.set("current_path", city_cookie_value(self.city_answer, name), domain=".dns-shop.ru", path="/")
            self.cookies.set("city_path", "ekaterinburg", domain=".dns-shop.ru", path="/")
            return json_response(url, self.city_answer)
        for (m, pattern), handler in self.routes:
            if m == method and re.search(pattern, unquote(url)):
                return handler(self, method, url, allow_redirects=allow_redirects, **kw)
        return response(404, url, "not found")

    def count(self, fragment: str) -> int:
        return sum(1 for _, u in self.calls if fragment in unquote(u))


def fake_mint() -> qrator.BrowserSession:
    return qrator.BrowserSession(user_agent="Mozilla/5.0 (X11; Linux x86_64) Chrome/140.0.0.0 Safari/537.36",
                                 jsid2="v2.fake-cookie-for-tests")


# ---- handlers built from fixtures -----------------------------------------------------

def html_page(fixture: str, final_url: str | None = None):
    def handler(fake, method, url, allow_redirects=True, **kw):
        body = load(fixture)
        return response(200, final_url or url, body, {"Content-Type": "text/html"})
    return handler


def redirect_then(fixture: str, final_url: str):
    """A 302 that requests would follow (allow_redirects=True) to `final_url`."""
    def handler(fake, method, url, allow_redirects=True, **kw):
        hop = response(302, url, b"", {"Location": final_url})
        if not allow_redirects:
            return hop
        return response(200, final_url, load(fixture), {"Content-Type": "text/html"}, history=[hop])
    return handler


def redirect_only(location: str):
    def handler(fake, method, url, allow_redirects=True, **kw):
        return response(302, url, b"", {"Location": location})
    return handler


def ajax_state(fake, method, url, allow_redirects=True, data=None, **kw):
    body = json.loads(data["data"])
    kind = body["type"]
    states = []
    if kind == "product-buy":
        table = {**load("search_pylesos_prices.json"), **load("product_buy_cards.json")}
        for c in body["containers"]:
            if c["data"]["id"] in table:
                states.append({"id": c["id"], "data": table[c["data"]["id"]]})
    elif kind == "avails-container":
        table = load("avails.json")
        for c in body["containers"]:
            if c["data"]["id"] in table:
                states.append({"id": c["id"], "data": {"html": table[c["data"]["id"]]}})
    elif kind == "product-warranty":
        for c in body["containers"]:
            if c["data"]["id"] == PHILIPS:
                states.append({"id": c["id"], "data": load("warranty_philips.json")})
    return json_response(url, {"result": True, "data": {"states": states}, "message": "", "errors": []})


def json_fixture(name: str):
    def handler(fake, method, url, allow_redirects=True, **kw):
        return json_response(url, load(name))
    return handler


def opinions(fake, method, url, allow_redirects=True, files=None, **kw):
    form: dict = {}
    for k, (_, v) in files:
        form.setdefault(k, []).append(v)
    guid = form["objectId"][0]
    own = form["onlyObjectOpinions"][0] == "1"
    if "grades[]" in form:
        name = "opinions_philips_1_2_stars.json"
    elif guid == PHILIPS:
        name = "opinions_philips_product.json"
    elif guid == IPHONE:
        name = "opinions_iphone_product.json" if own else "opinions_iphone_card.json"
    else:
        return json_response(url, {"data": {"opinions": [], "totalCount": 0, "averageRating": 0,
                                            "hasSeveralModels": False, "grades": []}})
    return json_response(url, load(name))


PHILIPS_URL = f"{SITE}/product/9d6245855873d582/pylesos-philips-fc973401-fioletovyj/"
SEARCH_REDIRECT = f"{SITE}/search/?q=%D0%BF%D1%8B%D0%BB%D0%B5%D1%81%D0%BE%D1%81&category=17a8cd3716404e77"


def site_routes() -> list:
    return [
        (("GET", r"/search/\?q=5482008$"), redirect_then("product_philips.html", PHILIPS_URL)),
        (("GET", r"/search/\?q=пылесос(?!.*category)"), redirect_only(SEARCH_REDIRECT)),
        (("GET", r"/search/\?q=пылесос&category=17a8cd3716404e77"), html_page("search_pylesos.html")),
        (("GET", r"/search/\?q=apple$"), redirect_only(f"{SITE}/brand/apple/")),
        (("GET", r"/brand/apple/$"), html_page("brand_apple.html")),
        (("GET", r"/search/\?q=фывапролджэ"), lambda f, m, u, **k: response(210, u, load("search_nothing_found.html"))),
        (("GET", r"/product/9d6245855873d582/"), html_page("product_philips.html", PHILIPS_URL)),
        (("POST", r"/ajax-state/"), ajax_state),
        (("GET", r"/product/microdata/" + PHILIPS), json_fixture("microdata_philips.json")),
        (("GET", r"/pwa/pwa/get-product/\?id=" + PHILIPS), json_fixture("pwa_philips.json")),
        (("POST", r"get-min-price-discounts/\?id=" + PHILIPS), json_fixture("discounts_philips.json")),
        (("POST", r"get-product-price-history"), json_fixture("history_philips.json")),
        (("GET", r"/product/service-rating/9d6245855873d582/"), json_fixture("service_rating_philips.json")),
        (("POST", r"/pickup/v1/shops-avail-by-partition"), json_fixture("shops_philips.json")),
        (("POST", r"restapi\.dns-shop\.ru/v1/opinion/get-product-opinions"), opinions),
        (("GET", r"/catalog/search/filters/"), json_fixture("search_filters_pylesos.json")),
    ]


@pytest.fixture
def isolated(tmp_path, monkeypatch):
    """No real cache dir, no env leaking into tests."""
    for var in ("DNS_CITY", "DNS_MIN_INTERVAL", "DNS_PROXY", "DNS_HEADLESS"):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setenv("DNS_CACHE_DIR", str(tmp_path / "cache"))
    return tmp_path


@pytest.fixture
def make_client(isolated):
    from dns_shop_mcp.client import DnsClient

    def build(routes=None, city_answer=EKB):
        http = FakeHTTP(site_routes() if routes is None else routes, city_answer=city_answer)
        sleeps: list[float] = []
        mints: list[int] = []

        def minter():
            mints.append(1)
            return fake_mint()
        client = DnsClient(http=http, minter=minter, sleep=sleeps.append, state_dir=isolated / "state")
        client.fake, client.sleeps, client.mint_calls = http, sleeps, mints
        return client
    (isolated / "state").mkdir(exist_ok=True)
    return build


def host(url: str) -> str:
    return urlsplit(url).hostname or ""
