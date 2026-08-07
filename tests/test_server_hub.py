from __future__ import annotations

import json
import sys
import tempfile
import threading
import time
import unittest
from http.client import HTTPConnection
from http.server import ThreadingHTTPServer
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from weft_mcp.core import WeftStore
from weft_mcp.server import (
    WeftDispatcher,
    _MCPRequestHandler,
    _Metrics,
    _ServerHubState,
    _TokenBucket,
    _WindowRateLimiter,
)


def _make_handler(dispatcher, token=None, origins=None):
    handler = type("ServerHubHandler", (_MCPRequestHandler,), {})
    handler.dispatcher = dispatcher
    handler.token = token
    handler.allowed_origins = origins or {"http://localhost"}
    handler.rate_limiter = _WindowRateLimiter()
    handler.mcp_rate_limiter = _WindowRateLimiter(limit=120, window_seconds=60, max_concurrent=16)
    handler.metrics = _Metrics()
    return handler


def _pair(dispatcher, team="team-h", initiator="hub-a", actor_token=None):
    if actor_token is not None:
        return dispatcher.call_tool("create_pairing", {
            "team_id": team, "initiator_id": initiator, "actor_token": actor_token,
        })
    return dispatcher.call_tool("create_pairing", {
        "team_id": team, "initiator_id": initiator,
    })


def _join(dispatcher, token, agent_id, team="team-h", actor_token=None):
    args = {"token": token, "agent_id": agent_id, "consent": True}
    if actor_token is not None:
        args["actor_token"] = actor_token
    return dispatcher.call_tool("join_pairing", args)


