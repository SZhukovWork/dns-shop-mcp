"""Pure translations of raw dns-shop.ru answers into tool output.

Nothing here does I/O, so every function is exercised by tests on recorded
answers (tests/fixtures). HTML is parsed with narrow regular expressions
anchored on DNS's own class names; when the markup changes, a parser returns
None/empty rather than a guess, and the caller reports "no data".
"""
from __future__ import annotations

import html as _html
import json
import re
from typing import Any, Iterable
from urllib.parse import parse_qs, unquote, urljoin, urlsplit

SITE = "https://www.dns-shop.ru"

_GUID = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$", re.I)
_HEX16 = re.compile(r"^[0-9a-f]{16}$", re.I)
_CODE = re.compile(r"^\d{4,9}$")


# ---- identifiers ------------------------------------------------------------

def is_guid(s: str) -> bool:
    return bool(_GUID.match(s or ""))


def guid_to_hex16(guid: str) -> str:
    """The 16-hex id DNS puts in product URLs, derived from the product GUID.

    Observed on every product seen: first 8 hex + second group + 4 hex from the
    middle of the last group (9d624585-5873-11ef-9250-0050569d5825 -> 9d6245855873d582).
    """
    g = guid.lower()
    return g[:8] + g[9:13] + g[-5:-1]


def product_short_url(guid: str) -> str:
    """A working product link (DNS redirects it to the slugged URL)."""
    return f"{SITE}/product/{guid_to_hex16(guid)}/"


def classify_product_ref(ref: str) -> tuple[str, str]:
    """What kind of product reference the user passed: (kind, value).

    kinds: "guid", "hex16", "code", "markdown" (a used/discounted unit), or raises ValueError.
    """
    s = (ref or "").strip()
    if s.startswith("#"):
        s = s[1:].strip()
    if is_guid(s):
        return "guid", s.lower()
    if _HEX16.match(s):
        return "hex16", s.lower()
    if _CODE.match(s):
        return "code", s
    if "dns-shop.ru" in s or s.startswith("/"):
        path = urlsplit(s if "://" in s else "https://" + s.lstrip("/") if not s.startswith("/") else SITE + s).path
        m = re.search(r"/product/(?:opinion/|characteristics/|service-rating/)?([0-9a-f]{16})(?:/|$)", path, re.I)
        if m:
            return "hex16", m.group(1).lower()
        m = re.search(r"/catalog/markdown/([0-9a-f-]{36})", path, re.I)
        if m:
            return "markdown", m.group(1).lower()
    raise ValueError(
        f"Not a DNS product reference: {ref!r}. Pass the product code (e.g. 5482008, shown as "
        "'Код товара'), the product URL, its 16-hex id from the URL or its GUID."
    )


# ---- small helpers -------------------------------------------------------------

def text(fragment: str | None, multiline: bool = False) -> str:
    """Visible text of an HTML fragment.

    Single line by default; `multiline` keeps paragraph breaks (<p>, <br>, and
    the line breaks buyers typed), for review texts.
    """
    if not fragment:
        return ""
    s = fragment if multiline else re.sub(r"\s+", " ", fragment)
    s = re.sub(r"<br\s*/?>|</p>|</li>", "\n", s, flags=re.I)
    s = re.sub(r"<[^>]+>", " ", s)
    s = _html.unescape(s).replace("\xa0", " ")
    s = re.sub(r"[ \t\r\f\v]+", " ", s)
    s = re.sub(r" *\n+ *", "\n", s).strip()
    return s if multiline else s.replace("\n", " ")


def _num(s: str | None) -> float | None:
    if s is None:
        return None
    m = re.search(r"-?\d+(?:[.,]\d+)?", s.replace("\xa0", "").replace(" ", ""))
    return float(m.group(0).replace(",", ".")) if m else None


def _int(v: Any) -> int | None:
    if v is None or v == "":
        return None
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    return int(f) if f == int(f) else None


def rub(v: Any) -> int | float | None:
    """A ruble amount as DNS sends it; 0/None/'' mean "no amount", not free."""
    if v in (None, "", 0, "0"):
        return None
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    if f <= 0:
        return None
    return int(f) if f == int(f) else round(f, 2)


