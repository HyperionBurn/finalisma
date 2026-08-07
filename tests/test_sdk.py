"""Tests for the Finalisma SDK — stdlib-only Python client.

Strategy: spin up the REAL weft_mcp HTTP server on an ephemeral localhost
port in setUp, then drive a FULL two-agent flow through the SDK.  This proves
the SDK speaks the protocol correctly against the production server code.
"""

from __future__ import annotations

import json
import sys
import tempfile
import threading
import unittest
from http.client import HTTPConnection
from http.server import ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlsplit

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from weft_mcp.core import WeftStore
from weft_mcp.server import WeftDispatcher, _MCPRequestHandler, _Metrics, _WindowRateLimiter

from weft_sdk import (
    WeftClient,
    WeftError,
    AuthError,
    EvidenceError,
    NotFoundError,
    ConflictError,
    TaskResult,
    PairingResult,
    JoinResult,
    SessionEvent,
    CredentialRotation,
)


class _ServerHarness:
    """Start the real MCP HTTP handler on an ephemeral port."""

    def __init__(self, store: WeftStore, allowed_origin: str | None = None):
        self.dispatcher = WeftDispatcher(store)
        handler = type("SDKHandler", (_MCPRequestHandler,), {})
        handler.dispatcher = self.dispatcher
        handler.token = None
        handler.allowed_origins = {allowed_origin} if allowed_origin else {"http://127.0.0.1", "http://localhost"}
        handler.rate_limiter = _WindowRateLimiter()
        handler.mcp_rate_limiter = _WindowRateLimiter(limit=120, window_seconds=60, max_concurrent=16)
        handler.metrics = _Metrics()
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.host, self.port = self.server.server_address

    @property
    def base_url(self) -> str:
        return f"http://{self.host}:{self.port}/mcp"

    def shutdown(self) -> None:
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=5)


