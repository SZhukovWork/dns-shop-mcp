"""Parsers on recorded dns-shop.ru answers. Each DNS pitfall has a test here or in test_server/test_client."""
import json
from datetime import date

import pytest

from conftest import EKB, IPHONE, PHILIPS, load
from dns_shop_mcp import parse


# ---- identifiers ------------------------------------------------------------------

@pytest.mark.parametrize("ref, expected", [
    ("5482008", ("code", "5482008")),
    ("#5482008", ("code", "5482008")),
    (PHILIPS, ("guid", PHILIPS)),
    (PHILIPS.upper(), ("guid", PHILIPS)),
    ("9d6245855873d582", ("hex16", "9d6245855873d582")),
    ("https://www.dns-shop.ru/product/9d6245855873d582/pylesos-philips-fc973401-fioletovyj/", ("hex16", "9d6245855873d582")),
    ("www.dns-shop.ru/product/9d6245855873d582/", ("hex16", "9d6245855873d582")),
    ("https://www.dns-shop.ru/product/opinion/9d6245855873d582/x/", ("hex16", "9d6245855873d582")),
    ("https://www.dns-shop.ru/catalog/markdown/0b90bfec-b56c-11f1-94bb-0050569d9cb8/", ("markdown", "0b90bfec-b56c-11f1-94bb-0050569d9cb8")),
])
def test_product_reference_kinds(ref, expected):
    assert parse.classify_product_ref(ref) == expected


def test_bad_reference_explains_what_to_pass():
    with pytest.raises(ValueError, match="Код товара"):
        parse.classify_product_ref("пылесос")


@pytest.mark.parametrize("guid, hex16", [
    (PHILIPS, "9d6245855873d582"),
    (IPHONE, "7924fcf76f27d0a4"),
    ("02e42f5b-94d8-4e73-b3de-3a9f8db84271", "02e42f5b94d88427"),
    ("391235f1-fdb7-11ec-8fe5-00155d8ed20b", "391235f1fdb7ed20"),
])
def test_url_id_is_derived_from_guid(guid, hex16):
    assert parse.guid_to_hex16(guid) == hex16


# ---- search listing -------------------------------------------------------------------

def test_search_tiles_codes_names_and_status():
    tiles = parse.listing_tiles(load("search_pylesos.html"))
    assert len(tiles) == 24
    first = tiles[0]
    assert first["code"] == "5606097" and first["guid"] == "ccac80f3-a655-11ef-929e-0050569d9cb8"
    assert first["name"] == "Пылесос Samsung SC4581 черный"
    assert first["short_specs"].startswith("[2000 Вт")
    assert first["url"].startswith("https://www.dns-shop.ru/product/ccac80f3a655d9cb/")
    assert {t["avail_status"] for t in tiles} <= {"now", "today", "tomorrow", "later", "out_of_stock"}
    assert all(t["seller"] is None for t in tiles)


def test_rounded_tile_review_count_is_flagged():
    """Trap: tiles show '2.9k отзывов' — an approximation, not a count."""
    tile = parse.listing_tiles(load("search_pylesos.html"))[0]
    assert tile["rating_value"] == 4.78 and tile["reviews_label"] == "2.9k отзывов"
    assert parse.reviews_label_count(tile["reviews_label"]) == (2900, True)
    assert parse.reviews_label_count("677 отзывов") == (677, False)
    assert parse.reviews_label_count("нет отзывов") == (0, False)
    assert parse.reviews_label_count(None) == (None, False)


def test_search_counts_only_inside_the_chosen_category():
    """Trap: 'Найдено 445 товаров в категории …' counts one category, not the whole query."""
    html = load("search_pylesos.html")
    assert parse.search_total(html) == 445
    assert parse.heading_category(parse.search_heading(html)) == "Бытовые напольные пылесосы"
    filters = parse.search_filters(load("search_filters_pylesos.json"))
    assert filters["selected_category"] == "17a8cd3716404e77"
    names = [t["name"] for t in filters["category_tabs"]]
    assert "Роботы-пылесосы" in names and "Вертикальные пылесосы" in names
    assert filters["price_range"] == {"min": 2050, "max": 90799}
    assert filters["seller_counts"] == {"dns": 416, "other": 29}