def reviews_label_count(label: str | None) -> tuple[int | None, bool]:
    """'2.3k отзывов' -> (2300, approximate=True); '677 отзывов' -> (677, False)."""
    if not label:
        return None, False
    s = label.replace("\xa0", " ").strip().lower()
    if "нет" in s:
        return 0, False
    m = re.search(r"(\d+(?:[.,]\d+)?)\s*([kкmм])?", s)
    if not m:
        return None, False
    value = float(m.group(1).replace(",", "."))
    suffix = m.group(2)
    if suffix in ("k", "к"):
        return int(round(value * 1000)), True
    if suffix in ("m", "м"):
        return int(round(value * 1_000_000)), True
    return int(value), False


# ---- page plumbing -----------------------------------------------------------------

def csrf_token(html: str) -> str | None:
    m = re.search(r'<meta name="csrf-token" content="([^"]+)"', html)
    return m.group(1) if m else None


def ajax_registry(html: str) -> dict[str, dict]:
    """`window.AjaxState.register([...])` of a page: {type: {"hash", "containers"}}."""
    m = re.search(r"window\.AjaxState\.register\((\[.*?\])\);", html, re.S)
    if not m:
        return {}
    try:
        blocks = json.loads(m.group(1))
    except ValueError:
        return {}
    out: dict[str, dict] = {}
    for block in blocks:
        if not block or not isinstance(block[0], dict) or "type" not in block[0]:
            continue
        out[block[0]["type"]] = {"hash": block[0].get("hash"),
                                 "containers": block[1] if len(block) > 1 else []}
    return out


def page_city_guid(html: str) -> str | None:
    """The city a page was rendered for (`window.baseHeaderConfig.config.cityGuid`)."""
    m = re.search(r'baseHeaderConfig\s*=\s*\{"config":\{"cityGuid":"([0-9a-f-]{36})"', html)
    return m.group(1).lower() if m else None


def current_path_city(cookie_value: str | None) -> dict | None:
    """City stored in DNS's signed `current_path` cookie: {"city", "cityName", "method"}."""
    raw = unquote(cookie_value or "")
    m = re.search(r'(\{"city".*?\})', raw)
    if not m:
        return None
    try:
        return json.loads(m.group(1))
    except ValueError:
        return None


# ---- search / listing -------------------------------------------------------------

def _attr(tag: str, name: str) -> str | None:
    m = re.search(name + r'="([^"]*)"', tag)
    return _html.unescape(m.group(1)) if m else None


def listing_tiles(html: str) -> list[dict]:
    """Product tiles of a search or catalog page, in the site's order.

    Prices are not in the HTML (they come from /ajax-state/product-buy/).
    """
    starts = [m for m in re.finditer(r'<div[^>]*class="catalog-product ui-button-widget"[^>]*>', html)]
    out = []
    for i, m in enumerate(starts):
        tag = m.group(0)
        end = starts[i + 1].start() if i + 1 < len(starts) else m.end() + 12000
        seg = html[m.end():end]
        name = re.search(r'<a class="catalog-product__name[^"]*"[^>]*href="([^"]+)"[^>]*>(.*?)</a>', seg, re.S)
        short_specs = None
        title = None
        if name:
            specs = re.search(r'<span class="catalog-product__short-specs">(.*?)</span>', name.group(2), re.S)
            short_specs = text(specs.group(1)) if specs else None
            title = text(re.sub(r'<span class="catalog-product__short-specs">.*?</span>', "", name.group(2), flags=re.S))
        rating = re.search(r'<a class="catalog-product__rating[^"]*"[^>]*>(.*?)</a>', seg, re.S)
        rating_value, reviews_label = None, None
        if rating:
            b = re.search(r"<b>\s*([\d.,]+)\s*</b>", rating.group(1))
            rating_value = float(b.group(1).replace(",", ".")) if b else None
            label = text(re.sub(r"<b>.*?</b>|<span>\|</span>", " ", rating.group(1), flags=re.S))
            reviews_label = label or None
        reliability = re.search(r'<a class="catalog-product__service-rating"[^>]*>(.*?)</a>', seg, re.S)
        seller = re.search(r'<div class="catalog-product__seller">(.*?)</div>', seg, re.S)
        seller_name = text(seller.group(1)) if seller else None
        if seller_name and seller_name.lower().startswith("продавец:"):
            seller_name = seller_name.split(":", 1)[1].strip()
        out.append({
            "guid": (_attr(tag, "data-product") or "").lower() or None,
            "code": _attr(tag, "data-code"),
            "avail_status": _attr(tag, "data-avail-status") or None,
            "name": title,
            "short_specs": short_specs,
            "url": urljoin(SITE, name.group(1)) if name else None,
            "rating_value": rating_value,
            "reviews_label": reviews_label,
            "reliability_label": text(reliability.group(1)) if reliability else None,
            "seller": seller_name or None,
        })
    return out


