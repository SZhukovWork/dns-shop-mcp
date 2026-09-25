"""Tool registration and whole-tool flows over recorded answers (real client + parsers, fake transport)."""
import json

import anyio
import pytest
from mcp.server.mcpserver.exceptions import ToolError

from conftest import EKB, FAKE_PII, IPHONE, PHILIPS
from dns_shop_mcp import server

TOOLS = {"search_products", "get_product", "get_reviews", "get_price_history", "compare_products"}


def tools():
    return {t.name: t for t in anyio.run(server.mcp.list_tools)}


def test_tools_are_registered_read_only_with_descriptions():
    registered = tools()
    assert set(registered) == TOOLS
    for tool in registered.values():
        assert tool.annotations.read_only_hint is True
        assert tool.annotations.destructive_hint is False
        assert tool.description and len(tool.description) > 80


def test_enumerated_parameters():
    t = tools()
    search = t["search_products"].input_schema["properties"]
    assert set(search["sort"]["enum"]) == {"popular", "price_asc", "price_desc", "rating", "reviews", "newest", "discount"}
    assert set(search["availability"]["enum"]) == {"any", "on_sale", "in_stock", "today", "tomorrow", "later", "out_of_stock"}
    reviews = t["get_reviews"].input_schema["properties"]
    assert set(reviews["scope"]["enum"]) == {"product", "multicard"}
    assert set(reviews["sort"]["enum"]) == {"newest", "helpful", "best"}
    assert reviews["limit"]["maximum"] == 50
    assert t["compare_products"].input_schema["properties"]["products"]["maxItems"] == 24
    for tool in t.values():
        assert "city" in tool.input_schema["properties"]


def test_instructions_explain_prices_ratings_and_untrusted_text():
    text = server.INSTRUCTIONS
    for phrase in ("online_payment_discount_rub", "credit_monthly_rub", "not_sold", "this_product",
                   "showcase", "delivery_cost_rub", "never", "instructions", "code"):
        assert phrase in text


def test_error_reason_reaches_the_agent(monkeypatch):
    from dns_shop_mcp.client import RateLimited

    class Throttled:
        def resolve_city(self, city):
            raise RateLimited("DNS is rate limiting this IP (HTTP 429). Wait a few minutes")

    monkeypatch.setattr(server, "_client", Throttled())
    with pytest.raises(ToolError, match="rate limiting"):
        server.get_product("5482008")


def test_unexpected_error_is_a_tool_error_too(monkeypatch):
    class Broken:
        def resolve_city(self, city):
            raise KeyError("boom")

    monkeypatch.setattr(server, "_client", Broken())
    with pytest.raises(ToolError, match="Unexpected error"):
        server.get_price_history("5482008")


def run(tool, **kwargs):
    res = getattr(server, tool)(**kwargs)
    assert json.loads(res.content[0].text) == res.structured_content
    return res.structured_content


@pytest.fixture
def offline(make_client, monkeypatch):
    c = make_client()
    monkeypatch.setattr(server, "_client", c)
    return c


# ---- get_product ------------------------------------------------------------------------------

def test_get_product_full_card(offline):
    p = run("get_product", product="5482008")
    assert (p["code"], p["guid"], p["model"]) == ("5482008", PHILIPS, "Philips FC9734/01")
    assert p["seller"] == {"name": "DNS", "marketplace": False}
    price = p["price"]
    assert price["price_rub"] == 15999
    assert price["online_payment_discount_rub"] == 1600
    assert price["price_if_paid_online_estimate_rub"] == 14399
    assert "only when the order is paid online" in price["price_if_paid_online_note"]
    assert "guest" in price["price_label"] and "Екатеринбург" in price["price_label"]
    assert price["discounts"][0]["kind"] == "Скидка за способ оплаты"
    assert price["warranty_extensions_paid"][0]["total_months"] == 36
    avail = p["availability"]
    assert (avail["stores_with_any_unit"], avail["stores_with_packaged_units"], avail["showcase_only_stores"]) == (13, 7, 6)
    assert "delivery_cost_rub" in avail and avail["delivery_cost_rub"] is None
    assert "cart" in avail["delivery_cost_note"]
    assert len(p["stores"]) == 13
    assert p["rating"]["shown_on_card"]["value"] == 4.83 and p["rating"]["this_product"]["reviews"] == 677
    assert p["reliability"]["no_problems_pct"] == 99.85
    assert p["characteristics"]["Общие параметры"]["Модель"] == "Philips FC9734/01"
    assert p["price_history_summary"]["weeks"] == 24
    assert p["city"]["id"] == EKB and p["city"]["source"].startswith("default")
    assert "availability widget" in p["city"]["confirmed_by"]
    assert p["fetched_at"].endswith("+00:00")