def test_marketplace_tiles_name_the_seller():
    tiles = parse.listing_tiles(load("search_pylesos_marketplace.html"))
    assert tiles[0]["seller"] == 'ООО "ГИНЗУ"'
    assert tiles[0]["reviews_label"] == "нет отзывов" and tiles[0]["rating_value"] is None


def test_catalog_listing_total_and_nothing_found_page():
    html = load("catalog_smartphones_apple.html")
    assert parse.catalog_total(html) == 257 and len(parse.listing_tiles(html)) == 3
    assert parse.nothing_found(load("search_nothing_found.html"))
    assert not parse.nothing_found(load("search_pylesos.html"))


def test_brand_page_categories():
    cats = parse.brand_categories(load("brand_apple.html"))
    assert cats[0] == {"id": "17a8a01d16404e77", "name": "Смартфоны", "count": 233}


# ---- prices -------------------------------------------------------------------------------

def test_online_payment_discount_is_an_estimate_not_the_price():
    """Trap: 'доп. скидка' applies only when paid online; the price stays 15 999."""
    p = parse.buy_state(load("product_buy_cards.json")[PHILIPS])
    assert p["price_rub"] == 15999
    assert p["online_payment_discount_rub"] == 1600
    assert p["price_if_paid_online_estimate_rub"] == 14399
    assert p["credit_monthly_rub"] == 1560 and p["credit_months"] == 12
    assert "price_before_discount_rub" not in p
    d = parse.discounts(load("discounts_philips.json"))
    assert d["discounts"][0]["condition"] == "Стоимость товара будет снижена при условии оплаты заказа на сайте онлайн"


def test_not_sold_product_reports_last_sale_price_not_a_price():
    """Trap: for a product DNS no longer sells, price.current is the LAST SALE price."""
    p = parse.buy_state(load("product_buy_cards.json")["391235f1-fdb7-11ec-8fe5-00155d8ed20b"])
    assert p["price_rub"] is None and p["not_sold"] is True
    assert p["last_sale_price_rub"] == 2699
    assert p["not_sold_reason"] == "Последняя цена продажи"


def test_same_name_different_products():
    """Trap: two products share the name 'Пылесос Philips FC9734/01 фиолетовый'; one is not sold."""
    cards = load("product_buy_cards.json")
    twins = {g: s for g, s in cards.items() if "Philips FC9734/01" in s["name"]}
    assert len(twins) == 2
    sold = parse.buy_state(twins[PHILIPS])
    other = parse.buy_state(next(s for g, s in twins.items() if g != PHILIPS))
    assert sold["price_rub"] == 15999 and other["price_rub"] is None and other["last_sale_price_rub"] == 14399


def test_marketplace_item_requires_prepayment():
    p = parse.buy_state(load("product_buy_cards.json")["02e42f5b-94d8-4e73-b3de-3a9f8db84271"])
    assert p["price_rub"] == 5500 and p["prepayment_required"] is True
    assert "100%" in p["prepayment_note"]


@pytest.mark.parametrize("price, shown, before", [
    ({"current": 10299, "previous": 12199}, 10299, 12199),   # Karcher VC 3 Plus as on the site
    ({"current": 12000, "min": 11000}, 11000, 12000),         # the site shows `min`, strikes `current`
    ({"current": 12000, "min": 11000, "previous": 13000}, 11000, 13000),
    ({"current": 7299}, 7299, None),
])
def test_price_block_mirrors_the_site_script(price, shown, before):
    p = parse.buy_state({"id": PHILIPS, "price": price})
    assert p["price_rub"] == shown
    assert p.get("price_before_discount_rub") == before


def test_search_prices_include_a_product_echoing_its_code():
    """DNS echoes the requested code instead of the GUID for some products (Midea VCS43A15)."""
    prices = load("search_pylesos_prices.json")
    midea = prices["8174410"]
    assert midea["id"] == "8174410"
    assert parse.buy_state(midea)["price_rub"] == 5499
    assert parse.buy_state(midea)["price_before_discount_rub"] == 6799


def test_warranty_extensions_are_separate_from_the_price():
    w = parse.warranty(load("warranty_philips.json"))
    assert w["standard_months"] == 24
    assert w["paid_extensions"][0] == {"name": "1 год", "price_rub": 2080, "extra_years": 1, "total_months": 36}


# ---- availability ---------------------------------------------------------------------------