def search_total(html: str) -> int | None:
    """Products found — inside the category DNS picked (`products-filtered-count`)."""
    m = re.search(r'data-role="items-filtered-count">\s*(\d+)\s*<', html)
    return int(m.group(1)) if m else None


def catalog_total(html: str) -> int | None:
    m = re.search(r'class="products-count"[^>]*>\s*(\d+)', html) or re.search(r'products-count[^>]*>\s*(\d+)\s*товар', html)
    return int(m.group(1)) if m else None


def search_heading(html: str) -> str | None:
    m = re.search(r'<h1[^>]*data-role="items-count"[^>]*>(.*?)</h1>', html, re.S) or re.search(r"<h1[^>]*>(.*?)</h1>", html, re.S)
    return text(m.group(1)) if m else None


def heading_category(heading: str | None) -> str | None:
    """'Найдено 445 товаров в категории Бытовые напольные пылесосы' -> category name."""
    if not heading:
        return None
    m = re.search(r"в категории\s+(.+)$", heading)
    if m:
        return m.group(1).strip()
    # A catalog listing's heading is the category name itself ("Смартфоны").
    return None if re.match(r"^(Найден|Ничего)", heading) else heading.strip()


def nothing_found(html: str) -> bool:
    return "ничего нет" in text(html[:200000]).lower() and not listing_tiles(html)


def query_param(url: str, name: str) -> str | None:
    values = parse_qs(urlsplit(url).query).get(name)
    return values[0] if values else None


def search_filters(data: dict) -> dict:
    """The site's filter panel for a search: category tabs, price range, availability options."""
    blocks = ((data or {}).get("data") or {}).get("blocks") or {}
    tabs, selected = [], None
    for block in blocks.get("pageTop") or []:
        if block.get("id") == "category":
            sel = block.get("selected") or []
            selected = sel[0] if sel else None
            tabs = [{"id": v.get("id"), "name": v.get("label")} for v in block.get("variants") or []]
    price, buckets = None, []
    for block in blocks.get("left") or []:
        if block.get("id") == "price":
            price = {"min": _int(block.get("min")), "max": _int(block.get("max"))}
            buckets = [{"label": v.get("label"), "min": _int(v.get("min")), "max": _int(v.get("max")),
                        "count": v.get("count")} for v in block.get("variants") or []]
    sellers = {}
    for block in blocks.get("left") or []:
        if block.get("id") == "seller":
            sellers = {v.get("id"): v.get("count") for v in block.get("variants") or []}
    return {"selected_category": selected, "category_tabs": tabs, "price_range": price,
            "price_buckets": buckets, "seller_counts": sellers}


def brand_categories(html: str) -> list[dict]:
    """Categories listed on a /brand/<slug>/ page with the number of the brand's products."""
    out, seen = [], set()
    for m in re.finditer(r'<a[^>]+href="/catalog/([0-9a-f]{16})/([^/"]+)/\?brand=([^"&]+)"[^>]*>(.*?)</a>', html, re.S):
        cat = m.group(1)
        if cat in seen:
            continue
        label = text(m.group(4))
        cm = re.match(r"^(.*?)\s+(\d+)$", label)
        if not label:
            continue
        seen.add(cat)
        out.append({"id": cat, "name": cm.group(1) if cm else label, "count": int(cm.group(2)) if cm else None})
    return out


# ---- prices ----------------------------------------------------------------------------