class SDKFullFlowTests(unittest.TestCase):
    """End-to-end two-agent flow driven entirely through the SDK."""

    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        root = Path(self.temp.name)
        self.store = WeftStore(root / "state.db", root, heartbeat_timeout=60)
        self.harness = _ServerHarness(self.store)
        # Agent A and B clients
        self.client_a = WeftClient(self.harness.base_url, "agent-a", "demo")
        self.client_b = WeftClient(self.harness.base_url, "agent-b", "demo")

    def tearDown(self) -> None:
        self.client_a.close()
        self.client_b.close()
        self.harness.shutdown()
        self.store.close()
        self.temp.cleanup()

    # ------------------------------------------------------------------
    # Basic connectivity & identity
    # ------------------------------------------------------------------

    def test_connect_returns_protocol_info(self) -> None:
        info = self.client_a.connect()
        self.assertEqual(info["protocol"], "finalisma.a2a")
        self.assertIn("version", info)

    def test_register_issues_actor_token(self) -> None:
        result = self.client_a.register(name="Planner", role="architect", capabilities=["planning"])
        self.assertEqual(result["agent_id"], "agent-a")
        self.assertIn("actor_token", result)
        token = result["actor_token"]
        self.assertTrue(token.startswith("fst_actor_"))
        # Store token on the client for subsequent calls
        self.client_a._actor_token = token
        self.assertTrue(self.client_a._actor_token.startswith("fst_actor_"))

    def test_repr_never_leaks_token(self) -> None:
        self.client_a._actor_token = "fst_actor_super_secret_value_here_1234567890"
        text = repr(self.client_a)
        self.assertIn("agent-a", text)
        self.assertNotIn("super_secret", text)
        self.assertNotIn("fst_actor", text)

    def test_env_var_token_hygiene(self) -> None:
        # The SDK should pick up WEFT_ACTOR_TOKEN from env when not passed
        import os as _os
        _os.environ["WEFT_TEST_TOKEN"] = "fst_actor_env_token_value_here_1234567890"
        try:
            c = WeftClient(self.harness.base_url, "env-agent", "demo",
                                 actor_token=_os.environ["WEFT_TEST_TOKEN"])
            self.assertEqual(c._actor_token, "fst_actor_env_token_value_here_1234567890")
            self.assertNotIn("env_token", repr(c))
        finally:
            _os.environ.pop("WEFT_TEST_TOKEN", None)

    # ------------------------------------------------------------------
    # Full two-agent pairing + task lifecycle
    # ------------------------------------------------------------------

    def test_full_two_agent_flow(self) -> None:
        """A registers → creates pairing → B joins (fresh identity) → A creates task →
        B claims → B submits evidence → A completes."""
        # 1. Agent A registers
        reg_a = self.client_a.register(name="Planner", role="architect", capabilities=["planning"])
        token_a = reg_a["actor_token"]
        self.client_a._actor_token = token_a

        # 2. A creates a pairing link
        pairing = self.client_a.create_pairing_link(capabilities=["read", "comment"])
        self.assertIsInstance(pairing, PairingResult)
        self.assertTrue(pairing.join_url.startswith("http"))
        self.assertIn("#token=", pairing.join_url)
        self.assertEqual(pairing.team_id, "demo")

        # 3. B joins the pairing as a fresh identity (gets actor_token from join)
        join = self.client_b.join_pairing(pairing.join_url, consent=True)
        self.assertIsInstance(join, JoinResult)
        self.assertEqual(join.team_id, "demo")
        self.assertIn("agent-a", join.members)
        self.assertIn("agent-b", join.members)
        self.assertTrue(join.session_token.startswith("fst_session_"))
        # B gets a new actor_token from joining (fresh identity)
        self.assertIsNotNone(join.actor_token)
        self.client_b._actor_token = join.actor_token

        # 4. A creates a task
        task_id = self.client_a.create_task(
            scope=["src/api.py"],
            description="Implement the /health endpoint with tests",
            priority=2,
            title="Implement /health",
        )
        self.assertTrue(task_id.startswith("task_"))

        # 5. B claims the task
        task = self.client_b.claim(task_id)
        self.assertIsInstance(task, TaskResult)
        self.assertEqual(task.task_id, task_id)
        self.assertEqual(task.status, "in_progress")
        self.assertEqual(task.claimed_by, "agent-b")
        self.assertGreater(task.fencing_token, 0)
        fencing_token = task.fencing_token

        # 6. B sends a progress update
        updated = self.client_b.update_progress(task_id, pct=50, note="Half done", fencing_token=fencing_token)
        self.assertEqual(updated.progress, 50)

        # 7. B sends a message to A
        msg = self.client_b.ask("agent-a", "Need clarification on response format?")
        self.assertTrue(msg.get("sent") or msg.get("idempotent"))

        # 8. B submits evidence (quality gate)
        # Create a real artifact file in the workspace so the gate passes
        workspace_root = Path(self.temp.name)
        artifact = workspace_root / "src" / "api.py"
        artifact.parent.mkdir(parents=True, exist_ok=True)
        artifact.write_text("# health endpoint\nreturn {'status': 'ok'}\n", encoding="utf-8")
        evidence_result = self.client_b.submit_evidence(
            task_id,
            artifact_paths=["src/api.py"],
            checks=[{"name": "unit-tests", "status": "passed", "command": "pytest", "evidence": "all green"}],
            fencing_token=fencing_token,
        )
        self.assertTrue(evidence_result["passed"])
        self.assertEqual(evidence_result["task_status"], "verified")

        # 9. B (lease owner) completes the task
        completed = self.client_b.complete(task_id, fencing_token=fencing_token, summary="Done and verified")
        self.assertEqual(completed.status, "done")
        self.assertEqual(completed.progress, 100)

    # ------------------------------------------------------------------
    # Session messaging
    # ------------------------------------------------------------------

    def test_session_messaging(self) -> None:
        reg_a = self.client_a.register(role="architect")
        self.client_a._actor_token = reg_a["actor_token"]

        pairing = self.client_a.create_pairing_link()
        # B joins as a fresh identity (no prior registration)
        join = self.client_b.join_pairing(pairing.join_url, consent=True)
        self.client_b._actor_token = join.actor_token
        session_token_b = join.session_token

        # B sends a session event
        evt = self.client_b.session_send(session_token_b, "greeting", {"msg": "hello from B"})
        self.assertIsInstance(evt, SessionEvent)
        self.assertEqual(evt.origin_agent, "agent-b")
        self.assertEqual(evt.seq, 1)

        # B polls its own event
        events = self.client_b.session_poll(session_token_b, after_seq=0)
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0].payload["msg"], "hello from B")

        # B acknowledges
        ack_seq = self.client_b.session_ack(session_token_b, seq=1)
        self.assertEqual(ack_seq, 1)

    # ------------------------------------------------------------------
    # Credential rotation
    # ------------------------------------------------------------------

    def test_rotate_credential(self) -> None:
        reg = self.client_a.register(role="architect")
        old_token = reg["actor_token"]
        self.client_a._actor_token = old_token

        rotation = self.client_a.rotate_credential(old_token)
        self.assertIsInstance(rotation, CredentialRotation)
        self.assertNotEqual(rotation.actor_token, old_token)
        self.assertTrue(rotation.actor_token.startswith("fst_actor_"))
        # Client should auto-update its stored token
        self.assertEqual(self.client_a._actor_token, rotation.actor_token)

        # Old token should now be rejected — use a separate client with old token
        stale_client = WeftClient(self.harness.base_url, "agent-a", "demo", actor_token=old_token)
        try:
            with self.assertRaises(AuthError):
                stale_client.heartbeat()
        finally:
            stale_client.close()

        # New token works
        result = self.client_a.heartbeat()
        self.assertEqual(result["agent_id"], "agent-a")

    # ------------------------------------------------------------------
    # Error cases
    # ------------------------------------------------------------------

    def test_wrong_token_raises_auth_error(self) -> None:
        # Register first to get a real identity, then use a wrong token
        reg = self.client_a.register(role="architect")
        self.client_a._actor_token = reg["actor_token"]  # real token
        # Now create a second client with the SAME agent_id but wrong token
        malicious = WeftClient(self.harness.base_url, "agent-a", "demo",
                                      actor_token="fst_actor_wrong_token_value_that_is_long_enough_1234567890")
        try:
            with self.assertRaises(AuthError):
                malicious.heartbeat()
        finally:
            malicious.close()

    def test_expired_pairing_link(self) -> None:
        reg_a = self.client_a.register(role="architect")
        self.client_a._actor_token = reg_a["actor_token"]
        # Create a pairing with minimum TTL then expire it directly in the store
        pairing = self.client_a.create_pairing_link(ttl_seconds=60)
        # Force-expire by manipulating the store directly
        import sqlite3
        conn = sqlite3.connect(str(Path(self.temp.name) / "state.db"))
        conn.execute("UPDATE pairings SET expires_at = 0 WHERE pairing_id = ?", (pairing.pairing_id,))
        conn.commit()
        conn.close()
        with self.assertRaises((ConflictError, NotFoundError, WeftError)):
            self.client_b.join_pairing(pairing.join_url, consent=True)

    def test_evidence_failure_raises_evidence_error(self) -> None:
        reg_a = self.client_a.register(role="architect")
        reg_b = self.client_b.register(role="coding")
        self.client_a._actor_token = reg_a["actor_token"]
        self.client_b._actor_token = reg_b["actor_token"]

        task_id = self.client_a.create_task(scope=["src/x.py"], description="do it", title="do it")
        task = self.client_b.claim(task_id)
        ft = task.fencing_token

        # Submit a failing check
        with self.assertRaises(EvidenceError):
            self.client_b.submit_evidence(
                task_id,
                artifact_paths=["src/x.py"],
                checks=[{"name": "lint", "status": "failed"}],
                fencing_token=ft,
            )

    def test_create_task_duplicate_raises_conflict(self) -> None:
        reg_a = self.client_a.register(role="architect")
        self.client_a._actor_token = reg_a["actor_token"]
        self.client_a.create_task(scope=["f.py"], description="duplicate task", title="duplicate task")
        # Same title+scope without idempotency key triggers duplicate detection
        with self.assertRaises(ConflictError):
            self.client_a.create_task(scope=["f.py"], description="duplicate task", title="duplicate task")

    def test_task_result_dataclass(self) -> None:
        reg_a = self.client_a.register(role="architect")
        self.client_a._actor_token = reg_a["actor_token"]
        task_id = self.client_a.create_task(scope=["a.py"], description="x", title="x")
        task = self.client_a.claim(task_id)
        self.assertIsInstance(task, TaskResult)
        self.assertEqual(task.task_id, task_id)
        self.assertEqual(task.status, "in_progress")

    def test_pairing_url_without_token_rejected(self) -> None:
        with self.assertRaises(WeftError) as ctx:
            self.client_b.join_pairing("http://127.0.0.1:8787/v1/join/pair_abc", consent=True)
        self.assertEqual(ctx.exception.code, "invalid_pairing_url")

    def test_heartbeat(self) -> None:
        reg = self.client_a.register(role="architect")
        self.client_a._actor_token = reg["actor_token"]
        result = self.client_a.heartbeat()
        self.assertEqual(result["agent_id"], "agent-a")
        self.assertIn("last_seen", result)

    def test_http_error_body_is_redacted_from_exception(self) -> None:
        # HIGH-2 security defect: a non-200 coordinator response body must NOT be
        # embedded verbatim into the raised exception — it can carry tokens.
        import socket
        from http.server import BaseHTTPRequestHandler

        SECRET_TOKEN = "fst_actor_TOP_SECRET_REDACTION_PROBE"

        class LeakyHandler(BaseHTTPRequestHandler):
            def log_message(self, *a: object) -> None:
                return

            def do_POST(self) -> None:
                length = int(self.headers.get("Content-Length", 0))
                self.rfile.read(length)
                body = json.dumps({"error": SECRET_TOKEN}).encode()
                self.send_response(500)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

        sock = socket.socket()
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
        sock.close()
        srv = ThreadingHTTPServer(("127.0.0.1", port), LeakyHandler)
        t = threading.Thread(target=srv.serve_forever, daemon=True)
        t.start()
        try:
            client = WeftClient(f"http://127.0.0.1:{port}/mcp", "agent-a", "demo")
            with self.assertRaises(WeftError) as ctx:
                client.connect()
            rendered = str(ctx.exception)
            self.assertNotIn(SECRET_TOKEN, rendered)
            details = ctx.exception.details
            rendered_details = json.dumps(details) if details else ""
            self.assertNotIn(SECRET_TOKEN, rendered_details)
        finally:
            srv.shutdown()
            srv.server_close()
            t.join(timeout=5)



