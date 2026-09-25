"""HTTP policies of the client against a fake transport: Qrator, one repeat, city checks, pacing."""
import json
import os
import stat
import time

import pytest

from conftest import EKB, MSK, PHILIPS, PHILIPS_URL, SITE, FakeHTTP, fake_mint, json_response, load, response, site_routes
from dns_shop_mcp import client as client_mod
from dns_shop_mcp.client import Blocked, CityMismatch, DnsClient, Implausible, RateLimited


def challenge(url):
    return response(401, url, load("qrator_challenge_401.html"),
                    {"Server": "QRATOR", "Set-Cookie": "qrator_jsr=x; Max-Age=300"})


def sequence(*answers):
    """A handler returning the given answers one after another (the last one repeats)."""
    state = {"i": 0}

    def handler(fake, method, url, allow_redirects=True, **kw):
        answer = answers[min(state["i"], len(answers) - 1)]
        state["i"] += 1
        return answer(url) if callable(answer) else answer
    return handler


def ok_json(url):
    return json_response(url, {"result": True, "data": {}})


def test_qrator_challenge_is_detected_in_the_recorded_answer():
    assert client_mod._is_challenge(challenge(SITE + "/"))
    assert not client_mod._is_challenge(response(401, SITE + "/x", "{}", {"Server": "nginx"}))


def test_first_request_mints_and_challenge_gets_exactly_one_new_pass(make_client):
    c = make_client([(("GET", r"/x"), sequence(challenge, ok_json))])
    assert c.send("GET", SITE + "/x").status_code == 200
    assert len(c.mint_calls) == 2  # initial session + one repeat after the challenge
    assert c.fake.count("/x") == 2


def test_challenge_twice_is_blocked_not_retried_in_a_loop(make_client):
    c = make_client([(("GET", r"/x"), sequence(challenge))])
    with pytest.raises(Blocked, match="VPN"):
        c.send("GET", SITE + "/x")
    assert c.fake.count("/x") == 2


def test_403_gets_one_recovery_then_blocked(make_client):
    c = make_client([(("GET", r"/x"), sequence(lambda u: response(403, u, "forbidden", {"Server": "QRATOR"})))])
    with pytest.raises(Blocked, match="403"):
        c.send("GET", SITE + "/x")
    assert c.fake.count("/x") == 2


def test_429_pauses_once_then_reports_rate_limit(make_client):
    c = make_client([(("GET", r"/x"), sequence(lambda u: response(429, u, "slow down")))])
    with pytest.raises(RateLimited, match="Wait a few minutes"):
        c.send("GET", SITE + "/x")
    assert c.fake.count("/x") == 2
    assert client_mod.RATE_LIMIT_PAUSE in c.sleeps


def test_429_then_ok(make_client):
    c = make_client([(("GET", r"/x"), sequence(lambda u: response(429, u, ""), ok_json))])
    assert c.send("GET", SITE + "/x").status_code == 200


def test_requests_are_paced(make_client, monkeypatch):
    monkeypatch.setenv("DNS_MIN_INTERVAL", "0.5")
    c = make_client([(("GET", r"/x"), sequence(ok_json))])
    assert c._min_interval == client_mod.MIN_INTERVAL_FLOOR  # never below 1.5 s
    c.send("GET", SITE + "/x")
    c.send("GET", SITE + "/x")
    assert c.sleeps and max(c.sleeps) >= 1.4


def test_session_is_stored_privately(make_client, isolated):
    c = make_client([(("GET", r"/x"), sequence(ok_json))])
    c.send("GET", SITE + "/x")
    f = isolated / "state" / "session.json"
    assert stat.S_IMODE(os.stat(f).st_mode) == 0o600
    saved = json.loads(f.read_text())
    assert saved["user_agent"].startswith("Mozilla/5.0") and any(k["name"] == "qrator_jsid2" for k in saved["cookies"])
    cache = client_mod.cache_dir()
    assert stat.S_IMODE(os.stat(cache).st_mode) == 0o700


def test_idle_session_is_renewed_before_use(make_client, isolated):
    c = make_client([(("GET", r"/x"), sequence(ok_json))])
    c.send("GET", SITE + "/x")
    assert len(c.mint_calls) == 1
    c._state["last_used"] = time.time() - client_mod.IDLE_REMINT - 5
    c.send("GET", SITE + "/x")
    assert len(c.mint_calls) == 2


# ---- city --------------------------------------------------------------------------------------

def test_city_is_set_explicitly_and_cached(make_client):
    c = make_client()
    c.use_city(EKB)
    c.use_city(EKB)
    assert c.fake.count("/city-service/set-city/") == 1
    http2 = FakeHTTP(site_routes())
    fresh = DnsClient(http=http2, minter=fake_mint, sleep=lambda s: None, state_dir=c._dir)
    fresh.use_city(EKB)  # the signed cookie is reused from the session file
    assert http2.count("/city-service/set-city/") == 0


def test_city_not_applied_is_an_error(make_client):
    c = make_client(city_answer=MSK)
    with pytest.raises(CityMismatch, match="did not switch"):
        c.use_city(EKB)


def test_page_for_another_city_is_refused(make_client):
    moscow_page = load("product_philips.html").replace(EKB, MSK)
    c = make_client([(("GET", r"/product/9d6245855873d582/"), lambda f, m, u, **k: response(200, u, moscow_page))])
    with pytest.raises(CityMismatch, match="instead of"):
        c.page(PHILIPS_URL, EKB)
    assert c.fake.count("/city-service/set-city/") == 2  # set, then re-set once