def test_get_product_never_leaks_personal_data(offline):
    out = json.dumps(run("get_product", product="5482008"), ensure_ascii=False)
    assert not [pii for pii in FAKE_PII if pii in out]


def test_used_units_are_refused_with_a_reason(offline):
    with pytest.raises(ToolError, match="used/discounted"):
        server.get_product("https://www.dns-shop.ru/catalog/markdown/0b90bfec-b56c-11f1-94bb-0050569d9cb8/")


# ---- search ------------------------------------------------------------------------------------

def test_search_follows_dns_category_and_keeps_parameters(offline):
    r = run("search_products", query="пылесос", sort="price_asc", price_min=5000, price_max=20000)
    assert r["resolved_category"] == {"id": "17a8cd3716404e77", "name": "Бытовые напольные пылесосы",
                                      "source": "chosen by DNS"}
    assert r["total_found_in_category"] == 445 and r["has_more"] is True and r["returned"] == 24
    # the redirect drops the order and price window: they are sent again
    listing = [u for m, u in offline.fake.calls if "category=17a8cd3716404e77" in u and "/search/" in u][0]
    assert "order=price-asc" in listing and "price=5000-20000" in listing
    assert any(t["name"] == "Роботы-пылесосы" for t in r["other_categories"])
    prices = [i["price_rub"] for i in r["items"] if "price_rub" in i]
    assert prices == sorted(prices)
    assert any("not monotonic" in n for n in r["notes"])
    assert {i["position"] for i in r["items"]} == set(range(1, 25))
    midea = next(i for i in r["items"] if i["code"] == "8174410")
    assert midea["price_rub"] == 5499 and midea["price_before_discount_rub"] == 6799
    samsung = next(i for i in r["items"] if i["code"] == "5606097")
    assert samsung["rating"] == {"value": 4.78, "reviews": 2900, "scope": "card (may merge variants)",
                                 "reviews_approx": True, "reviews_label": "2.9k отзывов"}
    assert samsung["stores_with_any_unit"] == 23
    assert r["city"]["id"] == EKB


def test_search_brand_query_lists_brand_categories(offline):
    r = run("search_products", query="apple")
    assert r["items"] == [] and r["brand_page"]["brand"] == "apple"
    assert r["brand_page"]["categories"][0]["name"] == "Смартфоны"
    assert "category" in r["notes"][0]


def test_search_nothing_found(offline):
    r = run("search_products", query="фывапролджэ зщшгн")
    assert r["total_found_in_category"] == 0 and r["items"] == []


def test_search_code_query_points_to_the_product(offline):
    r = run("search_products", query="5482008")
    assert r["matched_product"]["hex16"] == "9d6245855873d582"


# ---- reviews, history, compare -------------------------------------------------------------------

def test_reviews_product_scope(offline):
    r = run("get_reviews", product=PHILIPS, limit=5)
    assert r["total_matching"] == 677 and r["stats"]["average"] == 4.83
    assert r["stats"]["scope"] == "this product"
    assert r["reviews"][0]["pinned_top_review"] is True
    assert any("pinned" in n for n in r["notes"])
    out = json.dumps(r, ensure_ascii=False)
    assert not [pii for pii in FAKE_PII if pii in out]


def test_reviews_card_scope_of_a_multicard(offline):
    r = run("get_reviews", product=IPHONE, scope="multicard")
    assert r["total_matching"] == 2268 and r["card_merges_several_models"] is True
    assert any(i.get("other_variant") for i in r["reviews"])


def test_reviews_star_filter(offline):
    r = run("get_reviews", product=PHILIPS, max_rating=2, limit=50)
    assert r["total_matching"] == 19 and "stats" not in r
    assert r["filters"] == {"min_rating": 1, "max_rating": 2}
    with pytest.raises(ToolError):
        server.get_reviews(PHILIPS, min_rating=4, max_rating=2)


