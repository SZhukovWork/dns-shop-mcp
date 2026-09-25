"""End-to-end check over the real MCP stdio transport against live dns-shop.ru (Yekaterinburg).

Deselected by default; run with:  pytest -m live
Needs a Russian IP without VPN (Qrator blocks foreign/datacenter addresses) and
about 30 requests to the store (~1.5 min at the default pace).
Live prices and stock change, so the checks are about labels and consistency,
not exact numbers.
"""
import json
import os
import re
import sys

import anyio
import pytest
from mcp import ClientSession
from mcp.client.stdio import StdioServerParameters, stdio_client

pytestmark = pytest.mark.live

EKB = "83878977-f329-11dd-9648-00151716f9f5"
PHILIPS = "5482008"   # Philips FC9734/01: online-payment discount, showcase samples in stores
IPHONE = "5491322"    # iPhone 16 128 GB black: a multi-card merging 33 variants
TOOLS = {"search_products", "get_product", "get_reviews", "get_price_history", "compare_products"}


async def _call_all(errlog):
    params = StdioServerParameters(command=sys.executable, args=["-m", "dns_shop_mcp"], env=dict(os.environ))
    async with stdio_client(params, errlog=errlog) as (read, write):
        async with ClientSession(read, write) as session:
            await session.initialize()
            tools = {t.name for t in (await session.list_tools()).tools}
            results = {}
            for name, args in [
                ("search_products", {"query": "пылесос", "sort": "price_asc", "price_min": 5000,
                                     "price_max": 15000, "availability": "on_sale"}),
                ("get_product", {"product": PHILIPS}),
                ("get_price_history", {"product": PHILIPS}),
                ("get_reviews", {"product": IPHONE, "scope": "multicard", "max_rating": 2, "limit": 5}),
                ("compare_products", {"products": [PHILIPS, IPHONE, "99999999"]}),
            ]:
                res = await session.call_tool(name, args)
                assert not res.is_error, (name, res.content)
                results[name] = res.structured_content or json.loads(res.content[0].text)
            return tools, results


def _ekb(block):
    assert block["id"] == EKB and block["name"] == "Екатеринбург", block


def test_all_tools_over_stdio(tmp_path):
    log_path = tmp_path / "server-stderr.log"
    with open(log_path, "w") as errlog:
        tools, r = anyio.run(_call_all, errlog)
    # The server logs one line per tool call: requests made, Qrator passes, seconds.
    for line in log_path.read_text().splitlines():
        m = re.search(r"(\w+: \d+ request\(s\) to DNS.*|Qrator challenge passed.*)", line)
        if m:
            print(m.group(1))
    assert tools == TOOLS

    search = r["search_products"]
    _ekb(search["city"])
    assert "availability widget" in search["city"]["confirmed_by"]
    assert search["resolved_category"]["id"] and search["total_found_in_category"] > 0
    priced = [i for i in search["items"] if i.get("price_rub")]
    assert priced, search
    prices = [i["price_rub"] for i in priced]
    assert prices == sorted(prices)
    assert min(prices) >= 5000 * 0.9 and max(prices) <= 15000 * 1.1
    assert all("not_sold" not in i for i in search["items"])  # on_sale filter

    product = r["get_product"]
    _ekb(product["city"])
    assert product["code"] == PHILIPS and product["model"]
    price = product["price"]
    if price.get("not_sold"):
        assert price.get("price_rub") is None
    else:
        assert price["price_rub"] > 0 and "guest" in price["price_label"]
        if price.get("online_payment_discount_rub"):
            assert price["price_if_paid_online_estimate_rub"] == price["price_rub"] - price["online_payment_discount_rub"]
            assert "only when" in price["price_if_paid_online_note"]
    avail = product["availability"]
    assert avail["delivery_cost_rub"] is None and avail["delivery_cost_note"]
    if "stores_with_packaged_units" in avail:
        assert avail["stores_with_packaged_units"] + avail["showcase_only_stores"] == avail["stores_with_any_unit"]
    assert product["rating"]["this_product"]["reviews"] >= 0
    assert "Модель" in json.dumps(product["characteristics"], ensure_ascii=False)

    history = r["get_price_history"]
    _ekb(history["city"])
    assert len(history["points"]) >= 2
    assert history["current_price_rub"] == price.get("price_rub")

    reviews = r["get_reviews"]
    assert reviews["card_merges_several_models"] is True and reviews["total_matching"] > 0
    assert reviews["reviews"] and all(x["rating"] <= 2 for x in reviews["reviews"])
    assert '"user"' not in json.dumps(reviews)

    compare = r["compare_products"]
    _ekb(compare["city"])
    rows = {row["input"]: row for row in compare["items"]}
    assert rows["99999999"]["available"] is False
    assert rows[IPHONE]["card_rating_merges_several_models"] is True
    assert rows[PHILIPS].get("price_rub") == price.get("price_rub")