def buy_state(data: dict | None) -> dict:
    """One /ajax-state/product-buy/ state -> labelled price fields.

    Mirrors the site's own rendering (ajax-state-product-buy.js):
    - the big price is `price.min || price.current`;
    - the struck-through price is shown when `previous` exists or `min` differs
      from `current`; it is the larger of `current` and `previous`;
    - `onlinePay` is the "доп. скидка" line: a discount applied only when the
      order is paid online; the checkout total does not include it;
    - `notAvail` means the product is not sold: `current` is then the LAST SALE
      price (tooltip "Последняя цена продажи"), not a price one can buy at;
    - `avail` is a text shown instead of a price;
    - `isPrepay`: 100 % prepayment required (typical for marketplace sellers).
    """
    data = data or {}
    price = data.get("price") or {}
    current = rub(price.get("current"))
    minimum = rub(price.get("min"))
    previous = rub(price.get("previous"))
    online = rub(price.get("onlinePay"))
    credit = data.get("credit") or {}
    out: dict[str, Any] = {"guid": (data.get("id") or "").lower() or None, "name": data.get("name")}
    not_sold = bool(data.get("notAvail")) or bool(data.get("avail"))
    shown = minimum or current
    if not_sold:
        out.update({
            "price_rub": None,
            "not_sold": True,
            "not_sold_reason": text(data.get("avail")) or data.get("tooltip") or "not on sale",
        })
        if current:
            out["last_sale_price_rub"] = current
        return out
    if shown is None:
        out.update({"price_rub": None, "not_sold": True, "not_sold_reason": "DNS shows no price"})
        return out
    out["price_rub"] = shown
    if previous or (minimum and current and minimum != current):
        before = max(x for x in (current, previous) if x)
        if before > shown:
            out["price_before_discount_rub"] = before
    if data.get("showPriceFrom"):
        out["price_is_from"] = True
    if online:
        out["online_payment_discount_rub"] = online
        out["price_if_paid_online_estimate_rub"] = shown - online
    if credit.get("amount"):
        out["credit_monthly_rub"] = rub(credit.get("amount"))
        out["credit_months"] = _int(credit.get("period"))
        if credit.get("isInstallment"):
            out["credit_is_installment"] = True
    if data.get("isPrepay"):
        out["prepayment_required"] = True
        if data.get("tooltip"):
            out["prepayment_note"] = data.get("tooltip")
    if data.get("honestPriceText"):
        out["price_note"] = text(data.get("honestPriceText"))
    if data.get("maxPeriodPrice"):
        out["max_period_price_label"] = text(str(data.get("maxPeriodPrice")))
    return out


def discounts(data: dict | None) -> dict:
    """/catalog/product/get-min-price-discounts/ -> {discounts: [...], raw prices}."""
    d = (data or {}).get("data") or {}
    items = []
    for x in d.get("discounts") or []:
        details = x.get("details") or {}
        items.append({
            "kind": x.get("description") or details.get("title"),
            "amount_rub": rub(x.get("amount")),
            "condition": text(details.get("content")) or None,
            "optional": bool(x.get("optionalState")),
            "type_code": x.get("type"),
            "details_url": urljoin(SITE, details["url"]) if details.get("url") else None,
        })
    return {"current": rub(d.get("current")), "previous": rub(d.get("previous")), "min": rub(d.get("min")),
            "online_pay": rub(d.get("onlinePay")), "discounts": items}


def warranty(state: dict | None) -> dict | None:
    """/ajax-state/product-warranty/ -> paid warranty extensions (not part of the price)."""
    if not state:
        return None
    base = _int((state.get("defaultPeriod") or {}).get("period"))
    items = []
    for x in state.get("items") or []:
        if not rub(x.get("price")):
            continue
        extra = _int(x.get("period"))
        items.append({"name": x.get("name"), "price_rub": rub(x.get("price")), "extra_years": extra,
                      "total_months": base + 12 * extra if base is not None and extra is not None else None})
    return {"standard_months": base, "paid_extensions": items}


# ---- availability ---------------------------------------------------------------------

