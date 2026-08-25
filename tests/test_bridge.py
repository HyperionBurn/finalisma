"""TDD tests for weft_mcp.bridge — universal adapters for non-MCP hosts.

Covers: WebhookBridge, PollingBridge, ClipboardBridge, HttpBridgeClient,
actor-auth binding, signature verification, one-use enforcement.
"""

from __future__ import annotations
from tests._server_readiness import await_serving as _await_serving

import hashlib
import hmac
import json
import socket
import sys
import tempfile
import threading
import time
import unittest
import uuid
from unittest import mock
from http.client import HTTPConnection
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlsplit

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from weft_mcp.core import WeftStore, WeftError
from weft_mcp.bridge import (
    WebhookBridge,
    PollingBridge,
    ClipboardBridge,
    HttpBridgeClient,
    MAX_SAFE_INTEGER,
    BridgeAuthError,
    BridgeSignatureError,
    _now_epoch,
    _validate_webhook_url,
)


def _make_store(tmpdir):
    root = Path(tmpdir)
    return WeftStore(root / "state.db", root, require_actor_auth=True)


def _register_agent(store, team_id, agent_id, actor_token=None):
    return store.register_agent(
        team_id=team_id,
        agent_id=agent_id,
        actor_token=actor_token,
    )


class WebhookBridgeTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.store = _make_store(self.temp.name)
        self.bridge = WebhookBridge(self.store, allow_local_webhooks=True)
        cred = _register_agent(self.store, "team-1", "agent-1")
        self.actor_token = cred["actor_token"]

    def tearDown(self):
        self.store.close()
        self.temp.cleanup()

    def test_register_webhook_stores_sha256_only(self):
        result = self.bridge.register_webhook(
            team_id="team-1",
            agent_id="agent-1",
            url="https://example.com/hook",
            secret_ref="whsec_test_secret_value",
            actor_token=self.actor_token,
        )
        self.assertIn("webhook_id", result)
        # Secret must not be returned
        self.assertNotIn("secret_ref", result)
        self.assertNotIn("whsec_test_secret_value", json.dumps(result))
        # Verify DB stores SHA-256, not plaintext
        with self.store._read() as conn:
            row = conn.execute("SELECT secret_hash FROM bridge_webhooks WHERE webhook_id=?", (result["webhook_id"],)).fetchone()
            expected_hash = hashlib.sha256(b"whsec_test_secret_value").hexdigest()
            self.assertEqual(row["secret_hash"], expected_hash)

    def test_register_webhook_requires_actor_auth(self):
        with self.assertRaises(BridgeAuthError):
            self.bridge.register_webhook(
                team_id="team-1",
                agent_id="agent-1",
                url="https://example.com/hook",
                secret_ref="whsec_test",
                actor_token="wrong-token",
            )

    def test_register_webhook_rejects_bad_url(self):
        with self.assertRaises(WeftError):
            self.bridge.register_webhook(
                team_id="team-1",
                agent_id="agent-1",
                url="ftp://example.com/hook",
                secret_ref="whsec_test",
                actor_token=self.actor_token,
            )
        secure_bridge = WebhookBridge(self.store)
        with self.assertRaises(WeftError):
            secure_bridge.register_webhook(
                team_id="team-1",
                agent_id="agent-1",
                url="http://127.0.0.1:8080/hook",
                secret_ref="whsec_test",
                actor_token=self.actor_token,
            )

    def test_webhook_url_rejects_non_global_ranges_and_malformed_input(self):
        cases = [
            "http://127.0.0.1:8080/hook",
            "http://10.0.0.1/hook",
            "http://169.254.169.254/hook",
            "http://100.64.0.1/hook",
            "http://[::1]/hook",
            "http://[fd00::1]/hook",
            "http://[::ffff:127.0.0.1]/hook",
            "http://[::ffff:100.64.0.1]/hook",
            "http://[::1/hook",
            "http://example.com:bad/hook",
            " http://example.com/hook",
        ]
        for url in cases:
            with self.subTest(url=url):
                with self.assertRaises(WeftError) as raised:
                    _validate_webhook_url(url, allow_local=False, resolve=False)
                self.assertEqual(raised.exception.code, "invalid_argument")

    def test_deliver_pins_the_validated_dns_address(self):
        received = {}

        class HookHandler(BaseHTTPRequestHandler):
            def do_POST(self):
                length = int(self.headers.get("Content-Length", 0))
                received["body"] = json.loads(self.rfile.read(length))
                received["host"] = self.headers.get("Host")
                self.send_response(204)
                self.send_header("Content-Length", "0")
                self.end_headers()

            def log_message(self, *args):
                pass

        server = ThreadingHTTPServer(("127.0.0.1", 0), HookHandler)
        port = server.server_address[1]
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        _await_serving(server)
        connect_calls = []

        def connect_to_receiver(address, timeout=None, source_address=None):
            connect_calls.append(address)
            sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            sock.settimeout(timeout)
            if source_address:
                sock.bind(source_address)
            sock.connect(("127.0.0.1", port))
            return sock

        try:
            reg = self.bridge.register_webhook(
                team_id="team-1",
                agent_id="agent-1",
                url=f"http://rebind.test:{port}/hook",
                secret_ref="whsec_signing_secret",
                actor_token=self.actor_token,
            )
            with mock.patch(
                "weft_mcp.bridge.socket.getaddrinfo",
                return_value=[(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.216.34", port))],
            ), mock.patch("weft_mcp.bridge.socket.create_connection", side_effect=connect_to_receiver):
                result = self.bridge.deliver(
                    webhook_id=reg["webhook_id"],
                    event={"kind": "dns-pinned"},
                    signing_secret="whsec_signing_secret",
                )
            self.assertTrue(result["delivered"])
            self.assertEqual(connect_calls, [("93.184.216.34", port)])
            self.assertEqual(received["body"], {"kind": "dns-pinned"})
            self.assertEqual(received["host"], f"rebind.test:{port}")
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=5)

    def test_deliver_rejects_non_2xx_without_following_redirect(self):
        redirect_source = {"count": 0}
        redirected = {"count": 0}

        class RedirectHandler(BaseHTTPRequestHandler):
            def do_POST(self):
                redirect_source["count"] += 1
                self.send_response(302)
                self.send_header(
                    "Location",
                    f"http://127.0.0.1:{target_server.server_address[1]}/should-not-receive",
                )
                self.send_header("Content-Length", "0")
                self.end_headers()

            def log_message(self, *args):
                pass

        class TargetHandler(BaseHTTPRequestHandler):
            def do_POST(self):
                redirected["count"] += 1
                self.send_response(204)
                self.send_header("Content-Length", "0")
                self.end_headers()

            def log_message(self, *args):
                pass

        redirect_server = ThreadingHTTPServer(("127.0.0.1", 0), RedirectHandler)
        target_server = ThreadingHTTPServer(("127.0.0.1", 0), TargetHandler)
        redirect_thread = threading.Thread(target=redirect_server.serve_forever, daemon=True)
        target_thread = threading.Thread(target=target_server.serve_forever, daemon=True)
        redirect_thread.start()
        _await_serving(redirect_server)
        target_thread.start()
        _await_serving(target_server)
        try:
            port = redirect_server.server_address[1]
            reg = self.bridge.register_webhook(
                team_id="team-1",
                agent_id="agent-1",
                url=f"http://127.0.0.1:{port}/hook",
                secret_ref="whsec_signing_secret",
                actor_token=self.actor_token,
            )
            result = self.bridge.deliver(
                webhook_id=reg["webhook_id"],
                event={"kind": "redirect"},
                signing_secret="whsec_signing_secret",
            )
            self.assertFalse(result["delivered"])
            self.assertTrue(result["error"])
            self.assertEqual(redirect_source["count"], 1)
            self.assertEqual(redirected["count"], 0)
        finally:
            redirect_server.shutdown()
            target_server.shutdown()
            redirect_server.server_close()
            target_server.server_close()
            redirect_thread.join(timeout=5)
            target_thread.join(timeout=5)

    def test_deliver_emits_signed_event(self):
        # Spin up a local HTTP server to receive webhook
        received = {}

        class HookHandler(BaseHTTPRequestHandler):
            def do_POST(self):
                length = int(self.headers.get("Content-Length", 0))
                body = self.rfile.read(length)
                received["body"] = json.loads(body)
                received["sig"] = self.headers.get("X-Weft-Signature")
                received["ts"] = self.headers.get("X-Weft-Timestamp")
                self.send_response(200)
                self.send_header("Content-Length", "0")
                self.end_headers()

            def log_message(self, *args):
                pass

        server = ThreadingHTTPServer(("127.0.0.1", 0), HookHandler)
        host, port = server.server_address
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        _await_serving(server)

        try:
            reg = self.bridge.register_webhook(
                team_id="team-1",
                agent_id="agent-1",
                url=f"http://{host}:{port}/hook",
                secret_ref="whsec_signing_secret",
                actor_token=self.actor_token,
            )
            event = {"kind": "task.dispatch", "payload": {"title": "hello"}, "agent_id": "agent-1"}
            result = self.bridge.deliver(
                webhook_id=reg["webhook_id"],
                event=event,
                signing_secret="whsec_signing_secret",
            )
            self.assertTrue(result["delivered"])

            # Wait for the POST to arrive
            deadline = time.monotonic() + 3.0
            while "body" not in received and time.monotonic() < deadline:
                time.sleep(0.05)
            self.assertIn("body", received)

            # Verify signature
            sig = received["sig"]
            self.assertIsNotNone(sig)
            self.assertTrue(sig.startswith("sha256="))

            # Verify the signature matches
            ts = received["ts"]
            payload = received["body"]
            signed_payload = f"{ts}.{json.dumps(payload, sort_keys=True, separators=(',', ':'))}"
            expected_sig = "sha256=" + hmac.new(
                b"whsec_signing_secret", signed_payload.encode("utf-8"), hashlib.sha256
            ).hexdigest()
            self.assertEqual(sig, expected_sig)
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=5)

    def test_deliver_without_secret_fails_closed(self):
        # HIGH-1 security defect: deliver() must NOT fall back to signing with the
        # stored SHA-256 hash. Anyone with DB read access could forge signatures.
        # A signing path that works without the real secret is the bug.
        reg = self.bridge.register_webhook(
            team_id="team-1",
            agent_id="agent-1",
            url="https://example.com/hook",
            secret_ref="whsec_signing_secret",
            actor_token=self.actor_token,
        )
        event = {"kind": "task.dispatch", "payload": {"title": "hello"}, "agent_id": "agent-1"}
        with self.assertRaises(WeftError):
            self.bridge.deliver(
                webhook_id=reg["webhook_id"],
                event=event,
            )

    def test_verify_signature_rejects_tampered(self):
        self.assertFalse(
            self.bridge.verify_signature(
                secret="secret",
                signature="sha256=deadbeef",
                timestamp="1000",
                body={"x": 1},
            )
        )

    def test_verify_signature_rejects_expired_timestamp(self):
        old_ts = str(int(time.time()) - 600)  # 10 minutes ago
        body = {"x": 1}
        signed_payload = f"{old_ts}.{json.dumps(body, sort_keys=True, separators=(',', ':'))}"
        sig = "sha256=" + hmac.new(b"secret", signed_payload.encode(), hashlib.sha256).hexdigest()
        self.assertFalse(
            self.bridge.verify_signature(
                secret="secret",
                signature=sig,
                timestamp=old_ts,
                body=body,
                max_age_seconds=60,
            )
        )

    def test_verify_signature_accepts_valid(self):
        ts = str(int(time.time()))
        body = {"x": 1}
        signed_payload = f"{ts}.{json.dumps(body, sort_keys=True, separators=(',', ':'))}"
        sig = "sha256=" + hmac.new(b"secret", signed_payload.encode(), hashlib.sha256).hexdigest()
        self.assertTrue(
            self.bridge.verify_signature(
                secret="secret",
                signature=sig,
                timestamp=ts,
                body=body,
                max_age_seconds=60,
            )
        )


class PollingBridgeTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.store = _make_store(self.temp.name)
        self.bridge = PollingBridge(self.store)
        cred = _register_agent(self.store, "team-1", "agent-1")
        self.actor_token = cred["actor_token"]

    def tearDown(self):
        self.store.close()
        self.temp.cleanup()

    def test_get_pending_returns_events_since_cursor(self):
        # Enqueue events for agent-1
        self.bridge.enqueue(team_id="team-1", agent_id="agent-1", event={"kind": "test.1", "payload": {}}, actor_token=self.actor_token)
        self.bridge.enqueue(team_id="team-1", agent_id="agent-1", event={"kind": "test.2", "payload": {}}, actor_token=self.actor_token)

        result = self.bridge.get_pending(team_id="team-1", agent_id="agent-1", cursor=0, actor_token=self.actor_token)
        self.assertEqual(len(result["events"]), 2)
        self.assertEqual(result["events"][0]["kind"], "test.1")
        self.assertGreater(result["next_cursor"], 0)

    def test_get_pending_respects_cursor(self):
        self.bridge.enqueue(team_id="team-1", agent_id="agent-1", event={"kind": "test.1", "payload": {}}, actor_token=self.actor_token)
        self.bridge.enqueue(team_id="team-1", agent_id="agent-1", event={"kind": "test.2", "payload": {}}, actor_token=self.actor_token)

        first = self.bridge.get_pending(team_id="team-1", agent_id="agent-1", cursor=0, actor_token=self.actor_token)
        second = self.bridge.get_pending(team_id="team-1", agent_id="agent-1", cursor=first["next_cursor"], actor_token=self.actor_token)
        self.assertEqual([event["seq"] for event in second["events"]], [1, 2])
        self.bridge.ack(
            team_id="team-1",
            agent_id="agent-1",
            event_ids=[event["event_id"] for event in second["events"]],
            actor_token=self.actor_token,
        )
        third = self.bridge.get_pending(
            team_id="team-1",
            agent_id="agent-1",
            cursor=second["next_cursor"],
            actor_token=self.actor_token,
        )
        self.assertEqual(len(third["events"]), 0)

    def test_out_of_order_ack_does_not_skip_unacked_event(self):
        self.bridge.enqueue(team_id="team-1", agent_id="agent-1", event={"kind": "test.1", "payload": {}}, actor_token=self.actor_token)
        self.bridge.enqueue(team_id="team-1", agent_id="agent-1", event={"kind": "test.2", "payload": {}}, actor_token=self.actor_token)

        first = self.bridge.get_pending(
            team_id="team-1", agent_id="agent-1", cursor=0, actor_token=self.actor_token,
        )
        ack_result = self.bridge.ack(
            team_id="team-1",
            agent_id="agent-1",
            event_ids=[first["events"][1]["event_id"]],
            actor_token=self.actor_token,
        )
        self.assertEqual(ack_result["cursor"], 0)

        resumed = self.bridge.get_pending(
            team_id="team-1",
            agent_id="agent-1",
            cursor=first["next_cursor"],
            actor_token=self.actor_token,
        )
        self.assertEqual([event["seq"] for event in resumed["events"]], [1])

    def test_ack_checkpoints_cursor(self):
        self.bridge.enqueue(team_id="team-1", agent_id="agent-1", event={"kind": "test.1", "payload": {}}, actor_token=self.actor_token)
        result = self.bridge.get_pending(team_id="team-1", agent_id="agent-1", cursor=0, actor_token=self.actor_token)
        event_id = result["events"][0]["event_id"]

        ack_result = self.bridge.ack(team_id="team-1", agent_id="agent-1", event_ids=[event_id], actor_token=self.actor_token)
        self.assertTrue(ack_result["ok"])
        self.assertEqual(ack_result["cursor"], 1)

        self.store.close()
        self.store = _make_store(self.temp.name)
        self.bridge = PollingBridge(self.store)
        resumed = self.bridge.get_pending(
            team_id="team-1",
            agent_id="agent-1",
            cursor=0,
            actor_token=self.actor_token,
        )
        self.assertEqual(resumed["events"], [])
        self.assertEqual(resumed["next_cursor"], 1)
        next_event = self.bridge.enqueue(
            team_id="team-1",
            agent_id="agent-1",
            event={"kind": "test.2", "payload": {}},
            actor_token=self.actor_token,
        )
        self.assertEqual(next_event["seq"], 2)
        duplicate_ack = self.bridge.ack(
            team_id="team-1",
            agent_id="agent-1",
            event_ids=[next_event["event_id"], next_event["event_id"], "bo_unknown"],
            actor_token=self.actor_token,
        )
        self.assertEqual(duplicate_ack["acked"], [next_event["event_id"]])
        self.assertEqual(duplicate_ack["acked_count"], 1)
        self.assertEqual(duplicate_ack["cursor"], 2)

    def test_future_cursor_is_rejected_after_all_events_are_acknowledged(self):
        event = self.bridge.enqueue(
            team_id="team-1",
            agent_id="agent-1",
            event={"kind": "test.future", "payload": {}},
            actor_token=self.actor_token,
        )
        self.bridge.ack(
            team_id="team-1",
            agent_id="agent-1",
            event_ids=[event["event_id"]],
            actor_token=self.actor_token,
        )
        with self.assertRaises(WeftError) as ctx:
            self.bridge.get_pending(
                team_id="team-1",
                agent_id="agent-1",
                cursor=2,
                actor_token=self.actor_token,
            )
        self.assertEqual(ctx.exception.code, "invalid_cursor")

    def test_cursor_above_safe_integer_limit_is_rejected_before_sqlite(self):
        for cursor in (MAX_SAFE_INTEGER + 1, 2**63):
            with self.subTest(cursor=cursor):
                with self.assertRaises(WeftError) as ctx:
                    self.bridge.get_pending(
                        team_id="team-1",
                        agent_id="agent-1",
                        cursor=cursor,
                        actor_token=self.actor_token,
                    )
                self.assertEqual(ctx.exception.code, "invalid_cursor")

    def test_enqueue_stops_at_safe_integer_sequence_limit(self):
        with self.store._transaction() as conn:
            conn.execute(
                """
                INSERT INTO bridge_outbox(event_id, team_id, agent_id, event_json, seq, created_at)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    "bo_safe_limit_seed",
                    "team-1",
                    "agent-1",
                    json.dumps({"kind": "seed"}),
                    MAX_SAFE_INTEGER - 1,
                    "2026-08-20T00:00:00+00:00",
                ),
            )

        at_limit = self.bridge.enqueue(
            team_id="team-1",
            agent_id="agent-1",
            event={"kind": "test.safe-limit", "payload": {}},
            actor_token=self.actor_token,
        )
        self.assertEqual(at_limit["seq"], MAX_SAFE_INTEGER)
        with self.assertRaises(WeftError) as ctx:
            self.bridge.enqueue(
                team_id="team-1",
                agent_id="agent-1",
                event={"kind": "test.beyond-safe-limit", "payload": {}},
                actor_token=self.actor_token,
            )
        self.assertEqual(ctx.exception.code, "invalid_cursor")

    def test_unacked_events_replay_until_acknowledged(self):
        self.bridge.enqueue(team_id="team-1", agent_id="agent-1", event={"kind": "test.1", "payload": {}}, actor_token=self.actor_token)
        first = self.bridge.get_pending(team_id="team-1", agent_id="agent-1", cursor=0, actor_token=self.actor_token)
        replay = self.bridge.get_pending(
            team_id="team-1",
            agent_id="agent-1",
            cursor=first["next_cursor"],
            actor_token=self.actor_token,
        )
        self.assertEqual(
            [event["event_id"] for event in replay["events"]],
            [first["events"][0]["event_id"]],
        )

    def test_acked_events_are_not_redelivered(self):
        self.bridge.enqueue(team_id="team-1", agent_id="agent-1", event={"kind": "test.1", "payload": {}}, actor_token=self.actor_token)
        first = self.bridge.get_pending(team_id="team-1", agent_id="agent-1", cursor=0, actor_token=self.actor_token)
        event_id = first["events"][0]["event_id"]
        # Ack it
        self.bridge.ack(team_id="team-1", agent_id="agent-1", event_ids=[event_id], actor_token=self.actor_token)
        # Get pending again — should not re-deliver
        second = self.bridge.get_pending(team_id="team-1", agent_id="agent-1", cursor=0, actor_token=self.actor_token)
        delivered_ids = [e["event_id"] for e in second["events"] if not e.get("acked")]
        self.assertNotIn(event_id, delivered_ids)

    def test_enqueue_requires_actor_auth(self):
        with self.assertRaises(BridgeAuthError):
            self.bridge.enqueue(team_id="team-1", agent_id="agent-1", event={"kind": "x"}, actor_token="bad")

    def test_get_pending_requires_actor_auth(self):
        with self.assertRaises(BridgeAuthError):
            self.bridge.get_pending(team_id="team-1", agent_id="agent-1", cursor=0, actor_token="bad")


class ClipboardBridgeTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.store = _make_store(self.temp.name)
        self.bridge = ClipboardBridge(self.store)
        cred = _register_agent(self.store, "team-1", "agent-1")
        self.actor_token = cred["actor_token"]

    def tearDown(self):
        self.store.close()
        self.temp.cleanup()

    def test_generate_bootstrap_snippet(self):
        pairing = self.store.create_pairing(
            initiator_id="agent-1",
            team_id="team-1",
            capabilities_offered=["read"],
            actor_token=self.actor_token,
        )
        snippet = self.bridge.generate_bootstrap(
            team_id="team-1",
            agent_id="agent-1",
            endpoint="http://127.0.0.1:8787",
            pairing_id=pairing["pairing_id"],
            join_token=pairing["join_token"],
            actor_token=self.actor_token,
        )
        self.assertIn("endpoint", snippet)
        self.assertIn("pairing_id", snippet)
        self.assertIn("join_token", snippet)
        self.assertIn("consent_contract", snippet)
        self.assertEqual(snippet["team_id"], "team-1")

    def test_generate_rejects_unusable_endpoint_urls(self):
        bad_endpoints = (
            "https://",
            "https://user:password@example.com",
            "https://example.com#fragment",
            "https://example.com:bad",
            "https://example.com:0",
            " https://example.com",
            "https://example.com/path with spaces",
        )
        for endpoint in bad_endpoints:
            with self.subTest(endpoint=endpoint):
                with self.assertRaises(WeftError) as ctx:
                    self.bridge.generate_bootstrap(
                        team_id="team-1",
                        agent_id="agent-1",
                        endpoint=endpoint,
                        pairing_id="pair_xxx",
                        join_token="tok_xxx",
                        actor_token=self.actor_token,
                    )
                self.assertEqual(ctx.exception.code, "invalid_argument")

    def test_parse_valid_bootstrap(self):
        pairing = self.store.create_pairing(
            initiator_id="agent-1",
            team_id="team-1",
            capabilities_offered=["read"],
            actor_token=self.actor_token,
        )
        snippet = self.bridge.generate_bootstrap(
            team_id="team-1",
            agent_id="agent-1",
            endpoint="http://127.0.0.1:8787",
            pairing_id=pairing["pairing_id"],
            join_token=pairing["join_token"],
            actor_token=self.actor_token,
        )
        parsed = self.bridge.parse_bootstrap(json.dumps(snippet))
        self.assertEqual(parsed["pairing_id"], pairing["pairing_id"])

    def test_parse_rejects_token_in_url(self):
        bad_snippet = {
            "endpoint": "http://127.0.0.1:8787",
            "team_id": "team-1",
            "pairing_id": "pair_abc",
            "join_token": "fst_actor_xxx",
            "join_url": "http://127.0.0.1:8787/v1/join/pair_abc#token=fst_actor_xxx",
        }
        # The snippet itself carries the token in a field, but a URL with token in path is rejected
        bad_snippet2 = {
            "endpoint": "http://127.0.0.1:8787",
            "team_id": "team-1",
            "pairing_id": "pair_abc",
            "join_token": "fst_actor_xxx",
            "join_url": "http://127.0.0.1:8787/v1/join/fst_actor_xxx",
        }
        with self.assertRaises(WeftError):
            self.bridge.parse_bootstrap(json.dumps(bad_snippet2))

    def test_one_use_enforcement(self):
        pairing = self.store.create_pairing(
            initiator_id="agent-1",
            team_id="team-1",
            capabilities_offered=["read"],
            actor_token=self.actor_token,
        )
        snippet = self.bridge.generate_bootstrap(
            team_id="team-1",
            agent_id="agent-1",
            endpoint="http://127.0.0.1:8787",
            pairing_id=pairing["pairing_id"],
            join_token=pairing["join_token"],
            actor_token=self.actor_token,
        )
        # First parse succeeds
        self.bridge.parse_bootstrap(json.dumps(snippet))
        # Second parse of the same nonce fails (one-use)
        with self.assertRaises(WeftError):
            self.bridge.parse_bootstrap(json.dumps(snippet))

    def test_parse_rejects_missing_or_non_string_nonce(self):
        pairing = self.store.create_pairing(
            initiator_id="agent-1",
            team_id="team-1",
            capabilities_offered=["read"],
            actor_token=self.actor_token,
        )
        snippet = self.bridge.generate_bootstrap(
            team_id="team-1",
            agent_id="agent-1",
            endpoint="http://127.0.0.1:8787",
            pairing_id=pairing["pairing_id"],
            join_token=pairing["join_token"],
            actor_token=self.actor_token,
        )
        for nonce_value in (None, 123):
            stripped = dict(snippet)
            if nonce_value is None:
                stripped.pop("_nonce")
            else:
                stripped["_nonce"] = nonce_value
            with self.assertRaises(WeftError) as ctx:
                self.bridge.parse_bootstrap(json.dumps(stripped))
            self.assertEqual(ctx.exception.code, "invalid_argument")

    def test_generate_requires_actor_auth(self):
        with self.assertRaises(BridgeAuthError):
            self.bridge.generate_bootstrap(
                team_id="team-1",
                agent_id="agent-1",
                endpoint="http://127.0.0.1:8787",
                pairing_id="pair_xxx",
                join_token="tok_xxx",
                actor_token="invalid",
            )


class HttpBridgeClientTests(unittest.TestCase):
    """Test HttpBridgeClient against the real server handler."""

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        from weft_mcp.server import WeftDispatcher, _MCPRequestHandler, _Metrics, _WindowRateLimiter
        self.store = _make_store(self.temp.name)
        self.dispatcher = WeftDispatcher(self.store)
        cred = _register_agent(self.store, "team-1", "agent-1")
        self.actor_token = cred["actor_token"]

        # Build a handler class
        handler = type("BridgeTestHandler", (_MCPRequestHandler,), {})
        handler.dispatcher = self.dispatcher
        handler.token = None
        handler.allowed_origins = {"http://127.0.0.1"}
        handler.rate_limiter = _WindowRateLimiter()
        handler.mcp_rate_limiter = _WindowRateLimiter(limit=120, window_seconds=60, max_concurrent=16)
        handler.metrics = _Metrics()

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
        self.host, self.port = self.server.server_address
        self.base_url = f"http://{self.host}:{self.port}"
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        _await_serving(self.server)
        self.client = HttpBridgeClient(base_url=self.base_url, timeout=5)

    def tearDown(self):
        self.client.close()
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=5)
        self.store.close()
        self.temp.cleanup()

    def test_preview_link(self):
        pairing = self.store.create_pairing(
            initiator_id="agent-1",
            team_id="team-1",
            capabilities_offered=["read"],
            actor_token=self.actor_token,
        )
        join_url = pairing["join_url"]
        preview = self.client.preview_link(join_url)
        self.assertIn("pairing", preview)
        self.assertEqual(preview["action"], "consent_then_join")

    def test_join_link_with_consent(self):
        pairing = self.store.create_pairing(
            initiator_id="agent-1",
            team_id="team-1",
            capabilities_offered=["read"],
            actor_token=self.actor_token,
        )
        join_url = pairing["join_url"]
        result = self.client.join_link(
            join_url=join_url,
            agent_id="agent-2",
            consent=True,
        )
        self.assertIn("session_token", result)

    def test_join_link_rejects_string_consent(self):
        pairing = self.store.create_pairing(
            initiator_id="agent-1",
            team_id="team-1",
            capabilities_offered=["read"],
            actor_token=self.actor_token,
        )
        join_url = pairing["join_url"]
        with self.assertRaises(WeftError):
            self.client.join_link(
                join_url=join_url,
                agent_id="agent-2",
                consent="yes",  # type: ignore
            )

    def test_send_and_poll_events(self):
        pairing = self.store.create_pairing(
            initiator_id="agent-1",
            team_id="team-1",
            capabilities_offered=["read"],
            actor_token=self.actor_token,
        )
        # agent-1's session token (initiator) is in the pairing result
        initiator_session_token = pairing["initiator_session_token"]
        join_result = self.store.join_pairing(
            token=pairing["join_token"],
            agent_id="agent-2",
            consent=True,
        )
        agent2_session_token = join_result["session_token"]

        # Send via client as agent-1 (initiator)
        send_result = self.client.send_event(
            session_token=initiator_session_token,
            agent_id="agent-1",
            kind="task.dispatch",
            payload={"title": "hello"},
            idempotency_key=f"idem-{uuid.uuid4().hex}",
            actor_token=self.actor_token,
        )
        # Response may nest the event under "event" key
        has_seq = "seq" in send_result or ("event" in send_result and "seq" in send_result.get("event", {}))
        self.assertTrue(has_seq)

        # Poll via client as agent-2
        poll_result = self.client.poll_events(
            session_token=agent2_session_token,
            agent_id="agent-2",
            after_seq=0,
        )
        self.assertGreaterEqual(len(poll_result["events"]), 1)

    def test_ack_events(self):
        pairing = self.store.create_pairing(
            initiator_id="agent-1",
            team_id="team-1",
            capabilities_offered=["read"],
            actor_token=self.actor_token,
        )
        initiator_session_token = pairing["initiator_session_token"]
        join_result = self.store.join_pairing(
            token=pairing["join_token"],
            agent_id="agent-2",
            consent=True,
        )
        agent2_session_token = join_result["session_token"]
        self.client.send_event(
            session_token=initiator_session_token,
            agent_id="agent-1",
            kind="test",
            payload={},
            idempotency_key=f"idem-{uuid.uuid4().hex}",
            actor_token=self.actor_token,
        )
        ack_result = self.client.ack(session_token=agent2_session_token, agent_id="agent-2", seq=1)
        # ack response varies — just verify it doesn't error and returns a dict
        self.assertIsInstance(ack_result, dict)

    def test_token_in_fragment_not_in_path(self):
        # A URL with token in fragment should work
        pairing = self.store.create_pairing(
            initiator_id="agent-1",
            team_id="team-1",
            capabilities_offered=["read"],
            actor_token=self.actor_token,
        )
        join_url = pairing["join_url"]
        # Fragment form: http://host/v1/join/pair_xxx#token=yyy
        self.assertIn("#token=", join_url)

        # A URL with token in path should be rejected
        bad_url = join_url.split("#token=")[0] + "#token=" + pairing["join_token"]
        # This is the correct form — now try a bad one
        bad_path_url = f"{self.base_url}/v1/join/{pairing['join_token']}"
        with self.assertRaises(WeftError):
            self.client.preview_link(bad_path_url)


class BridgeAuthPolicyTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.store = _make_store(self.temp.name)
        self.webhook = WebhookBridge(self.store)
        self.polling = PollingBridge(self.store)
        self.clipboard = ClipboardBridge(self.store)
        cred = _register_agent(self.store, "team-1", "agent-1")
        self.actor_token = cred["actor_token"]

    def tearDown(self):
        self.store.close()
        self.temp.cleanup()

    def test_all_bridges_bind_to_team_agent_and_actor(self):
        # Each bridge method must enforce (team_id, agent_id) + actor_token
        with self.assertRaises(BridgeAuthError):
            self.webhook.register_webhook(
                team_id="team-1",
                agent_id="agent-1",
                url="https://example.com/h",
                secret_ref="whsec_x",
                actor_token="invalid",
            )
        with self.assertRaises(BridgeAuthError):
            self.polling.enqueue(team_id="team-1", agent_id="agent-1", event={}, actor_token="invalid")
        with self.assertRaises(BridgeAuthError):
            self.polling.get_pending(team_id="team-1", agent_id="agent-1", cursor=0, actor_token="invalid")
        with self.assertRaises(BridgeAuthError):
            self.polling.ack(team_id="team-1", agent_id="agent-1", event_ids=[], actor_token="invalid")
        with self.assertRaises(BridgeAuthError):
            self.clipboard.generate_bootstrap(
                team_id="team-1",
                agent_id="agent-1",
                endpoint="http://127.0.0.1:8787",
                pairing_id="pair_x",
                join_token="tok_x",
                actor_token="invalid",
            )

    def test_webhook_secret_never_returned_after_registration(self):
        reg = self.webhook.register_webhook(
            team_id="team-1",
            agent_id="agent-1",
            url="https://example.com/h",
            secret_ref="whsec_my_secret_value_12345",
            actor_token=self.actor_token,
        )
        # The secret must not appear anywhere in the response
        serialized = json.dumps(reg)
        self.assertNotIn("whsec_my_secret_value_12345", serialized)
        self.assertNotIn("my_secret_value", serialized)


class BridgeInitTests(unittest.TestCase):
    def test_init_creates_schema(self):
        temp = tempfile.TemporaryDirectory()
        store = _make_store(temp.name)
        # init should be idempotent and create bridge tables
        from weft_mcp.bridge import init_bridge
        init_bridge(store)
        init_bridge(store)  # idempotent
        with store._read() as conn:
            tables = [row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table' AND name LIKE 'bridge_%'").fetchall()]
            self.assertIn("bridge_webhooks", tables)
            self.assertIn("bridge_outbox", tables)
            self.assertIn("bridge_cursors", tables)
            self.assertIn("bridge_bootstrap_nonces", tables)
        store.close()
        temp.cleanup()


if __name__ == "__main__":
    unittest.main()