class TokenBucketTests(unittest.TestCase):
    def test_refills_over_time(self):
        bucket = _TokenBucket(rate=10, capacity=10)
        # Drain the bucket
        for _ in range(10):
            self.assertTrue(bucket.consume())
        self.assertFalse(bucket.consume())
        # Wait for refill
        time.sleep(0.2)
        self.assertTrue(bucket.consume())

    def test_thread_safe_under_contention(self):
        # 20 threads × 50 = 1000 total consume attempts; bucket capacity 1000.
        # Exactly 1000 should be consumed — no overselling, no lost tokens.
        bucket = _TokenBucket(rate=0, capacity=1000)
        consumed = threading.Lock()
        count = 0

        def worker():
            nonlocal count
            for _ in range(50):
                if bucket.consume():
                    with consumed:
                        count += 1

        threads = [threading.Thread(target=worker) for _ in range(20)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        # Exactly 1000 tokens consumed (20 threads × 50), none oversold
        self.assertEqual(count, 1000)
        # Bucket should now be empty
        self.assertFalse(bucket.consume())


class MultiClientSessionTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        root = Path(self.temp.name)
        self.store = WeftStore(root / "state.db", root)
        self.dispatcher = WeftDispatcher(self.store)
        self.dispatcher.call_tool("register_agent", {"team_id": "team-h", "agent_id": "hub-a"})
        self.dispatcher.call_tool("register_agent", {"team_id": "team-h", "agent_id": "hub-b"})

    def tearDown(self):
        self.store.close()
        self.temp.cleanup()

    def _create_session(self):
        pairing = _pair(self.dispatcher)
        joined = _join(self.dispatcher, pairing["join_token"], "hub-b")
        return pairing["initiator_session_token"], joined["session_token"]

    def test_concurrent_clients_isolated_cursors(self):
        token_a, token_b = self._create_session()
        # Send 20 events from hub-a
        for i in range(20):
            self.dispatcher.call_tool("session_send", {
                "session_token": token_a, "agent_id": "hub-a",
                "kind": "test.msg", "payload": {"i": i},
                "idempotency_key": f"aaa-key-{i:06d}-xyz",
            })
        # 8 concurrent pollers from hub-b
        errors = []
        results = []

        def poll_worker():
            try:
                result = self.dispatcher.call_tool("session_poll", {
                    "session_token": token_b, "agent_id": "hub-b",
                    "after_seq": 0, "limit": 5,
                })
                results.append(result)
            except Exception as exc:
                errors.append(exc)

        threads = [threading.Thread(target=poll_worker) for _ in range(8)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        self.assertEqual(errors, [])
        # Each poller should get exactly 5 events, all with seq > 0
        for result in results:
            self.assertEqual(len(result["events"]), 5)
            for event in result["events"]:
                self.assertGreater(event["seq"], 0)

    def test_concurrent_sends_advance_cursor_atomically(self):
        token_a, token_b = self._create_session()
        errors = []

        def sender(agent_id, token, prefix):
            try:
                for i in range(50):
                    self.dispatcher.call_tool("session_send", {
                        "session_token": token, "agent_id": agent_id,
                        "kind": "test.msg", "payload": {"i": i},
                        "idempotency_key": f"{prefix}-key-{i:06d}-xyz",
                    })
            except Exception as exc:
                errors.append(exc)

        t1 = threading.Thread(target=sender, args=("hub-a", token_a, "aaa"))
        t2 = threading.Thread(target=sender, args=("hub-b", token_b, "bbb"))
        t1.start()
        t2.start()
        t1.join()
        t2.join()
        self.assertEqual(errors, [])
        # Total events should be exactly 100 (no lost seqs)
        status = self.dispatcher.call_tool("session_status", {
            "session_token": token_a, "agent_id": "hub-a",
        })
        self.assertEqual(status["cursor_head"], 100)

    def test_ack_monotonic_under_concurrent_poll(self):
        token_a, token_b = self._create_session()
        for i in range(10):
            self.dispatcher.call_tool("session_send", {
                "session_token": token_a, "agent_id": "hub-a",
                "kind": "test.msg", "payload": {"i": i},
                "idempotency_key": f"aaa-key-{i:06d}-xyz",
            })
        # Concurrent acks from hub-b with increasing seq
        errors = []

        def ack_worker(seq):
            try:
                self.dispatcher.call_tool("session_ack", {
                    "session_token": token_b, "agent_id": "hub-b", "seq": seq,
                })
            except Exception as exc:
                errors.append(exc)

        threads = [threading.Thread(target=ack_worker, args=(i,)) for i in range(1, 11)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        self.assertEqual(errors, [])
        status = self.dispatcher.call_tool("session_status", {
            "session_token": token_b, "agent_id": "hub-b",
        })
        cursor = {c["agent_id"]: c["last_ack_seq"] for c in status["cursors"]}
        self.assertEqual(cursor["hub-b"], 10)


class ReconnectResilienceTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        root = Path(self.temp.name)
        self.store = WeftStore(root / "state.db", root)
        self.dispatcher = WeftDispatcher(self.store)
        self.dispatcher.call_tool("register_agent", {"team_id": "team-h", "agent_id": "hub-a"})
        self.dispatcher.call_tool("register_agent", {"team_id": "team-h", "agent_id": "hub-b"})

    def tearDown(self):
        self.store.close()
        self.temp.cleanup()

    def _create_session(self):
        pairing = _pair(self.dispatcher)
        joined = _join(self.dispatcher, pairing["join_token"], "hub-b")
        return pairing["initiator_session_token"], joined["session_token"]

    def test_reconnect_resumes_cursor_no_loss_no_dup(self):
        token_a, token_b = self._create_session()
        # Send 30 events
        for i in range(30):
            self.dispatcher.call_tool("session_send", {
                "session_token": token_a, "agent_id": "hub-a",
                "kind": "test.msg", "payload": {"i": i},
                "idempotency_key": f"aaa-key-{i:06d}-xyz",
            })
        # hub-b polls first 10, acks them
        first = self.dispatcher.call_tool("session_poll", {
            "session_token": token_b, "agent_id": "hub-b",
            "after_seq": 0, "limit": 10,
        })
        self.assertEqual(len(first["events"]), 10)
        self.assertEqual(first["events"][-1]["seq"], 10)
        self.dispatcher.call_tool("session_ack", {
            "session_token": token_b, "agent_id": "hub-b", "seq": 10,
        })
        # Simulate disconnect: reconnect and poll from last ack
        reconnected = self.dispatcher.call_tool("session_poll", {
            "session_token": token_b, "agent_id": "hub-b",
            "after_seq": 10, "limit": 20,
        })
        self.assertEqual(len(reconnected["events"]), 20)
        self.assertEqual(reconnected["events"][0]["seq"], 11)
        self.assertEqual(reconnected["events"][-1]["seq"], 30)
        # No duplicates on second poll
        again = self.dispatcher.call_tool("session_poll", {
            "session_token": token_b, "agent_id": "hub-b",
            "after_seq": 10, "limit": 20,
        })
        self.assertEqual(again["events"], reconnected["events"])

    def test_last_ack_reconciliation_on_resume(self):
        token_a, token_b = self._create_session()
        for i in range(5):
            self.dispatcher.call_tool("session_send", {
                "session_token": token_a, "agent_id": "hub-a",
                "kind": "test.msg", "payload": {"i": i},
                "idempotency_key": f"aaa-key-{i:06d}-xyz",
            })
        # hub-b acks seq 3
        self.dispatcher.call_tool("session_ack", {
            "session_token": token_b, "agent_id": "hub-b", "seq": 3,
        })
        # Reconnect: poll with after_seq=0 should return all 5, but last_ack_seq=3
        result = self.dispatcher.call_tool("session_poll", {
            "session_token": token_b, "agent_id": "hub-b",
            "after_seq": 0, "limit": 10,
        })
        self.assertEqual(len(result["events"]), 5)
        self.assertEqual(result["last_ack_seq"], 3)
        # Client can reconcile: skip already-acked events
        unacked = [e for e in result["events"] if e["seq"] > result["last_ack_seq"]]
        self.assertEqual(len(unacked), 2)
        self.assertEqual(unacked[0]["seq"], 4)


class IdleDisconnectTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        root = Path(self.temp.name)
        self.store = WeftStore(root / "state.db", root)
        self.dispatcher = WeftDispatcher(self.store)
        self.dispatcher.call_tool("register_agent", {"team_id": "team-h", "agent_id": "hub-a"})
        self.dispatcher.call_tool("register_agent", {"team_id": "team-h", "agent_id": "hub-b"})

    def tearDown(self):
        self.store.close()
        self.temp.cleanup()

    def _expire_all_sessions(self):
        """Force-expire all active sessions by setting expires_at in the past (test-only)."""
        import time
        past = time.time() - 60.0
        with self.store._transaction() as conn:
            conn.execute("UPDATE sessions SET expires_at = ? WHERE state = 'active'", (past,))
            conn.execute("UPDATE session_credentials SET expires_at = ?", (past,))

    def test_stale_session_marked_after_heartbeat_timeout(self):
        pairing = _pair(self.dispatcher)
        joined = _join(self.dispatcher, pairing["join_token"], "hub-b")
        token_a = joined["session_token"]
        # Session is active
        status = self.dispatcher.call_tool("session_status", {
            "session_token": token_a, "agent_id": "hub-b",
        })
        self.assertEqual(status["state"], "active")
        # Force session into expired state (simulates heartbeat timeout)
        self._expire_all_sessions()
        # Now a write operation should expire the stale session
        with self.assertRaises(Exception) as ctx:
            self.dispatcher.call_tool("session_send", {
                "session_token": token_a, "agent_id": "hub-b",
                "kind": "test.msg", "payload": {},
                "idempotency_key": "stale-test-01",
            })
        self.assertEqual(ctx.exception.code, "session_expired")

    def test_abandoned_session_cleanup_no_leak(self):
        pairing = _pair(self.dispatcher)
        joined = _join(self.dispatcher, pairing["join_token"], "hub-b")
        token_a = joined["session_token"]
        # Send some events then abandon
        for i in range(5):
            self.dispatcher.call_tool("session_send", {
                "session_token": token_a, "agent_id": "hub-b",
                "kind": "test.msg", "payload": {"i": i},
                "idempotency_key": f"bbb-key-{i:06d}-xyz",
            })
        # Force session into expired state
        self._expire_all_sessions()
        # Trigger a write to mark it expired in state
        with self.assertRaises(Exception):
            self.dispatcher.call_tool("session_send", {
                "session_token": token_a, "agent_id": "hub-b",
                "kind": "test.msg", "payload": {},
                "idempotency_key": "abandon-test-01",
            })
        # Prune expired sessions
        result = self.store.prune_expired(apply=True)
        self.assertTrue(result["applied"])
        # Verify session is expired (status read should fail)
        with self.assertRaises(Exception):
            self.dispatcher.call_tool("session_status", {
                "session_token": token_a, "agent_id": "hub-b",
            })


class ServerHubRateLimitingTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        root = Path(self.temp.name)
        self.store = WeftStore(root / "state.db", root)
        self.dispatcher = WeftDispatcher(self.store)
        self.dispatcher.call_tool("register_agent", {"team_id": "team-h", "agent_id": "hub-a"})
        self.dispatcher.call_tool("register_agent", {"team_id": "team-h", "agent_id": "hub-b"})

    def tearDown(self):
        self.store.close()
        self.temp.cleanup()

    def test_per_team_rate_limiter_blocks_excess(self):
        hub_state = _ServerHubState()
        team_key = "team-h"
        # Allow 10 requests per second
        limiter = hub_state.get_team_limiter(team_key, rate=10, capacity=10)
        # Consume all tokens
        for _ in range(10):
            self.assertTrue(limiter.consume())
        # Next should be blocked
        self.assertFalse(limiter.consume())

    def test_per_agent_rate_limiter_blocks_excess(self):
        hub_state = _ServerHubState()
        agent_key = "team-h:hub-a"
        limiter = hub_state.get_agent_limiter(agent_key, rate=5, capacity=5)
        for _ in range(5):
            self.assertTrue(limiter.consume())
        self.assertFalse(limiter.consume())

    def test_rate_limit_hits_tracked_in_metrics(self):
        hub_state = _ServerHubState()
        limiter = hub_state.get_team_limiter("team-x", rate=2, capacity=2)
        limiter.consume()
        limiter.consume()
        limiter.consume()  # blocked
        hub_state.record_rate_limit_hit("team-x", "hub-a")
        metrics = hub_state.metrics_snapshot()
        self.assertGreaterEqual(metrics["rate_limit_hits"], 1)


class ServerHubMetricsTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        root = Path(self.temp.name)
        self.store = WeftStore(root / "state.db", root)
        self.dispatcher = WeftDispatcher(self.store)
        self.dispatcher.call_tool("register_agent", {"team_id": "team-h", "agent_id": "hub-a"})
        self.dispatcher.call_tool("register_agent", {"team_id": "team-h", "agent_id": "hub-b"})

    def tearDown(self):
        self.store.close()
        self.temp.cleanup()

    def test_metrics_report_active_sessions_and_cursors(self):
        pairing = _pair(self.dispatcher)
        joined = _join(self.dispatcher, pairing["join_token"], "hub-b")
        token_a = pairing["initiator_session_token"]
        # Send events
        for i in range(5):
            self.dispatcher.call_tool("session_send", {
                "session_token": token_a, "agent_id": "hub-a",
                "kind": "test.msg", "payload": {"i": i},
                "idempotency_key": f"aaa-key-{i:06d}-xyz",
            })
        # Ack some
        self.dispatcher.call_tool("session_ack", {
            "session_token": joined["session_token"], "agent_id": "hub-b", "seq": 3,
        })
        hub_state = _ServerHubState()
        hub_state.record_active_session("sess-1", "team-h", cursor_head=5)
        hub_state.record_reconnect("sess-1")
        hub_state.record_reconnect("sess-1")
        metrics = hub_state.metrics_snapshot()
        self.assertEqual(metrics["active_sessions"], 1)
        self.assertEqual(metrics["reconnect_count"], 2)
        self.assertIn("sess-1", metrics["sessions"])
        self.assertEqual(metrics["sessions"]["sess-1"]["cursor_head"], 5)


class HTTPTransportHardeningTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        root = Path(self.temp.name)
        self.store = WeftStore(root / "state.db", root)
        self.dispatcher = WeftDispatcher(self.store)
        self.dispatcher.call_tool("register_agent", {"team_id": "team-h", "agent_id": "hub-a"})
        self.dispatcher.call_tool("register_agent", {"team_id": "team-h", "agent_id": "hub-b"})

    def tearDown(self):
        self.store.close()
        self.temp.cleanup()

    def test_concurrent_http_clients_session_send_poll_ack(self):
        pairing = _pair(self.dispatcher)
        joined = _join(self.dispatcher, pairing["join_token"], "hub-b")
        handler = _make_handler(self.dispatcher)
        server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            host, port = server.server_address
            token_a = pairing["initiator_session_token"]
            token_b = joined["session_token"]
            errors = []

            def http_client(agent_id, token, prefix):
                try:
                    for i in range(10):
                        # Send
                        body = json.dumps({
                            "jsonrpc": "2.0", "id": i + 1, "method": "tools/call",
                            "params": {
                                "name": "session_send",
                                "arguments": {
                                    "session_token": token, "agent_id": agent_id,
                                    "kind": "test.msg", "payload": {"i": i},
                                    "idempotency_key": f"{prefix}-key-{i:06d}-xyz",
                                },
                            },
                        })
                        conn = HTTPConnection(host, port, timeout=10)
                        conn.request("POST", "/mcp", body=body,
                                     headers={"Content-Type": "application/json", "Origin": "http://localhost"})
                        resp = conn.getresponse()
                        resp.read()
                        conn.close()
                        if resp.status != 200:
                            errors.append(f"send {prefix}-{i} status={resp.status}")
                        # Poll
                        body = json.dumps({
                            "jsonrpc": "2.0", "id": i + 100, "method": "tools/call",
                            "params": {
                                "name": "session_poll",
                                "arguments": {
                                    "session_token": token, "agent_id": agent_id,
                                    "after_seq": 0, "limit": 5,
                                },
                            },
                        })
                        conn = HTTPConnection(host, port, timeout=10)
                        conn.request("POST", "/mcp", body=body,
                                     headers={"Content-Type": "application/json", "Origin": "http://localhost"})
                        resp = conn.getresponse()
                        resp.read()
                        conn.close()
                        if resp.status != 200:
                            errors.append(f"poll {prefix}-{i} status={resp.status}")
                except Exception as exc:
                    errors.append(str(exc))

            threads = [
                threading.Thread(target=http_client, args=("hub-a", token_a, "a")),
                threading.Thread(target=http_client, args=("hub-b", token_b, "b")),
            ]
            for t in threads:
                t.start()
            for t in threads:
                t.join()
            self.assertEqual(errors, [])
            # Verify total events
            status = self.dispatcher.call_tool("session_status", {
                "session_token": token_a, "agent_id": "hub-a",
            })
            self.assertEqual(status["cursor_head"], 20)
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=5)

    def test_http_rate_limiting_returns_429(self):
        handler = _make_handler(self.dispatcher)
        # Set a very low rate limit
        handler.mcp_rate_limiter = _WindowRateLimiter(limit=2, window_seconds=60, max_concurrent=1)
        server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            host, port = server.server_address
            # Exhaust the limit
            for i in range(2):
                body = json.dumps({
                    "jsonrpc": "2.0", "id": i + 1, "method": "tools/call",
                    "params": {"name": "protocol", "arguments": {}},
                })
                conn = HTTPConnection(host, port, timeout=5)
                conn.request("POST", "/mcp", body=body,
                             headers={"Content-Type": "application/json", "Origin": "http://localhost"})
                resp = conn.getresponse()
                resp.read()
                conn.close()
                self.assertEqual(resp.status, 200)
            # Next request should be rate limited
            body = json.dumps({
                "jsonrpc": "2.0", "id": 3, "method": "tools/call",
                "params": {"name": "protocol", "arguments": {}},
            })
            conn = HTTPConnection(host, port, timeout=5)
            conn.request("POST", "/mcp", body=body,
                         headers={"Content-Type": "application/json", "Origin": "http://localhost"})
            resp = conn.getresponse()
            resp.read()
            conn.close()
            self.assertEqual(resp.status, 429)
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=5)


if __name__ == "__main__":
    unittest.main()