def avails_summary(fragment: str | None) -> dict:
    """/ajax-state/avails-container/ HTML -> availability summary + the city it was computed for.

    "В наличии в 13 магазинах" counts every store holding a unit, showcase
    samples included (see `shops`).
    """
    h = fragment or ""
    marks = list(re.finditer(r'<div\s+class\s*=\s*"((?:order-avail-wrap|delivery-info-widget)[^"]*)"', h))
    parts = []
    for i, m in enumerate(marks):
        start = h.find(">", m.end()) + 1
        end = marks[i + 1].start() if i + 1 < len(marks) else len(h)
        part = text(h[start:end])
        if part:
            parts.append(part)
    if not marks and text(h):
        parts.append(text(h))
    summary = "; ".join(parts)
    stores = None
    m = re.search(r"в\s+(\d+)\s+магазин", summary)
    if m:
        stores = int(m.group(1))
    courier = re.search(r'class="delivery-info-widget__button[^"]*"[^>]*>(.*?)</a>', h, re.S)
    return {
        "summary_text": summary or None,
        "stores_with_any_unit": stores,
        "pickup_points_available": True if re.search(r"Пункты выдачи\s+доступны", summary) else None,
        "courier_eta_text": text(courier.group(1)) if courier else None,
        "city_id": (_attr_loose(h, "data-city-id") or "").lower() or None,
        "city_name": _attr_loose(h, "data-city-name"),
        "widget_price": rub(_attr_loose(h, "data-price")),
    }


def _attr_loose(h: str, name: str) -> str | None:
    m = re.search(name + r'\s*=\s*"([^"]*)"', h)
    return _html.unescape(m.group(1)).strip() if m else None


def shops(data: dict | None, guid: str) -> dict:
    """/pickup/v1/shops-avail-by-partition -> per-store units, packaged units and showcase samples.

    DNS counts a showcase sample as "in stock": `maxCount` includes it,
    `maxCountWithWrapped` counts only units in the factory package.
    """
    d = data or {}
    by_branch = {}
    for p in ((d.get("avail") or {}).get("products") or []):
        if (p.get("productId") or "").lower() == guid.lower():
            by_branch = {b.get("branchId"): b for b in p.get("branches") or []}
    stores, packaged_now, any_unit, showcase_only, orderable = [], 0, 0, 0, []
    for s in d.get("shops") or []:
        b = by_branch.get(s.get("id")) or {}
        units = _int(b.get("maxCount")) or 0
        packaged = _int(b.get("maxCountWithWrapped")) or 0
        showcase = sum(_int(x.get("count")) or 0 for x in s.get("showcases") or []
                       if (x.get("productId") or "").lower() == guid.lower())
        if units > 0:
            any_unit += 1
            if packaged > 0:
                packaged_now += 1
            else:
                showcase_only += 1
            stores.append({
                "name": s.get("title"),
                "type": s.get("type"),
                "address": s.get("address"),
                "hours": s.get("workTime"),
                "units": units,
                "packaged_units": packaged,
                "showcase_units": showcase or max(units - packaged, 0) or None,
                "packaged_unit_pickup": _when(b.get("whenWithWrapped")),
                "store_rating": (s.get("rating") or {}).get("rating"),
            })
        else:
            when = _when(b.get("when"))
            if when and when != "not possible":
                orderable.append(when)
    return {
        "city_id": next(((s.get("cityId") or "").lower() for s in d.get("shops") or [] if s.get("cityId")), None),
        "stores_in_city": len(d.get("shops") or []),
        "stores_with_any_unit": any_unit,
        "stores_with_packaged_units": packaged_now,
        "showcase_only_stores": showcase_only,
        "stores": stores,
        "other_stores_can_receive_it": len(orderable),
        "earliest_delivery_to_other_store": min(orderable) if orderable else None,
    }


def _when(v: str | None) -> str | None:
    if not v:
        return None
    if v == "now":
        return "now"
    if v.strip() == "-":
        return "not possible"
    return v


# ---- product card ----------------------------------------------------------------------

