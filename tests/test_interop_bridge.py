"""Weft bridge-adapter interop unit tests.

Reuses the same end-to-end pattern as scripts/interop-validate-bridge.py:
spawn the REAL coordinator over stdio, register an agent through its MCP
surface, then exercise the REAL bridge adapters against the coordinator's
database. Happy paths + deliberate negative refusals.

These tests compose the real bridge classes from src/weft_mcp/bridge.py;
they do NOT fabricate state through core.py store methods.
"""

from __future__ import annotations

import json
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from weft_mcp.core import WeftStore, WeftError
from weft_mcp.bridge import (
    WebhookBridge,
    PollingBridge,
    ClipboardBridge,
    BridgeAuthError,
)


TEAM_ID = "demo"


class _WebhookReceiver:
    """Minimal HTTP server recording the last POST it received."""

    def __init__(self) -> None:
        self._handler = _make_handler()
        self._server = ThreadingHTTPServer(("127.0.0.1", 0), self._handler)
        self._port = self._server.server_address[1]
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)

    def start(self) -> None:
        self._thread.start()

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self._port}/hook"

    def wait_for_request(self, timeout: float = 5.0) -> dict | None:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if self._handler.last_request is not None:
                return self._handler.last_request
            time.sleep(0.05)
        return self._handler.last_request

    def stop(self) -> None:
        self._server.shutdown()
        self._server.server_close()


def _make_handler():
    class _Handler(BaseHTTPRequestHandler):
        last_request: dict | None = None

        def log_message(self, *args):
            pass

        def do_POST(self):
            length = int(self.headers.get("Content-Length", "0"))
            raw = self.rfile.read(length) if length else b""
            sig = self.headers.get("X-Weft-Signature", "")
            ts = self.headers.get("X-Weft-Timestamp", "")
            try:
                body = json.loads(raw.decode("utf-8")) if raw else None
            except json.JSONDecodeError:
                body = raw.decode("utf-8", errors="replace")
            _Handler.last_request = {"signature": sig, "timestamp": ts, "body": body}
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(b'{"ok":true}')

    return _Handler


