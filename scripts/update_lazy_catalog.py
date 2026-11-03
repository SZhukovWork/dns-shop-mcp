#!/usr/bin/env python3
"""Rebuild packaged discovery catalogs offline; never invokes a storefront tool.

Run with the SDK version the catalog should support, e.g.
uv run --with mcp==2.3.0 python scripts/update_lazy_catalog.py
The server gets an empty development Avito session for initialization.
"""

from __future__ import annotations

import importlib
import json
import os
import queue
import subprocess
import sys
import tempfile
import threading
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
PACKAGE = next(path.name for path in (REPO / "src").iterdir() if path.name.endswith("_mcp"))
sys.path.insert(0, str(REPO / "src"))
lazy = importlib.import_module(PACKAGE + ".lazy_stdio")


def capture(protocol, mode):
    with tempfile.TemporaryDirectory(prefix="mcp-catalog-") as temporary:
        env = {
            **os.environ,
            "MCP_EAGER_START": "1",
            "PYTHONPATH": str(REPO / "src"),
            "AVITO_SESSION_FILE": str(Path(temporary) / "session.json"),
            "AVITO_TRANSPORT": "mobile",
            "AVITO_TOOLSET": mode if PACKAGE == "avito_mcp" else "standard",
            "WB_ACCOUNT": "1" if mode == "account" else "0",
        }
        env.pop("AVITO_SESSION", None)
        child = subprocess.Popen(
            [sys.executable, "-m", PACKAGE],
            env=env,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            start_new_session=(os.name == "posix"),
        )
        events = queue.Queue()
        threading.Thread(target=lazy._read_lines, args=(child.stdout, events, "backend"), daemon=True).start()

        def send(message):
            child.stdin.write((json.dumps({"jsonrpc": "2.0", **message}) + "\n").encode())
            child.stdin.flush()

        def receive(identifier):
            while True:
                _, line = events.get(timeout=45)
                if line is None:
                    raise RuntimeError("Server exited during catalog generation")
                message = json.loads(line)
                if message.get("id") == identifier and "method" not in message:
                    if "error" in message:
                        raise RuntimeError(f"Catalog request failed: {message['error']}")
                    return message["result"]
                if "id" in message and "method" in message:
                    raise RuntimeError("Unexpected client interaction during catalog generation")

        try:
            send(
                {
                    "id": 0,
                    "method": "initialize",
                    "params": {
                        "protocolVersion": protocol,
                        "capabilities": {},
                        "clientInfo": {"name": "catalog-build", "version": "1"},
                    },
                }
            )
            initial = receive(0)
            if initial["protocolVersion"] != protocol:
                return None
            send({"method": "notifications/initialized"})
            result = {"initialize": initial}
            identifier = 1
            for method, key in [
                ("tools/list", "tools"),
                ("prompts/list", "prompts"),
                ("resources/list", "resources"),
                ("resources/templates/list", "resourceTemplates"),
            ]:
                entries = []
                cursor = None
                while True:
                    send({"id": identifier, "method": method, "params": {"cursor": cursor} if cursor else {}})
                    page = receive(identifier)
                    identifier += 1
                    entries.extend(page[key])
                    cursor = page.get("nextCursor")
                    if not cursor:
                        break
                result[method] = {key: entries}
            return result
        finally:
            lazy._stop_child(child)


def main():
    modes = (
        ["standard", "legacy"]
        if PACKAGE == "avito_mcp"
        else (["public", "account"] if PACKAGE == "wildberries_mcp" else ["public"])
    )
    directory = REPO / "src" / PACKAGE / "catalogs"
    directory.mkdir(exist_ok=True)
    for mode in modes:
        protocols = {}
        for protocol in lazy.PROTOCOLS:
            result = capture(protocol, mode)
            if result is not None:
                protocols[protocol] = result
        if not protocols:
            raise RuntimeError("No compatible handshake protocols")
        catalog = {
            "versions": lazy.schema_versions(),
            "source_digest": lazy.source_digest(),
            "protocols": protocols,
        }
        target = directory / (
            mode + "-" + catalog["versions"]["mcp"] + "-" + catalog["versions"]["pydantic"] + ".json"
        )
        target.write_text(json.dumps(catalog, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        print(f"{PACKAGE}: {target.name}, {len(protocols)} protocols", flush=True)


if __name__ == "__main__":
    main()