def product_page(html: str) -> dict:
    """SSR product card: identity, breadcrumbs, the card's rating line, reliability, seller."""
    guid = re.search(r'data-product-card="([0-9a-f-]{36})"', html)
    title = re.search(r'<h1 class="product-card-top__title"[^>]*>(.*?)</h1>', html, re.S)
    code = re.search(r'<div class="product-card-top__code">.*?</span>\s*([0-9]+)\s*</div>', html, re.S)
    crumbs, category = [], None
    ol = re.search(r'<ol class="breadcrumb-list".*?</ol>', html, re.S)
    if ol:
        for li in re.finditer(r"<li[^>]*>(.*?)</li>", ol.group(0), re.S):
            href = re.search(r'href="([^"]+)"', li.group(1))
            name = text(li.group(1))
            if not name or name == "Каталог":
                continue
            if href and "?brand=" in href.group(1):
                continue
            if not href:
                continue  # the product itself
            crumbs.append(name)
            cm = re.search(r"/catalog/([0-9a-f]{16})/", href.group(1))
            if cm:
                category = {"id": cm.group(1), "name": name}
    brand = re.search(r'data-commerce-target="PRODUCT_BRAND"[^>]*>\s*<span[^>]*>(.*?)</span>', html, re.S)
    rating = re.search(r'<a class="product-card-top__rating[^"]*"[^>]*>(.*?)</a>', html, re.S)
    rating_value, reviews_label = None, None
    if rating:
        full = text(re.sub(r"<div.*?</div>", " ", rating.group(1), flags=re.S))
        v = re.match(r"^(\d+(?:[.,]\d+)?)\s+(.+)$", full)
        if v:
            rating_value, reviews_label = float(v.group(1).replace(",", ".")), v.group(2)
        else:
            reviews_label = full or None  # "нет отзывов"
    questions = re.search(r'<a class="product-card-top__comments"[^>]*>\s*<span>\s*(\d*)', html, re.S)
    reliability = re.search(r'<a class="product-card-top__service-rating"[^>]*>(.*?)</a>', html, re.S)
    valid = re.search(r'<div class="product-card__offer">(.*?)</div>', html, re.S)
    seller = None
    sm = re.search(r"window\.registerSellerInfoHint\([^,]+,\s*(\{.*?\})\);", html, re.S)
    if sm:
        try:
            info = json.loads(sm.group(1))
            seller = {"name": info.get("name"), "inn": info.get("inn"), "ogrn": info.get("ogrn"),
                      "legal_address": info.get("legalAddress"),
                      "catalog_url": urljoin(SITE, info["catalogUrl"]) if info.get("catalogUrl") else None}
        except ValueError:
            seller = None
    if seller is None:
        sn = re.search(r'<span class="product-card-top__seller-name"[^>]*>(.*?)</span>', html, re.S)
        if sn:
            seller = {"name": text(sn.group(1))}
    return {
        "guid": guid.group(1).lower() if guid else None,
        "code": code.group(1) if code else None,
        "name": text(title.group(1)) if title else None,
        "breadcrumbs": crumbs,
        "category": category,
        "brand": text(brand.group(1)) if brand else None,
        "rating_value": rating_value,
        "reviews_label": reviews_label,
        "questions_count": int(questions.group(1)) if questions and questions.group(1) else None,
        "reliability_label": text(reliability.group(1)) if reliability else None,
        "has_reliability": bool(reliability),
        "is_multicard": 'class="multicard ' in html or 'class="multicard"' in html,
        "offer_valid_on": text(valid.group(1)) if valid else None,
        "seller": seller,
        "city_guid": page_city_guid(html),
    }


def microdata(data: dict | None) -> dict:
    d = (data or {}).get("data") or {}
    offers = d.get("offers") or {}
    agg = d.get("aggregateRating") or {}
    return {
        "name": d.get("name"),
        "code": d.get("sku"),
        "brand": (d.get("brand") or {}).get("name"),
        "url": offers.get("url"),
        "price": rub(offers.get("price")),
        "availability": (offers.get("availability") or "").rsplit("/", 1)[-1] or None,
        "rating_value": agg.get("ratingValue") if agg.get("reviewCount") else None,
        "reviews": _int(agg.get("reviewCount")),
        "description": d.get("description"),
    }