def test_availability_widget_echoes_city_and_courier_eta():
    a = parse.avails_summary(load("avails.json")[PHILIPS])
    assert a["summary_text"] == "В наличии в 13 магазинах; Пункты выдачи доступны; Доставим на дом за 2 часа"
    assert a["stores_with_any_unit"] == 13
    assert a["city_id"] == EKB and a["city_name"] == "Екатеринбург"
    assert a["courier_eta_text"] == "за 2 часа" and a["pickup_points_available"] is True
    assert parse.avails_summary(None)["summary_text"] is None


def test_showcase_samples_are_not_packaged_stock():
    """Trap: 'в 13 магазинах' includes stores holding only a showcase sample.

    Checked against the site's stores dialog (2026-09-25): exactly these six
    stores were labelled 'витринный образец'.
    """
    s = parse.shops(load("shops_philips.json"), PHILIPS)
    assert s["city_id"] == EKB
    assert (s["stores_with_any_unit"], s["stores_with_packaged_units"], s["showcase_only_stores"]) == (13, 7, 6)
    showcase_only = {x["name"] for x in s["stores"] if x["packaged_units"] == 0}
    assert showcase_only == {"Терминал Чкаловский", "ЕКБ ТЦ Мегаполис", "ЕКБ ТЦ МЕГА", "ЕКБ ТЦ Дирижабль",
                             "ЕКБ ТЦ Парус", "ЕКБ ТРЦ Радуга Парк"}
    sakharova = next(x for x in s["stores"] if x["name"] == "ЕКБ Сахарова 78")
    assert (sakharova["units"], sakharova["packaged_units"], sakharova["showcase_units"]) == (2, 1, 1)
    assert sakharova["packaged_unit_pickup"] == "now"
    # shown on the site as "при заказе: завтра (c 12:00)" — local time of the city
    assert s["earliest_delivery_to_other_store"] == "2026-09-26 12:00:00"


def test_store_staff_contacts_never_leave_the_parser():
    out = json.dumps(parse.shops(load("shops_philips.json"), PHILIPS), ensure_ascii=False)
    assert "Фейковый" not in out and "fake.manager@example.com" not in out


# ---- product card ------------------------------------------------------------------------------

def test_product_page_identity_and_city():
    card = parse.product_page(load("product_philips.html"))
    assert card["guid"] == PHILIPS and card["code"] == "5482008"
    assert card["name"] == "Пылесос Philips FC9734/01 фиолетовый"
    assert card["category"] == {"id": "17a8cd3716404e77", "name": "Бытовые напольные пылесосы"}
    assert card["breadcrumbs"][-1] == "Бытовые напольные пылесосы" and card["brand"] == "Philips"
    assert (card["rating_value"], card["reviews_label"]) == (4.83, "677 отзывов")
    assert card["questions_count"] == 5 and card["reliability_label"] == "Отличная надежность"
    assert card["city_guid"] == EKB and not card["is_multicard"] and card["seller"] is None


def test_marketplace_card_has_seller_legal_details():
    card = parse.product_page(load("product_ginzzu.html"))
    assert card["seller"]["name"] == 'ООО "ГИНЗУ"' and card["seller"]["inn"] == "7733720370"
    assert card["reviews_label"] == "нет отзывов" and card["rating_value"] is None
    assert not card["has_reliability"]


def test_multicard_rating_is_not_the_products_rating():
    """Trap: the card/microdata rating of a multi-card merges 33 variants (other models too)."""
    card = parse.product_page(load("product_iphone.html"))
    assert card["is_multicard"] and card["reviews_label"] == "2.3k отзывов"
    micro = parse.microdata(load("microdata_iphone.json"))
    own = parse.opinions_stats(load("opinions_iphone_product.json"))
    merged = parse.opinions_stats(load("opinions_iphone_card.json"))
    assert micro["reviews"] == merged["total"] == 2268
    assert own["total"] == 452 and own["has_several_models"] is True
    assert own["stars"]["5"] == 424


def test_hardware_model_comes_from_the_variant_selector():
    """Trap: 'Модель' in characteristics is 'Apple iPhone 16'; A3286 vs A3287 is only in the variants."""
    mc = parse.multicard(load("multicard_iphone.json"), "5491322")
    assert mc["this_variant"] == {"Цвет": "Черный", "Модель": "A3287 (Nano-SIM + eSIM)", "Память (ГБ)": "128"}
    assert len(mc["variants"]) == 33
    models = {v["specs"].get("Модель") for v in mc["variants"]}
    assert {"A3286 (Nano-SIM + eSIM)", "A3287 (Nano-SIM + eSIM)", "A3288 (Dual Nano-SIM)"} <= models
    assert sum(v["this_product"] for v in mc["variants"]) == 1


