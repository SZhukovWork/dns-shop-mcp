# Changelog

## Unreleased

- Built-in lazy stdio startup for independent installs: packaged discovery catalogs,
  per-client backend activation, EOF cleanup, and unchanged CLI subcommands.
  No Buyer plugin or external wrapper required.


All notable changes to this project are documented here. Versions follow
[Semantic Versioning](https://semver.org): patch releases fix breakage caused
by changes on DNS's side, minor releases add features.

## [0.1.0] — 2026-09-25

### Added
- Access through DNS's Qrator anti-bot: a short headless-Chromium visit earns the
  cookie, everything else is plain HTTP; one fresh visit on a challenge or 403,
  one pause on 429, then a clear error. Requests paced 2 s apart (floor 1.5 s).
- Explicit city on every call (default Екатеринбург, `DNS_CITY`, `city`), checked
  against the city echoed by pages and availability widgets.
- `search_products`: 24 items per page, seven sort orders, price window, stock
  filter (incl. `on_sale`), DNS's category narrowing with parameters re-applied,
  catalog and brand redirects, other categories, price buckets, in-page re-sort
  for price orders.
- `get_product`: labelled price block (online-payment discount as an estimate,
  credit apart, warranty extensions), not-sold products with the last sale price,
  marketplace seller with INN, availability with packaged vs showcase units,
  per-store stock, card vs product rating, DNS reliability statistic,
  characteristics, multi-card variants, price-history summary.
- `get_reviews` (product or card scope, star/buyer/photo/text filters, three sort
  orders, no reviewer data), `get_price_history`, `compare_products` (up to 24).
- Sanity checks: empty AjaxState answers, answers about another product or city
  are repeated once and then refused.