def pwa_product(data: dict | None) -> dict:
    """/pwa/pwa/get-product/ -> characteristics grouped as on the site, model, warranty, country.

    The payload also carries a "top opinion" with the reviewer's name and city;
    it is dropped on purpose.
    """
    d = (data or {}).get("data") or {}
    groups: dict[str, dict[str, str]] = {}
    model = None
    for group, rows in (d.get("characteristics") or {}).items():
        values = {}
        for row in rows or []:
            title, value = text(row.get("title")), text(str(row.get("value", "")))
            if title:
                values[title] = value
                if title == "Модель" and model is None:
                    model = value
        if values:
            groups[group] = values
    return {
        "guid": (d.get("guid") or "").lower() or None,
        "code": d.get("code"),
        "name": d.get("name"),
        "short_specs": d.get("specs"),
        "model": model,
        "characteristics": groups,
        "description": d.get("description"),
        "manufacturer_country": d.get("manufacturerCountry"),
        "warranty_months": _int(d.get("monthWarranty")),
        "bonus": d.get("bonus"),
    }


def multicard(data: dict | None, code: str) -> dict:
    """/load-multicard/<code>/ -> the product's variant values and all variants of the card."""
    d = (data or {}).get("data") or {}
    params = [*(d.get("imageParams") or []), *(d.get("textParams") or []), *(d.get("numericParams") or [])]
    specs_by_code: dict[str, dict[str, str]] = {}
    for param in params:
        for value in param.get("values") or []:
            for c in value.get("products") or []:
                specs_by_code.setdefault(str(c), {})[param.get("title")] = value.get("title")
    variants = []
    for p in d.get("products") or []:
        c = str(p.get("id"))
        variants.append({"code": c, "url": urljoin(SITE, p.get("url") or ""), "available": p.get("isAvailable"),
                         "specs": specs_by_code.get(c, {}), "this_product": c == str(code)})
    return {"this_variant": specs_by_code.get(str(code), {}), "variants": variants}


def service_rating(data: dict | None) -> dict | None:
    """/product/service-rating/ HTML -> DNS's reliability statistic for the product."""
    h = (data or {}).get("html") or ""
    if not h:
        return None
    label = re.search(r'class="product-service-rating__panel-title"[^>]*>(.*?)</', h, re.S)
    hint = re.search(r'class="product-service-rating__panel-hint"[^>]*>(.*?)</', h, re.S)
    pairs = re.findall(r'__reasons-reason-text"[^>]*>(.*?)</div>\s*<div[^>]*__reasons-reason-percent"[^>]*>(.*?)</div>', h, re.S)
    details = re.findall(r'__reasons-detail-text"[^>]*>(.*?)</div>\s*<div[^>]*__reasons-detail-percent"[^>]*>(.*?)</div>', h, re.S)
    no_problem, service = None, None
    for name, pct in pairs:
        n, v = text(name), _num(text(pct))
        if n.startswith("Без проблем"):
            no_problem = v
        elif "обращений" in n:
            service = v
    avg = re.search(r"среднее количество обращений в сервис по категории\s+(.*?)\s+-\s+([\d.,]+)\s*%", text(h))
    return {
        "label": text(label.group(1)) if label else None,
        "hint": text(hint.group(1)) if hint else None,
        "no_problems_pct": no_problem,
        "service_requests_pct": service,
        "reasons": [{"reason": text(n), "pct": _num(text(p))} for n, p in details],
        "category_average_service_requests_pct": float(avg.group(2).replace(",", ".")) if avg else None,
        "category": avg.group(1) if avg else None,
    }


# ---- price history ---------------------------------------------------------------------

def price_history(data: dict | None) -> list[dict]:
    d = (data or {}).get("data") or {}
    points = []
    for group in d.get("groups") or []:
        for p in group.get("points") or []:
            price = rub(p.get("priceNum"))
            if price is not None:
                points.append({"week": p.get("label"), "avg_price_rub": price})
    return points


def history_summary(points: list[dict], current: float | None) -> dict:
    if not points:
        return {"weeks": 0, "note": "DNS has no price history for this product"}
    prices = [p["avg_price_rub"] for p in points]
    out = {
        "weeks": len(points),
        "first_week": points[0]["week"],
        "last_week": points[-1]["week"],
        "min_rub": min(prices),
        "max_rub": max(prices),
        "last_points": points[-4:],
    }
    if current is not None:
        out["current_vs_min_pct"] = round((current - min(prices)) / min(prices) * 100, 1)
    return out


