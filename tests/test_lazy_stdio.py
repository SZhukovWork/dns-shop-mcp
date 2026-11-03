"""Exercise the real standalone entry point without network/account writes."""

import importlib
import json
import os
import queue
import subprocess
import sys
import threading
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
PACKAGE = next(path.name for path in (REPO / "src").iterdir() if path.name.endswith("_mcp"))
lazy = importlib.import_module(PACKAGE + ".lazy_stdio")


class Client:
    def __init__(self, tmp_path, eager=False, protocol="2025-06-18", mode=None, command=None):
        self.env = {
            **os.environ,
            "PYTHONPATH": str(REPO / "src"),
            "MCP_EAGER_START": "1" if eager else "0",
            "AVITO_SESSION_FILE": str(tmp_path / "session.json"),
            "AVITO_TOOLSET": mode or "standard",
            "AVITO_TRANSPORT": "mobile",
            "WB_ACCOUNT": "1" if mode == "account" else "0",
        }
        self.env.pop("AVITO_SESSION", None)
        self.process = subprocess.Popen(
            command or [sys.executable, "-m", PACKAGE],
            env=self.env,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        self.events = queue.Queue()
        threading.Thread(
            target=lazy._read_lines, args=(self.process.stdout, self.events, "stdout"), daemon=True
        ).start()
        self.identifier = 0
        self.protocol = protocol

    def send(self, message):
        self.process.stdin.write((json.dumps({"jsonrpc": "2.0", **message}) + "\n").encode())
        self.process.stdin.flush()

    def rpc(self, method, params=None):
        self.identifier += 1
        self.send({"id": self.identifier, "method": method, "params": params or {}})
        while True:
            _, line = self.events.get(timeout=20)
            assert line is not None, self.process.stderr.read().decode()
            result = json.loads(line)
            if result.get("id") == self.identifier and "method" not in result:
                return result

    def initialize(self):
        result = self.rpc(
            "initialize",
            {
                "protocolVersion": self.protocol,
                "capabilities": {"elicitation": {"form": {}, "url": {}}},
                "clientInfo": {"name": "standalone-test", "version": "1"},
            },
        )
        self.send({"method": "notifications/initialized"})
        return result

    def children(self):
        path = Path(f"/proc/{self.process.pid}/task/{self.process.pid}/children")
        return path.read_text().split() if path.exists() else None

    def close(self):
        self.process.stdin.close()
        try:
            self.process.wait(timeout=8)
        except subprocess.TimeoutExpired:
            self.process.kill()
            self.process.wait()
            pytest.fail("Standalone server did not stop on stdin EOF")
        self.process.stdout.close()
        stderr = self.process.stderr.read().decode()
        self.process.stderr.close()
        assert self.process.returncode == 0, stderr


@pytest.fixture(autouse=True)
def environment(monkeypatch, tmp_path):
    monkeypatch.setenv("AVITO_SESSION_FILE", str(tmp_path / "session.json"))
    monkeypatch.setenv("AVITO_TRANSPORT", "mobile")
    monkeypatch.setenv("AVITO_TOOLSET", "standard")
    monkeypatch.setenv("WB_ACCOUNT", "0")
    monkeypatch.delenv("AVITO_SESSION", raising=False)


def test_lightweight_import_and_bundled_catalog():
    script = (
        f"import {PACKAGE}.lazy_stdio as lazy,sys; "
        "assert not any(n in sys.modules for n in ['mcp','pydantic','playwright']); "
        "assert lazy.load_catalog() is not None"
    )
    subprocess.run(
        [sys.executable, "-c", script], check=True, env={**os.environ, "PYTHONPATH": str(REPO / "src")}
    )


@pytest.mark.parametrize("protocol", lazy.PROTOCOLS)
def test_discovery_equals_real_server_without_starting_backend(tmp_path, protocol):
    deferred = Client(tmp_path, protocol=protocol)
    eager = Client(tmp_path, eager=True, protocol=protocol)
    try:
        assert deferred.initialize() == eager.initialize()
        for method in ["tools/list", "resources/list", "resources/templates/list", "prompts/list", "ping"]:
            assert deferred.rpc(method) == eager.rpc(method)
        if deferred.children() is not None:
            assert deferred.children() == []
    finally:
        deferred.close()
        eager.close()


def test_first_call_activates_once_and_shutdown_cleans_backend(tmp_path):
    client = Client(tmp_path)
    children = []
    try:
        client.initialize()
        assert client.children() in (None, [])
        result = client.rpc("tools/call", {"name": "__not_a_real_tool__", "arguments": {}})
        assert result.get("result", {}).get("isError") or "error" in result
        children = client.children()
        if children is not None:
            assert len(children) == 1
        # Same backend remains; no tool call is replayed or retried.
        assert "result" in client.rpc("ping")
        assert client.children() == children
    finally:
        client.close()
    for pid in children or []:
        assert not Path("/proc", pid).exists()


def test_multiple_clients_keep_separate_backends(tmp_path):
    clients = [Client(tmp_path), Client(tmp_path)]
    try:
        for client in clients:
            client.initialize()
            client.rpc("tools/call", {"name": "__not_a_real_tool__", "arguments": {}})
        children = [c.children() for c in clients]
        if all(x is not None for x in children):
            assert len(set(children[0] + children[1])) == 2
    finally:
        for client in clients:
            client.close()


def test_catalog_fallback_for_modified_sources_or_dependencies(monkeypatch):
    assert lazy.load_catalog() is not None
    monkeypatch.setattr(lazy, "source_digest", lambda: "changed")
    assert lazy.load_catalog() is None


def test_unknown_protocol_uses_backend(tmp_path):
    client = Client(tmp_path, protocol="2099-01-01")
    try:
        result = client.initialize()
        assert "result" in result
        if client.children() is not None:
            assert len(client.children()) == 1
    finally:
        client.close()


@pytest.mark.skipif(PACKAGE not in {"avito_mcp", "wildberries_mcp"}, reason="No alternate toolset")
def test_alternate_toolset_catalog(tmp_path, monkeypatch):
    mode = "legacy" if PACKAGE == "avito_mcp" else "account"
    monkeypatch.setenv("AVITO_TOOLSET", mode if PACKAGE == "avito_mcp" else "standard")
    monkeypatch.setenv("WB_ACCOUNT", "1" if mode == "account" else "0")
    assert lazy.load_catalog() is not None
    client = Client(tmp_path, mode=mode)
    eager = Client(tmp_path, eager=True, mode=mode)
    try:
        assert client.initialize() == eager.initialize()
        assert client.rpc("tools/list") == eager.rpc("tools/list")
        assert client.children() in (None, [])
    finally:
        client.close()
        eager.close()


@pytest.mark.skipif(PACKAGE != "avito_mcp", reason="Avito durable background work")
def test_avito_pending_tasks_bypass_lazy_start(tmp_path, monkeypatch):
    directory = tmp_path / "session-outbox" / "reply-tasks"
    directory.mkdir(parents=True)
    task = directory / ("a" * 32 + ".json")
    assert not lazy.needs_background_resume()
    task.write_text(json.dumps({"stage": "working", "expires": 9999999999}))
    assert lazy.needs_background_resume()
    called = []
    monkeypatch.setattr(sys, "argv", ["avito-mcp"])
    monkeypatch.setattr(lazy, "eager_main", lambda: called.append(True))
    lazy.main()
    assert called == [True]
    task.write_text(json.dumps({"stage": "done", "expires": 9999999999}))
    assert not lazy.needs_background_resume()
    task.write_text("malformed")
    assert lazy.needs_background_resume()