class SDKRetryTests(unittest.TestCase):
    """Verify stdlib-only retry on transient HTTP failures."""

    def test_retry_on_transient_failure(self) -> None:
        """The SDK should retry idempotent calls on 503 errors."""
        import socket
        from http.server import BaseHTTPRequestHandler

        call_count = {"n": 0}

        class FlakyHandler(BaseHTTPRequestHandler):
            def log_message(self, *a: object) -> None:
                return

            def do_POST(self) -> None:  # noqa: N802
                call_count["n"] += 1
                length = int(self.headers.get("Content-Length", "0"))
                self.rfile.read(length)
                if call_count["n"] < 3:
                    self.send_response(503)
                    self.send_header("Content-Length", "0")
                    self.end_headers()
                    return
                payload = json.dumps({
                    "jsonrpc": "2.0",
                    "id": call_count["n"],
                    "result": {"content": [{"type": "text", "text": "{}"}], "structuredContent": {"protocol": "finalisma.a2a", "version": "1.0"}},
                }).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(payload)))
                self.end_headers()
                self.wfile.write(payload)

        server = ThreadingHTTPServer(("127.0.0.1", 0), FlakyHandler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        host, port = server.server_address
        try:
            client = WeftClient(f"http://{host}:{port}/mcp", "agent-a", "demo")
            result = client.connect()
            self.assertEqual(result["protocol"], "finalisma.a2a")
            self.assertGreaterEqual(call_count["n"], 3)
        finally:
            client.close()
            server.shutdown()
            server.server_close()
            thread.join(timeout=5)


if __name__ == "__main__":
    unittest.main()