# ---- reviews ---------------------------------------------------------------------------

_OPINION_KEEP = ("id", "rating", "insertStamp", "plus", "minus", "comment", "period", "votes", "images",
                 "videos", "source", "multiCardSpecs", "productName", "productId", "isTop", "additions",
                 "usersCommentsCount")


def opinions_stats(data: dict | None) -> dict:
    d = (data or {}).get("data") or {}
    grades = {str(g.get("grade")): g.get("count") for g in d.get("grades") or []}
    return {
        "total": _int(d.get("totalCount")),
        "average": d.get("averageRating") if d.get("totalCount") else None,
        "stars": grades or None,
        "has_several_models": d.get("hasSeveralModels"),
        "characteristic_grades": {text(c.get("title")): c.get("value") for c in d.get("characteristicGrades") or []} or None,
        "tags_positive": d.get("tagCloudPositive") or None,
        "tags_neutral": d.get("tagCloudNeutral") or None,
        "with_photos": ((d.get("filtersCount") or {}).get("hasPhoto")),
    }


def opinion_items(data: dict | None, this_guid: str | None = None) -> list[dict]:
    """Review texts without anything that identifies the reviewer.

    Only whitelisted fields are read; the author block (name, avatar, id,
    profile link) is never touched.
    """
    d = (data or {}).get("data") or {}
    out = []
    for o in d.get("opinions") or []:
        o = {k: o.get(k) for k in _OPINION_KEEP}
        votes = o.get("votes") or {}
        source = o.get("source") or {}
        additions = [
            {"date": (a.get("insertStamp") or a.get("date") or "")[:10] or None,
             "text": text(a.get("text") or a.get("comment"), multiline=True)}
            for a in o.get("additions") or [] if isinstance(a, dict)
        ]
        item = {
            "date": (o.get("insertStamp") or "")[:10] or None,
            "rating": o.get("rating"),
            "plus": text(o.get("plus"), multiline=True) or None,
            "minus": text(o.get("minus"), multiline=True) or None,
            "comment": text(o.get("comment"), multiline=True) or None,
            "usage_period": o.get("period") or None,
            "variant": o.get("multiCardSpecs") or None,
            "product_name": o.get("productName"),
            "votes_up": votes.get("positive"),
            "votes_down": votes.get("negative"),
            "photos": len(o.get("images") or []),
            "videos": len(o.get("videos") or []) or None,
            "additions": [a for a in additions if a["text"]] or None,
            "source": source.get("title") if source.get("isExternal") else None,
            "pinned_top_review": True if o.get("isTop") else None,
        }
        if this_guid and o.get("productId") and o["productId"].lower() != this_guid.lower():
            item["other_variant"] = True
        out.append({k: v for k, v in item.items() if v is not None})
    return out


# ---- cities ----------------------------------------------------------------------------

def city_list(data: dict | None) -> list[dict]:
    d = (data or {}).get("data") or {}
    regions = {r.get("id"): r.get("name") for r in d.get("regions") or []}
    out, seen = [], set()
    for c in [*(d.get("bigCities") or []), *(d.get("cities") or [])]:
        cid = (c.get("id") or "").lower()
        if not cid or cid in seen:
            continue
        seen.add(cid)
        out.append({"id": cid, "name": c.get("name"), "slug": c.get("citySlug"),
                    "region": regions.get(c.get("regionId"))})
    return out


def _norm(s: str) -> str:
    return re.sub(r"\s+", " ", (s or "").strip().lower().replace("ё", "е"))


def find_city(cities: Iterable[dict], query: str) -> list[dict]:
    """Cities matching a GUID, slug, name or "name, region"."""
    q = _norm(query)
    cities = list(cities)
    if is_guid(q):
        return [c for c in cities if c["id"] == q]
    by_slug = [c for c in cities if _norm(c.get("slug") or "") == q]
    if by_slug:
        return by_slug
    name, _, region = q.partition(",")
    name, region = name.strip(), region.strip()
    matches = [c for c in cities if _norm(c.get("name") or "") == name]
    if region:
        matches = [c for c in matches if region in _norm(c.get("region") or "")]
    return matches