def test_availability_for_another_city_is_refused(make_client):
    html = load("avails.json")[PHILIPS].replace(EKB, MSK)

    def avails(fake, method, url, allow_redirects=True, data=None, **kw):
        body = json.loads(data["data"])
        return json_response(url, {"result": True, "data": {"states": [
            {"id": body["containers"][0]["id"], "data": {"html": html}}]}})
    c = make_client([(("GET", r"/product/9d6245855873d582/"), lambda f, m, u, **k: response(200, u, load("product_philips.html"))),
                     (("POST", r"/ajax-state/avails-container/"), avails)])
    with pytest.raises(CityMismatch, match="availability"):
        c.avails([PHILIPS], EKB, PHILIPS_URL, PHILIPS_URL)


# ---- AjaxState ------------------------------------------------------------------------------------

def test_empty_state_list_is_not_no_price(make_client):
    """Trap: a stale hash makes DNS answer result:true with no states. Refresh once, then error."""
    empty = lambda u: json_response(u, {"result": True, "data": {"states": []}})  # noqa: E731
    c = make_client([(("GET", r"/product/9d6245855873d582/"), lambda f, m, u, **k: response(200, u, load("product_philips.html"))),
                     (("POST", r"/ajax-state/product-buy/"), sequence(empty))])
    with pytest.raises(Implausible, match="not 'no price'"):
        c.buy(["5482008"], EKB, PHILIPS_URL, PHILIPS_URL)
    assert c.fake.count("/ajax-state/product-buy/") == 2
    assert c.fake.count("/product/9d6245855873d582/") == 2  # tokens from the page, then refreshed once


def test_empty_state_list_recovers_after_refresh(make_client):
    empty = lambda u: json_response(u, {"result": True, "data": {"states": []}})  # noqa: E731
    good = lambda u: json_response(u, {"result": True, "data": {"states": [  # noqa: E731
        {"id": "as-p0", "data": load("product_buy_cards.json")[PHILIPS]}]}})
    c = make_client([(("GET", r"/product/9d6245855873d582/"), lambda f, m, u, **k: response(200, u, load("product_philips.html"))),
                     (("POST", r"/ajax-state/product-buy/"), sequence(empty, good))])
    states = c.buy(["5482008"], EKB, PHILIPS_URL, PHILIPS_URL)
    assert states["5482008"]["price"]["current"] == 15999


def test_unknown_products_are_simply_absent(make_client):
    c = make_client()
    states = c.buy(["5482008", "99999999"], EKB, PHILIPS_URL, PHILIPS_URL)
    assert set(states) == {"5482008"}


def test_answer_about_another_product_is_repeated_once_then_refused(make_client):
    wrong = load("microdata_philips.json")
    wrong["data"]["offers"]["url"] = "https://www.dns-shop.ru/product/7924fcf76f27d0a4/x/"
    c = make_client([(("GET", r"/product/microdata/"), lambda f, m, u, **k: json_response(u, wrong))])
    c._state["csrf"], c._state["hashes"] = "t", {"product-buy": "h"}
    with pytest.raises(Implausible, match="twice"):
        c.microdata(PHILIPS, EKB)
    assert c.fake.count("/product/microdata/") == 2


def test_stores_of_another_city_are_refused(make_client):
    foreign = load("shops_philips.json")
    foreign["shops"][0]["cityId"] = MSK
    c = make_client([(("POST", r"/pickup/v1/"), lambda f, m, u, **k: json_response(u, foreign))])
    c._state["csrf"], c._state["hashes"] = "t", {"product-buy": "h"}
    with pytest.raises(Implausible, match="another city"):
        c.shops(PHILIPS, EKB, PHILIPS_URL)


def test_filter_panel_is_accepted_despite_result_false(make_client):
    """Quirk: /catalog/search/filters/ answers result:false with a complete panel."""
    c = make_client()
    c._state["csrf"], c._state["hashes"] = "t", {"product-buy": "h"}
    assert load("search_filters_pylesos.json")["result"] is False
    assert c.search_filters("пылесос", "17a8cd3716404e77", EKB, SITE + "/search/") is not None


# ---- cities ------------------------------------------------------------------------------------------

def test_city_resolution(make_client, monkeypatch):
    c = make_client([(("GET", r"restapi\.dns-shop\.ru/v1/get-city-list"),
                      lambda f, m, u, **k: json_response(u, _big_city_list()))])
    city, source = c.resolve_city(None)
    assert city["id"] == EKB and source.startswith("default")
    monkeypatch.setenv("DNS_CITY", "Москва")
    city, source = c.resolve_city(None)
    assert city["id"] == MSK and source == "DNS_CITY environment variable"
    assert c.fake.count("get-city-list") == 0  # the two big cities need no directory
    with pytest.raises(client_mod.NotFound, match="Several DNS cities"):
        c.resolve_city("Михайловка")
    assert c.resolve_city("Михайловка, Волгоградская")[0]["region"] == "Волгоградская область"
    with pytest.raises(client_mod.NotFound, match="no city"):
        c.resolve_city("Атлантида")


def _big_city_list():
    data = load("city_list_sample.json")
    filler = [{"id": f"00000000-0000-0000-0000-{i:012d}", "name": f"Город {i}", "regionId": 0,
               "citySlug": f"city{i}"} for i in range(120)]
    data["data"]["cities"] = data["data"]["cities"] + filler
    return data


def test_only_unknown_products_is_not_a_stale_token(make_client):
    """An empty answer for unknown codes is told apart from a stale token by a control product."""
    c = make_client()
    assert c.buy(["99999999"], EKB, PHILIPS_URL, PHILIPS_URL) == {}
    c._state["hashes"]["product-buy"] = "cached-token"
    assert c.buy(["99999998"], EKB, PHILIPS_URL, PHILIPS_URL) == {}
    assert c.fake.count("/ajax-state/product-buy/") == 3  # 1 + (cached try, then with a control)
