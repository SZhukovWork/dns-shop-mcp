# Standalone lazy startup verification — 2026-11-03

The installed console entry point is self-contained; Buyer and the former
external launcher are not required. `python -m dns_shop_mcp`
uses the same frontend. Existing business handlers are unchanged.

- Full repository suite: **98 passed, 2 skipped, 1 deselected in 6.84s**.
- Dedicated subprocess checks rerun after the final catalog-version change.
- Source distribution and wheel built successfully with `uv build`.
- Built wheel installed by `uv run --isolated --no-project --with WHEEL` in a
  temporary directory, with no source-tree PYTHONPATH or account credentials.
- The installed module came from site-packages, loaded a matching catalog and
  did not import MCP/Pydantic during discovery.
- Current standalone resolution: MCP 2.3.0, mcp-types 2.3.0, Pydantic 2.14.2.
- Discovery returned **5 tools**, with **0 backend children**.
- Calling a real tool with missing required arguments activated **1 backend**
  and returned its validation error. No storefront request/account write was
  needed to verify activation. A later ping used that same backend.
- EOF exited the command and left **0 backend children**.
- Wheel contents were checked: catalog JSON is included; private session files,
  .work and virtual environments are not.

The subprocess suite also compares initialization and full tool/resource/prompt
catalogs against the eager server on four protocol versions, checks two-client
isolation and unknown-protocol/source-change fallback. Mode-specific tests run
only for the packages that have those modes. Avito's durable-reply restoration
bypass is tested without reading or copying live credentials.

These are transport, packaging and existing unit tests on Linux. Live retailer
API tests were deliberately deselected; this does not claim every retailer's
network/API scenario passed or that Windows/macOS were exercised. There is no
idle eviction: a used backend stays until the host closes its connection.
Unrecognized dependency/schema versions fall back to the original server.
