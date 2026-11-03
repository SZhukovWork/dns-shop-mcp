# dns-shop-mcp

**English** · [Русский](README.ru.md)

An [MCP](https://modelcontextprotocol.io) server that gives LLM agents live,
honestly-labelled data from [DNS](https://www.dns-shop.ru) (dns-shop.ru), a large
Russian electronics and home-appliance retailer: search with real pages, sorting,
price windows and stock filters; the full product card for one city; per-store
stock that separates packaged units from showcase samples; per-product ratings
and reviews; weekly price history.

> Buyer side, anonymous: no DNS account is used. Nothing is added to a cart and
> nothing is ordered.

## Why this server

DNS's own numbers are easy to misread. The server is built around not doing that:

| DNS pitfall | What this server does |
|---|---|
| Every dynamic page answers 401 with a Qrator proof-of-work challenge | A headless Chromium visit (≈1.5–2 s) earns the `qrator_jsid2` cookie; the browser is closed and all data comes over plain HTTP. One fresh visit on a challenge or 403, then a clear error |
| The city depends on the client (geo-IP or Moscow by default); prices and stock differ by city | The city is set explicitly on every call (default Екатеринбург) and checked against the city each page and availability widget echoes; a mismatch is an error, not data |
| The rating on a card or tile merges all variants of a "multi-card" — colours, memory sizes, even different hardware models (iPhone 16: 2 268 reviews on the card, 452 for the black 128 GB A3287) | `rating.shown_on_card` (scope "card") next to `rating.this_product`; `merges_several_models` flag |
| Tiles round review counts: "2.3k отзывов" | Parsed as ≈2300 with `reviews_approx: true`; exact counts come from the card |
| "В наличии в 13 магазинах" counts stores that hold only a showcase sample | `stores_with_packaged_units` and `showcase_only_stores` from the store list (checked against the site's store dialog) |
| "доп. скидка 1 600 ₽" applies only when paid online (СБП); the checkout total stays 15 999 ₽ | `price_rub` stays the regular price; the discount is a separate field and `price_if_paid_online_estimate_rub` is labelled as a conditional estimate |
| Products DNS no longer sells still carry a price: the **last sale price** | `not_sold: true`, `price_rub` absent, `last_sale_price_rub` separately |
| Marketplace ("DNS Селлер") items sit in the same search: another seller, 100 % prepayment, not in DNS stores | `seller` with legal name and INN, `prepayment_required` |
| Search silently narrows a query to one category and drops the order, price window and page on that redirect; some queries go to a catalog or brand page instead | The server repeats the request with every parameter; `resolved_category`, `other_categories`; brand queries return the brand's categories |
| "Cheapest first" is not strictly monotonic | Items re-sorted by price within the page, DNS's order kept in `position`, a note says so |
| Different products share one name; "Модель" in characteristics can be generic ("Apple iPhone 16") | `code` everywhere; `model` plus `variant_specs` (e.g. "A3287 (Nano-SIM + eSIM)") and all variants of a multi-card |
| A stale page token makes DNS answer "no states" — which looks like "no price" | Refreshed once, then an error; never reported as "no price" |
| Delivery cost is computed only in the cart | `delivery_cost_rub: null` with a note; only the courier ETA text is given |

Every answer carries `fetched_at` (UTC) and `city` — the city id, name, where the
choice came from and which part of DNS's answer confirmed it.

## Tools

| Tool | What it returns |
|---|---|
| `search_products(query, page, sort, price_min, price_max, availability, category, city, with_availability)` | 24 items per page (fixed by DNS); sort `popular` / `price_asc` / `price_desc` / `rating` / `reviews` / `newest` / `discount`; availability `any` (site default, includes products no longer sold) / `on_sale` / `in_stock` / `today` / `tomorrow` / `later` / `out_of_stock`. `resolved_category`, `other_categories`, `total_found_in_category`, `has_more`, the category's price range and DNS's price buckets (page 1). Per item: code, name, short specs, price fields, `not_sold` + `last_sale_price_rub`, marketplace `seller`, `avail_status`, availability summary and `stores_with_any_unit`, card rating (scope "card"), reliability label, URL |
| `get_product(product, city, include_stores, include_price_history, include_characteristics)` | `product` = code, URL, 16-hex id or GUID. Price block (price, pre-discount price, each discount with its condition, online-payment estimate, credit payment, standard warranty and paid extensions), seller, availability (stores with any unit / with a packaged unit / showcase-only, pickup points, courier ETA, `delivery_cost_rub: null`), per-store stock with pickup time, card rating vs this product's rating with star split and aspect grades, DNS reliability statistic (share of units without service claims, reasons, category average), characteristics incl. "Модель", description, variants of a multi-card, price-history summary, questions count |
| `get_reviews(product, page, limit, scope, sort, min_rating, max_rating, verified_buyers_only, with_photos, search, city)` | `scope` `product` (default) or `multicard` (all variants of the card); `sort` `newest` / `helpful` / `best`; star filter (`max_rating=2` = complaints); DNS's "real buyer" and photo filters; text search inside reviews. Date, stars, pros, cons, comment, usage period, bought variant, votes, photo count; statistics for the scope. No reviewer names |
| `get_price_history(product, city)` | DNS's weekly average selling prices (≈24 weeks) next to the live price; `current_week_is_partial` |
| `compare_products(products, city)` | Up to 24 products: one price request and one availability request for all, plus one review-statistics request each. Price fields, not-sold/prepayment flags, availability, this product's rating and the multi-card flag |

Questions & answers are deliberately not fetched: they live on club.dns-shop.ru,
whose rules forbid automated scripts.

## Requirements

Measured on 2026-09-25 (Linux, home Russian IP, Playwright 1.63, Chromium 1243):

| | |
|---|---|
| Python | ≥ 3.10, with [uv](https://docs.astral.sh/uv/) (or pip) |
| Browser | Playwright's Chromium, downloaded automatically on first run into `~/.cache/ms-playwright` (≈660 MB on disk: headless shell 261 MB + Chromium 393 MB used as a fallback; shared by all Playwright tools). It runs only to pass the Qrator challenge: 1.5–2.0 s, then it is closed |
| Memory | Peak ≈500 MB (PSS of the server plus Chromium, during a Qrator pass); ≈75 MB between passes (the Python server alone) |
| Cold start | MCP `initialize` 1.4 s; the first tool call adds one Qrator pass (1.5–2 s) — a first search took 13.7 s in total |
| Call duration | Requests are paced 2 s apart: search 7–10 s (4–5 requests), `get_product` 21–26 s (10–12; 12–15 s for marketplace or not-sold items), `get_reviews` 3–7 s (1–3), `get_price_history` 5–9 s (3–4), `compare_products` ≈2 s × (2 + number of products). A single request takes 0.1–0.35 s |
| Re-pass | After ~15 min without calls the cookie expires (Max-Age 1000 s); the next call passes Qrator again (+1.5–2 s) |
| Display | Not needed: everything runs headless |
| Docker | Not needed |
| System libraries | Present on desktop Linux, macOS and Windows. On a minimal Debian/Ubuntu server install them once (root): `uvx --from playwright playwright install --with-deps chromium` |
| Network | A Russian IP without VPN: Qrator blocks foreign, VPN and datacenter addresses. Or set `DNS_PROXY` |
| Tested on | Linux (CachyOS). macOS and Windows are supported by Playwright but untested |

## Install

Claude Code:

```bash
claude mcp add dns-shop -- uvx --from git+https://github.com/SZhukovWork/dns-shop-mcp dns-shop-mcp
```

Any MCP client (`claude_desktop_config.json`, `.mcp.json`, …):

```json
{
  "mcpServers": {
    "dns-shop": {
      "command": "uvx",
      "args": ["--from", "git+https://github.com/SZhukovWork/dns-shop-mcp", "dns-shop-mcp"]
    }
  }
}
```

From a checkout: `uv venv && uv pip install -e . && .venv/bin/dns-shop-mcp`.

## Configuration (environment variables)

| Variable | Default | Meaning |
|---|---|---|
| `DNS_CITY` | Екатеринбург | Default city: name as on the site, `Name, Region` for ambiguous names, the city slug or its GUID. Every tool also takes `city` |
| `DNS_MIN_INTERVAL` | `2.0` | Seconds between requests to DNS; values below 1.5 are raised to 1.5 |
| `DNS_PROXY` | — | Proxy URL for HTTP and the browser, e.g. `http://user:pass@host:3128` |
| `DNS_CACHE_DIR` | `~/.cache/dns-shop-mcp` (`$XDG_CACHE_HOME/dns-shop-mcp` if set) | Session (cookie, city cookies, page tokens; file mode 0600, directory 0700) and the city directory (refreshed daily) |
| `DNS_HEADLESS` | `1` | `0` shows the browser window during the Qrator pass (debugging) |

## What the numbers mean

- **`price_rub`**: the price on dns-shop.ru in the reported city for a guest
  (not logged in) — the big price on the card. `price_before_discount_rub` is the
  struck-through price.
- **`online_payment_discount_rub`**: the "доп. скидка" line. DNS applies it only
  when the order is paid online on the site (the badge says СБП); the card's
  "Итого" shows the regular price. `price_if_paid_online_estimate_rub` =
  price − discount, an estimate under that condition.
- **`credit_monthly_rub`**: a loan or instalment payment — not a price.
- **`not_sold` / `last_sale_price_rub`**: DNS does not sell the product now; the
  number is the last price it was sold at.
- **`prepayment_required`**: 100 % prepayment (marketplace sellers).
- **Ratings**: `rating.shown_on_card` is what the card shows and may merge all
  variants of a multi-card; `rating.this_product` counts only this product. Search
  tiles show the card rating (`scope: "card"`).
- **Stock**: `stores_with_any_unit` includes showcase samples;
  `stores_with_packaged_units` counts stores with a unit in its factory package;
  `showcase_only_stores` hold only a showcase sample. Pickup times are local time
  of the city as DNS sends them.
- **Reliability**: DNS's own statistic of units sold in its stores without a
  service claim; not a manufacturer figure.
- **Price history**: weekly average selling price ("За прошедшие недели
  отображается средняя цена продажи"); the last week is the current, partial one.
  Whether past weeks are city-specific is not confirmed (in a comparison of Moscow
  and Yekaterinburg past weeks matched while the current prices differed).

## Limitations

- Unofficial: relies on the site's internal endpoints and markup; DNS can change
  them any time. Parsers are isolated in `parse.py` and tested on recorded answers;
  when markup changes, fields go missing rather than being guessed.
- `robots.txt` of dns-shop.ru disallows `/search/` and URLs with `?` for all
  robots; search cannot work without them. Keep the request rate human (the
  default pace is one request per 2 s) and use it for personal research only.
- Qrator can change its challenge; a Playwright build that passes today may fail
  later. Qrator may bind the cookie to the IP (not verified).
- No personal prices, ProZaPass bonuses or cart: no account is used.
- Delivery cost is not available (only in the cart); only the courier ETA text.
- Used/discounted units (`/catalog/markdown/`) are not supported: `get_product`
  refuses their URLs. Their card shows the rating of the new product. The markdown
  catalog ignores the search phrase, so a search tool needs more research.
- Bundles ("Выгодные комплекты") and accessories/analogs sliders are not covered.
- Questions & answers are not fetched (club.dns-shop.ru rules forbid scripts).
- iPhones: buyers report units activated before sale; this server only passes such
  reviews on — it cannot check a unit.
- DNS's "best rated" search order is its own ranking, not a strict sort by the
  shown rating; `get_reviews(sort="best")` means highest stars first.

## Roadmap

- Optional account mode (personal prices, bonuses) and cart — later.
- Used/discounted units search — needs more research of the markdown catalog.
- Checkout, payment and address changes are deliberately out of scope.

## Development

```bash
uv venv && uv pip install -e '.[dev]'
.venv/bin/pytest            # offline tests on recorded, anonymised answers
.venv/bin/pytest -m live    # end-to-end over MCP stdio against live dns-shop.ru (Russian IP, ~30 requests)
```

## Disclaimer

Not affiliated with DNS. The server uses the site's undocumented internal
endpoints and passes its anti-bot check with a real browser. It is meant for
personal price research at a human request rate; respect DNS's terms of use.

License: MIT.


## Standalone lazy startup

The normal console command and `python -m dns_shop_mcp` now start a lightweight
stdio frontend. No Buyer plugin, external launcher, absolute installation path,
or user configuration change is required. Initialization and tool/resource/prompt
listing use the packaged catalog; the first other request starts this connection's
original server. Tool schemas and results are unchanged, and client sessions are
never shared. The backend remains alive until the client disconnects, preserving
state and ongoing work. Closing stdin stops it; this is not an idle-eviction policy.

Catalogs are checked against package source and installed schema-library versions.
If a catalog is missing or stale, startup falls back to the original server.
Supported catalogs cover MCP SDK 2.2.0 and 2.3.0 and handshake protocols
2024-11-05, 2025-03-26, 2025-06-18 and 2025-11-25. Other versions/protocols continue
working through the original server. Set `MCP_EAGER_START=1` to bypass lazy startup
for diagnostics. Existing CLI subcommands retain their original behavior.

This reduces memory/import CPU when a host eagerly initializes unused connections;
it cannot prevent the host from spawning the initial process. To get zero store
processes in unrelated projects, disable the MCP in those projects in the host.
On POSIX an inherited negative nice priority is normalized to zero.

Maintainers: after changing package sources, regenerate catalogs with
`python scripts/update_lazy_catalog.py` in each supported SDK environment, then
run `pytest -q tests/test_lazy_stdio.py` and build/test the wheel. Generation only
initializes and lists declarations; it does not call storefront tools. The
schema/version tests deliberately fail if declarations and packaged catalogs drift.