class BridgeInteropTests(unittest.TestCase):
    """End-to-end bridge adapter tests against a real spawned coordinator."""

    proc: subprocess.Popen | None = None
    bridge_store: WeftStore | None = None
    webhook_receiver: _WebhookReceiver | None = None
    actor_token: str = ""
    _scratch: tempfile.TemporaryDirectory | None = None
    _request_id: int = 0

    @classmethod
    def setUpClass(cls) -> None:
        cls._scratch = tempfile.TemporaryDirectory(prefix="weft-interop-bridge-test-")
        workspace = Path(cls._scratch.name)
        state_path = workspace / ".weft" / "state.db"

        cls.proc = subprocess.Popen(
            [
                sys.executable,
                "-B",
                "scripts/weft-mcp.py",
                "--transport",
                "stdio",
                "--team-id",
                TEAM_ID,
                "--workspace",
                str(workspace),
                "--state",
                str(state_path),
            ],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            cwd=str(PROJECT_ROOT),
            encoding="utf-8",
            errors="replace",
        )

        # initialize handshake
        cls._rpc(1, "initialize", {"protocolVersion": "2025-03-26", "capabilities": {}})
        cls._rpc(2, "tools/list")

        # register agent
        reg = cls._call_tool(
            3,
            "register_agent",
            {"team_id": TEAM_ID, "agent_id": "bridge-agent", "role": "generalist", "name": "Bridge Agent"},
        )
        cls.actor_token = reg["actor_token"]

        # open bridge store against coordinator's DB
        cls.bridge_store = WeftStore(
            str(state_path),
            str(workspace),
            require_actor_auth=True,
        )

    @classmethod
    def tearDownClass(cls) -> None:
        if cls.bridge_store is not None:
            try:
                cls.bridge_store.close()
            except Exception:
                pass
        if cls.proc is not None:
            cls.proc.terminate()
            try:
                cls.proc.wait(timeout=10)
            except subprocess.TimeoutExpired:
                cls.proc.kill()
                cls.proc.wait(timeout=10)
        if cls._scratch is not None:
            cls._scratch.cleanup()

    def setUp(self) -> None:
        # fresh webhook receiver per test that needs one
        pass

    def tearDown(self) -> None:
        if self.webhook_receiver is not None:
            self.webhook_receiver.stop()
            self.webhook_receiver = None

    @classmethod
    def _rpc(cls, request_id: int, method: str, params: dict | None = None) -> dict:
        payload = {"jsonrpc": "2.0", "id": request_id, "method": method}
        if params is not None:
            payload["params"] = params
        line_out = json.dumps(payload, separators=(",", ":"))
        assert cls.proc is not None
        assert cls.proc.stdin is not None and cls.proc.stdout is not None
        cls.proc.stdin.write(line_out + "\n")
        cls.proc.stdin.flush()
        line = cls.proc.stdout.readline()
        if not line:
            raise RuntimeError("coordinator closed stdin without a reply")
        reply = json.loads(line)
        if "error" in reply:
            raise RuntimeError(f"JSON-RPC error {reply['error']}")
        return reply.get("result", {})

    @classmethod
    def _call_tool(cls, request_id: int, name: str, arguments: dict) -> dict:
        result = cls._rpc(request_id, "tools/call", {"name": name, "arguments": arguments})
        content = result.get("content") or []
        text = content[0].get("text", "") if content else ""
        if result.get("isError"):
            raise RuntimeError(f"{name} failed: {text}")
        try:
            return json.loads(text)
        except json.JSONDecodeError:
            return {"raw": text}

    def _next_pairing(self) -> tuple[str, str]:
        """Create a fresh pairing and return (pairing_id, join_token)."""
        self.__class__._request_id += 10
        rid = self.__class__._request_id
        pairing = self._call_tool(
            rid,
            "create_pairing",
            {
                "initiator_id": "bridge-agent",
                "team_id": TEAM_ID,
                "capabilities_offered": ["read"],
                "actor_token": self.actor_token,
            },
        )
        return pairing["pairing_id"], pairing["join_token"]

    # ------------------------------------------------------------------
    # ClipboardBridge
    # ------------------------------------------------------------------

    def test_clipboard_generate_and_parse(self) -> None:
        clipboard = ClipboardBridge(self.bridge_store)
        pairing_id, join_token = self._next_pairing()
        bootstrap = clipboard.generate_bootstrap(
            team_id=TEAM_ID,
            agent_id="bridge-agent",
            endpoint="http://127.0.0.1:8787",
            pairing_id=pairing_id,
            join_token=join_token,
            actor_token=self.actor_token,
        )
        self.assertEqual(bootstrap["team_id"], TEAM_ID)
        self.assertIn("_nonce", bootstrap)
        parsed = clipboard.parse_bootstrap(json.dumps(bootstrap))
        self.assertEqual(parsed["team_id"], TEAM_ID)

    def test_clipboard_one_shot_enforcement(self) -> None:
        """Second parse of the same bootstrap snippet is refused."""
        clipboard = ClipboardBridge(self.bridge_store)
        pairing_id, join_token = self._next_pairing()
        bootstrap = clipboard.generate_bootstrap(
            team_id=TEAM_ID,
            agent_id="bridge-agent",
            endpoint="http://127.0.0.1:8787",
            pairing_id=pairing_id,
            join_token=join_token,
            actor_token=self.actor_token,
        )
        raw = json.dumps(bootstrap)
        clipboard.parse_bootstrap(raw)  # first: ok
        with self.assertRaises(WeftError) as ctx:
            clipboard.parse_bootstrap(raw)  # second: refused
        self.assertEqual(ctx.exception.code, "bootstrap_reused")

    # ------------------------------------------------------------------
    # PollingBridge
    # ------------------------------------------------------------------

    def test_polling_enqueue_get_ack_no_repeat(self) -> None:
        polling = PollingBridge(self.bridge_store)
        enqueue = polling.enqueue(
            team_id=TEAM_ID,
            agent_id="bridge-agent",
            event={"kind": "t", "payload": {"v": 1}},
            actor_token=self.actor_token,
        )
        event_id = enqueue["event_id"]

        pending = polling.get_pending(TEAM_ID, "bridge-agent", cursor=0, actor_token=self.actor_token)
        self.assertEqual(len(pending["events"]), 1)
        self.assertEqual(pending["events"][0]["event_id"], event_id)
        next_cursor = pending["next_cursor"]

        polling.ack(TEAM_ID, "bridge-agent", [event_id], actor_token=self.actor_token)

        pending2 = polling.get_pending(TEAM_ID, "bridge-agent", cursor=next_cursor, actor_token=self.actor_token)
        self.assertEqual(len(pending2["events"]), 0)  # at-most-once

    def test_polling_wrong_token_refused(self) -> None:
        polling = PollingBridge(self.bridge_store)
        with self.assertRaises((WeftError, BridgeAuthError)):
            polling.get_pending(TEAM_ID, "bridge-agent", cursor=0, actor_token="not-a-valid-token")

    def test_polling_nonmember_refused(self) -> None:
        polling = PollingBridge(self.bridge_store)
        with self.assertRaises((WeftError, BridgeAuthError)):
            polling.get_pending(TEAM_ID, "ghost-agent", cursor=0, actor_token=self.actor_token)

    # ------------------------------------------------------------------
    # WebhookBridge
    # ------------------------------------------------------------------

    def test_webhook_register_deliver_verify(self) -> None:
        self.webhook_receiver = _WebhookReceiver()
        self.webhook_receiver.start()
        webhook = WebhookBridge(self.bridge_store)
        secret = "whsec_live_signing_secret_value_12345"

        wh = webhook.register_webhook(
            team_id=TEAM_ID,
            agent_id="bridge-agent",
            url=self.webhook_receiver.url,
            secret_ref=secret,
            actor_token=self.actor_token,
        )
        webhook_id = wh["webhook_id"]

        result = webhook.deliver(
            webhook_id=webhook_id,
            event={"kind": "alert", "payload": {"msg": "hi"}},
            signing_secret=secret,
        )
        self.assertTrue(result.get("delivered"))

        received = self.webhook_receiver.wait_for_request(timeout=5.0)
        self.assertIsNotNone(received)
        self.assertTrue(received["signature"].startswith("sha256="))

        # verify with real secret passes
        self.assertTrue(
            WebhookBridge.verify_signature(
                secret=secret,
                signature=received["signature"],
                timestamp=received["timestamp"],
                body=received["body"],
            )
        )
        # verify with wrong secret fails
        self.assertFalse(
            WebhookBridge.verify_signature(
                secret="wrong_secret",
                signature=received["signature"],
                timestamp=received["timestamp"],
                body=received["body"],
            )
        )

    def test_webhook_deliver_without_signing_secret_refused(self) -> None:
        """HIGH-1 fail-closed: deliver with no signing_secret is refused."""
        self.webhook_receiver = _WebhookReceiver()
        self.webhook_receiver.start()
        webhook = WebhookBridge(self.bridge_store)
        secret = "whsec_live_signing_secret_value_12345"
        wh = webhook.register_webhook(
            team_id=TEAM_ID,
            agent_id="bridge-agent",
            url=self.webhook_receiver.url,
            secret_ref=secret,
            actor_token=self.actor_token,
        )
        with self.assertRaises(WeftError) as ctx:
            webhook.deliver(wh["webhook_id"], {"kind": "x"}, signing_secret=None)
        self.assertEqual(ctx.exception.code, "signing_secret_required")


if __name__ == "__main__":
    unittest.main()