def test_characteristics_model_and_no_reviewer_data():
    pwa = parse.pwa_product(load("pwa_philips.json"))
    assert pwa["model"] == "Philips FC9734/01" and pwa["warranty_months"] == 24
    assert pwa["characteristics"]["Габариты и вес"]["Вес"] == "5.5 кг"
    out = json.dumps(pwa, ensure_ascii=False)
    assert "Фейков" not in out and "Фейкоград" not in out


def test_reliability_statistic():
    r = parse.service_rating(load("service_rating_philips.json"))
    assert r["label"] == "Отличная надежность"
    assert (r["no_problems_pct"], r["service_requests_pct"]) == (99.85, 0.15)
    assert r["category_average_service_requests_pct"] == 1.28
    assert r["reasons"][0] == {"reason": "Прочие дефекты", "pct": 0.11}


def test_microdata_price_and_rating():
    m = parse.microdata(load("microdata_philips.json"))
    assert (m["code"], m["price"], m["rating_value"], m["reviews"]) == ("5482008", 15999, 4.83, 677)


# ---- price history ------------------------------------------------------------------------------

def test_price_history_points_and_summary():
    points = parse.price_history(load("history_philips.json"))
    assert len(points) == 24 and points[-1] == {"week": "21.09-25.09", "avg_price_rub": 15999}
    s = parse.history_summary(points, 15999)
    assert (s["min_rub"], s["max_rub"], s["weeks"]) == (14599, 19199, 24)
    assert parse.history_summary([], None) == {"weeks": 0, "note": "DNS has no price history for this product"}


def test_partial_current_week():
    from dns_shop_mcp.server import _week_is_partial
    assert _week_is_partial("21.09-25.09", date(2026, 9, 25)) is True
    assert _week_is_partial("14.09-20.09", date(2026, 9, 25)) is False
    assert _week_is_partial("29.12-04.01", date(2027, 1, 2)) is False


# ---- reviews -------------------------------------------------------------------------------------

def test_reviews_without_reviewer_identity():
    items = parse.opinion_items(load("opinions_philips_product.json"), PHILIPS)
    out = json.dumps(items, ensure_ascii=False)
    from conftest import FAKE_PII
    assert not [p for p in FAKE_PII if p in out]
    top = items[0]
    assert top["pinned_top_review"] is True and top["rating"] == 5 and top["votes_up"] == 68
    assert "\n" in top["comment"]  # paragraphs typed by the buyer are kept
    assert items[1]["date"] == "2026-09-22"


def test_card_scope_reviews_mark_other_variants():
    items = parse.opinion_items(load("opinions_iphone_card.json"), IPHONE)
    others = [i for i in items if i.get("other_variant")]
    assert others and all(i["variant"] for i in items)


def test_star_filtered_reviews():
    data = load("opinions_philips_1_2_stars.json")
    assert parse.opinions_stats(data)["total"] == 19
    assert {i["rating"] for i in parse.opinion_items(data)} <= {1, 2}


# ---- city -----------------------------------------------------------------------------------------

def test_city_directory_and_ambiguous_names():
    cities = parse.city_list(load("city_list_sample.json"))
    assert parse.find_city(cities, "Екатеринбург")[0]["id"] == EKB
    assert parse.find_city(cities, "ekaterinburg")[0]["id"] == EKB
    assert parse.find_city(cities, EKB.upper())[0]["name"] == "Екатеринбург"
    assert len(parse.find_city(cities, "Михайловка")) == 6
    assert len(parse.find_city(cities, "Михайловка, Волгоградская")) == 1


def test_city_cookie_and_page_echo():
    from conftest import city_cookie_value
    assert parse.current_path_city(city_cookie_value(EKB, "Екатеринбург"))["city"] == EKB
    assert parse.page_city_guid(load("search_pylesos.html")) == EKB
    assert parse.current_path_city(None) is None


def test_ajax_registry_types():
    reg = parse.ajax_registry(load("product_philips.html"))
    assert {"product-buy", "avails-container", "product-warranty"} <= set(reg)
    assert reg["product-buy"]["containers"][0]["data"]["id"] == "5482008"