def test_price_history_next_to_live_price(offline):
    r = run("get_price_history", product="5482008")
    assert r["current_price_rub"] == 15999 and len(r["points"]) == 24
    assert r["summary"]["min_rub"] == 14599 and r["range_text_on_site"] == "от 14 599 ₽ до 19 199 ₽"
    assert "city-specific is not confirmed" in r["note"]


def test_compare_marks_unknown_and_not_sold_products(offline):
    r = run("compare_products", products=["5482008", "99999999", "391235f1-fdb7-11ec-8fe5-00155d8ed20b"])
    rows = {row["input"]: row for row in r["items"]}
    assert rows["5482008"]["price_rub"] == 15999
    assert rows["5482008"]["rating_this_product"] == {"value": 4.83, "reviews": 677}
    assert rows["99999999"]["available"] is False
    dexp = rows["391235f1-fdb7-11ec-8fe5-00155d8ed20b"]
    assert "price_rub" not in dexp and dexp["not_sold"] is True and dexp["last_sale_price_rub"] == 2699
    assert r["city"]["id"] == EKB


def test_unknown_code_is_not_found(offline):
    with pytest.raises(ToolError, match="no product with code 99999999"):
        server.get_reviews("99999999")
    r = run("compare_products", products=["99999999"])
    assert r["items"] == [{"input": "99999999", "available": False,
                           "reason": "DNS does not know this product (no price block)"}]


def test_item_without_a_price_block_has_no_invented_price(make_client, monkeypatch):
    """Trap: a tile DNS gives no price for must not get a price (or a 0)."""
    from conftest import ajax_state, json_response, site_routes

    def without_samsung(fake, method, url, data=None, **kw):
        resp = ajax_state(fake, method, url, data=data, **kw)
        body = json.loads(resp.content)
        if "product-buy" in url:
            body["data"]["states"] = [s for s in body["data"]["states"] if "SC4581" not in s["data"].get("name", "")]
        return json_response(url, body)
    routes = [r for r in site_routes() if r[0] != ("POST", r"/ajax-state/")] + [(("POST", r"/ajax-state/"), without_samsung)]
    monkeypatch.setattr(server, "_client", make_client(routes))
    r = run("search_products", query="пылесос")
    samsung = next(i for i in r["items"] if i["code"] == "5606097")
    assert "price_rub" not in samsung and "no price block" in samsung["price_note"]
    assert any("price is unknown, not zero" in n for n in r["notes"])


def test_catalog_redirect_gets_the_order_back(make_client, monkeypatch):
    """Some queries are redirected to a catalog listing whose URL resets the order to 'popular'."""
    from conftest import SITE, html_page, json_response, redirect_only, site_routes
    catalog = f"{SITE}/catalog/17a8a01d16404e77/smartfony/?q=iphone&stock=now-today-tomorrow-later-out_of_stock&order=popular"
    def any_price(fake, method, url, data=None, **kw):  # synthetic prices: this test is about the URL
        body = json.loads(data["data"])
        return json_response(url, {"result": True, "data": {"states": [
            {"id": c["id"], "data": {"id": c["data"]["id"], "name": "test", "price": {"current": 1000}}}
            for c in body["containers"]]}})
    routes = [(("GET", r"/search/\?q=iphone"), redirect_only(catalog)),
              (("GET", r"/catalog/17a8a01d16404e77/smartfony/"), html_page("catalog_smartphones_apple.html")),
              (("POST", r"/ajax-state/product-buy/"), any_price)] + site_routes()
    c = make_client(routes)
    monkeypatch.setattr(server, "_client", c)
    r = run("search_products", query="iphone", sort="price_asc", with_availability=False)
    listing = [u for m, u in c.fake.calls if "/catalog/17a8a01d16404e77/" in u][0]
    assert "order=price-asc" in listing and "order=popular" not in listing and "q=iphone" in listing
    assert r["resolved_category"] == {"id": "17a8a01d16404e77", "name": "Смартфоны", "source": "chosen by DNS"}
    assert r["total_found_in_category"] == 257
