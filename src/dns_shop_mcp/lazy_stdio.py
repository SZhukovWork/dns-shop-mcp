"""Standalone lazy MCP entry point. Standard library only until first real use.

Bundled catalogs are bound to source and schema-library versions. Unknown or
modified installations use the original server, never a guessed tool schema.
"""

from __future__ import annotations

import hashlib
import json
import os
import queue
import signal
import subprocess
import sys
import threading
import time
from importlib.metadata import PackageNotFoundError, version
from pathlib import Path

PACKAGE = __package__
ROOT = Path(__file__).resolve().parent
PROTOCOLS = ("2024-11-05", "2025-03-26", "2025-06-18", "2025-11-25")


def schema_versions():
    return {name: version(name) for name in ("mcp", "mcp-types", "pydantic")}


def source_digest():
    digest = hashlib.sha256()
    for path in sorted(ROOT.rglob("*.py")):
        digest.update(path.relative_to(ROOT).as_posix().encode())
        digest.update(path.read_bytes())
    return digest.hexdigest()


def catalog_mode():
    if PACKAGE == "wildberries_mcp":
        return "account" if os.environ.get("WB_ACCOUNT") == "1" else "public"
    if PACKAGE == "avito_mcp":
        return os.environ.get("AVITO_TOOLSET", "standard")
    return "public"


def needs_background_resume():
    """Inspect task metadata only, never credentials or the account API."""
    if PACKAGE != "avito_mcp" or os.environ.get("AVITO_TRANSPORT", "mobile") != "mobile":
        return False
    session = Path(os.environ.get("AVITO_SESSION_FILE", "~/.local/share/avito-mcp/session.json")).expanduser()
    directory = session.parent / (session.stem + "-outbox") / "reply-tasks"
    try:
        if directory.is_symlink() or directory.parent.is_symlink():
            return True  # Original server validates unsafe paths itself.
        for path in directory.glob("*.json"):
            if path.is_symlink():
                return True
            state = json.loads(path.read_text())
            if state.get("stage") == "working" and state.get("expires", float("inf")) > time.time():
                return True
        return False
    except (OSError, ValueError, TypeError, AttributeError):
        return True  # Never hide a recovery/validation error behind lazy discovery.


def load_catalog():
    try:
        versions = schema_versions()
        path = (
            ROOT
            / "catalogs"
            / (catalog_mode() + "-" + versions["mcp"] + "-" + versions["pydantic"] + ".json")
        )
        catalog = json.loads(path.read_text())
        if catalog["versions"] != versions or catalog["source_digest"] != source_digest():
            return None
        return catalog
    except (OSError, ValueError, KeyError, PackageNotFoundError):
        return None


def eager_main():
    if PACKAGE == "avito_mcp":
        from .cli import main
    else:
        from .server import main
    main()


def _read_lines(stream, events, source):
    try:
        for line in iter(stream.readline, b""):
            events.put((source, line))
    finally:
        events.put((source, None))


def _stop_child(child):
    try:
        child.stdin.close()
    except (OSError, ValueError):
        pass
    try:
        child.wait(timeout=2)
    except subprocess.TimeoutExpired:
        if os.name == "posix":
            try:
                os.killpg(child.pid, signal.SIGTERM)
            except ProcessLookupError:
                pass
        else:
            child.terminate()
        try:
            child.wait(timeout=2)
        except subprocess.TimeoutExpired:
            if os.name == "posix":
                try:
                    os.killpg(child.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
            else:
                child.kill()
            child.wait()
    if os.name == "posix":
        # A launcher may exit before a browser descendant does.
        try:
            os.killpg(child.pid, signal.SIGTERM)
        except ProcessLookupError:
            pass
    child.stdout.close()


def serve(catalog):
    events = queue.Queue()
    threading.Thread(target=_read_lines, args=(sys.stdin.buffer, events, "client"), daemon=True).start()
    child = None
    initialization = None
    expected_init = None
    replaying = False
    deadline = None
    pending = []

    def emit(message):
        sys.stdout.buffer.write((json.dumps(message, ensure_ascii=False) + "\n").encode())
        sys.stdout.buffer.flush()

    def forward(message):
        child.stdin.write((json.dumps(message, ensure_ascii=False) + "\n").encode())
        child.stdin.flush()

    def reply(message, result):
        emit({"jsonrpc": "2.0", "id": message["id"], "result": result})

    def start():
        nonlocal child, replaying, deadline
        child = subprocess.Popen(
            [sys.executable, "-m", PACKAGE, *sys.argv[1:]],
            env={**os.environ, "MCP_EAGER_START": "1"},
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            start_new_session=(os.name == "posix"),
        )
        threading.Thread(target=_read_lines, args=(child.stdout, events, "backend"), daemon=True).start()
        if initialization:
            replaying = True
            deadline = time.monotonic() + 60
            forward(initialization)

    try:
        while True:
            if deadline is not None and time.monotonic() >= deadline:
                raise RuntimeError("Backend startup timed out; queued requests were not sent")
            try:
                source, line = events.get(timeout=0.2)
            except queue.Empty:
                continue
            if line is None:
                if source == "backend" and replaying:
                    raise RuntimeError("Backend exited during initialization; queued requests were not sent")
                return
            if not line.strip():
                continue
            message = json.loads(line)
            if source == "backend":
                if replaying and "method" not in message and message.get("id") == initialization["id"]:
                    if message.get("result") != expected_init:
                        raise RuntimeError(
                            "Backend handshake changed; regenerate catalogs. Queued requests were not sent"
                        )
                    replaying = False
                    deadline = None
                    forward({"jsonrpc": "2.0", "method": "notifications/initialized"})
                    for item in pending:
                        forward(item)
                    pending.clear()
                else:
                    emit(message)
                continue
            if child:
                if replaying and "method" in message:
                    pending.append(message)
                else:
                    forward(message)
                continue
            method = message.get("method")
            params = message.get("params") or {}
            protocol = params.get("protocolVersion")
            entry = catalog["protocols"].get(protocol) if isinstance(protocol, str) else None
            if method == "initialize" and not initialization and entry and "id" in message:
                initialization = message
                expected_init = entry["initialize"]
                reply(message, expected_init)
            elif initialization and method == "notifications/initialized":
                pass
            elif initialization and method == "ping" and "id" in message:
                reply(message, {})
            elif (
                initialization
                and method in catalog["protocols"][expected_init["protocolVersion"]]
                and method != "initialize"
                and not params.get("cursor")
                and "id" in message
            ):
                reply(message, catalog["protocols"][expected_init["protocolVersion"]][method])
            else:
                start()
                if replaying:
                    pending.append(message)
                else:
                    forward(message)
    finally:
        if child:
            _stop_child(child)


def main():
    if os.name == "posix" and os.getpriority(os.PRIO_PROCESS, 0) < 0:
        os.setpriority(os.PRIO_PROCESS, 0, 0)
    server_args = not sys.argv[1:] or (PACKAGE == "avito_mcp" and sys.argv[1:] == ["serve"])
    if not server_args or os.environ.get("MCP_EAGER_START") == "1" or needs_background_resume():
        eager_main()
        return
    catalog = load_catalog()
    if catalog is None:
        eager_main()
        return

    def stop(_signum, _frame):
        raise SystemExit(0)

    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)
    try:
        serve(catalog)
    except (OSError, ValueError, RuntimeError) as error:
        print(f"{PACKAGE}: {error}", file=sys.stderr)
        raise SystemExit(1) from None


if __name__ == "__main__":
    main()
