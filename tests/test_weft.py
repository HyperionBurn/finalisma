from __future__ import annotations

import io
import json
import sys
import tempfile
import threading
import unittest
from concurrent.futures import ThreadPoolExecutor, TimeoutError as FutureTimeoutError
from http.client import HTTPConnection
from http.server import ThreadingHTTPServer
from pathlib import Path
from unittest.mock import patch
from urllib.parse import urlsplit

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from weft_mcp.core import WeftError, WeftStore
from weft_mcp.server import WeftDispatcher, _MCPRequestHandler, _Metrics, _WindowRateLimiter, handle_json_rpc, run_stdio


class WeftStoreTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.store = WeftStore(self.root / ".weft" / "state.db", self.root, heartbeat_timeout=30)
        self.store.register_agent("demo", "agent-a", "Planner", "architect", "gpt-5.6-luna", ["planning", "research"])
        self.store.register_agent("demo", "agent-b", "Builder", "coding", "qwen-3.8-max", ["coding", "testing"])

    def tearDown(self) -> None:
        self.store.close()
        self.temp.cleanup()

    def test_duplicate_and_idempotent_task_creation(self) -> None:
        first = self.store.create_task(
            "demo",
            "agent-a",
            "Implement API route",
            "Add the API route and tests",
            scope=["src/api.py"],
            idempotency_key="task-1",
        )
        self.assertTrue(first["created"])
        repeated = self.store.create_task(
            "demo",
            "agent-a",
            "Completely different title",
            "Ignored because the operation key is the same",
            idempotency_key="task-1",
        )
        self.assertTrue(repeated["idempotent"])
        self.assertEqual(first["task"]["task_id"], repeated["task"]["task_id"])
        duplicate = self.store.create_task(
            "demo",
            "agent-a",
            "Implement API route",
            "Add the API route and tests now",
            scope=["src/api.py"],
        )
        self.assertFalse(duplicate["created"])
        self.assertIn("duplicate", duplicate)

    def test_routed_task_waits_for_an_atomic_claim(self) -> None:
        routed = self.store.create_task("demo", "agent-a", "Review a coding change", "Inspect implementation and tests", preferred_agent="agent-b")
        self.assertEqual(routed["task"]["owner_id"], "agent-b")
        self.assertEqual(routed["task"]["status"], "pending")
        claimed = self.store.claim_task("demo", "agent-b", routed["task"]["task_id"])
        self.assertEqual(claimed["status"], "in_progress")
        self.assertGreater(claimed["fencing_token"], 0)

    def test_two_agents_cannot_claim_the_same_task(self) -> None:
        created = self.store.create_task("demo", "agent-a", "Implement coding endpoint", "Write code and tests", scope=["src/api.py"])
        task_id = created["task"]["task_id"]

        def claim(agent_id: str):
            try:
                return (agent_id, "ok", self.store.claim_task("demo", agent_id, task_id))
            except WeftError as exc:
                return (agent_id, exc.code, None)

        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(claim, ["agent-a", "agent-b"]))
        successes = [item for item in results if item[1] == "ok"]
        failures = [item for item in results if item[1] != "ok"]
        self.assertEqual(len(successes), 1)
        self.assertEqual(len(failures), 1)
        self.assertIn(failures[0][1], {"task_claim_conflict", "scope_lock_conflict"})
        self.assertGreater(successes[0][2]["fencing_token"], 0)

    def test_fencing_token_and_quality_gate(self) -> None:
        artifact = self.root / "artifact.txt"
        created = self.store.create_task("demo", "agent-a", "Publish artifact", "Create the artifact", scope=["artifact.txt"], preferred_agent="agent-b")
        claimed = self.store.claim_task("demo", "agent-b", created["task"]["task_id"])
        with self.assertRaises(WeftError) as stale:
            self.store.update_task("demo", "agent-b", claimed["task_id"], progress=20, fencing_token=claimed["fencing_token"] + 1)
        self.assertEqual(stale.exception.code, "stale_fencing_token")

        failed = self.store.verify_task(
            "demo", "agent-b", claimed["task_id"], claimed["fencing_token"], ["missing.txt"], [{"name": "tests", "status": "passed", "evidence": "unit tests"}]
        )
        self.assertFalse(failed["passed"])
        self.assertEqual(failed["task_status"], "review")

        artifact.write_text("Weft artifact\n", encoding="utf-8")
        passed = self.store.verify_task(
            "demo", "agent-b", claimed["task_id"], claimed["fencing_token"], ["artifact.txt"], [{"name": "tests", "status": "passed", "evidence": "unit tests pass"}]
        )
        self.assertTrue(passed["passed"])
        self.assertEqual(passed["task_status"], "verified")
        completed = self.store.complete_task("demo", "agent-b", claimed["task_id"], claimed["fencing_token"], "Artifact verified")
        self.assertEqual(completed["status"], "done")

    def test_fencing_token_fits_javascript_safe_integer(self) -> None:
        # Real MCP hosts (opencode, Claude Desktop, Cursor) are JavaScript/TypeScript
        # clients. JSON integers above 2^53-1 lose precision in transit, which
        # truncates the fencing token and causes every verify/complete to fail
        # with stale_fencing_token. This was found by a real-host validation on
        # 2026-08-05 (docs/INTEROP_VALIDATION_2026-08-05.md). Every issued token
        # must stay within the JS safe-integer range.
        max_safe = 2**53 - 1
        for i in range(32):
            with self.subTest(i=i):
                scope = [f"p{i}.txt"]
                created = self.store.create_task("demo", "agent-a", f"Token probe {i}", "probe", scope=scope, preferred_agent="agent-b")
                claimed = self.store.claim_task("demo", "agent-b", created["task"]["task_id"])
                token = claimed["fencing_token"]
                self.assertLessEqual(token, max_safe, f"fencing token {token} exceeds JS safe integer {max_safe}")
                self.assertGreater(token, 0)

    def test_message_idempotency_and_broadcast_ack(self) -> None:
        first = self.store.send_message("demo", "agent-a", "question", {"ask": "ready?"}, recipient_id="agent-b", idempotency_key="msg-1")
        repeated = self.store.send_message("demo", "agent-a", "question", {"ask": "different but ignored"}, recipient_id="agent-b", idempotency_key="msg-1")
        self.assertTrue(first["sent"])
        self.assertTrue(repeated["idempotent"])
        self.assertEqual(first["message"]["message_id"], repeated["message"]["message_id"])
        broadcast = self.store.send_message("demo", "agent-a", "team.notice", {"note": "hello"})
        self.assertTrue(broadcast["sent"])
        inbox = self.store.read_inbox("demo", "agent-b", acknowledge=False)
        self.assertEqual(inbox["count"], 2)
        self.assertEqual(inbox["messages"][0]["protocol"], "weft.a2a")
        acknowledged = self.store.read_inbox("demo", "agent-b", acknowledge=True)
        self.assertEqual(acknowledged["count"], 2)
        self.assertEqual(self.store.read_inbox("demo", "agent-b")["count"], 0)

    def test_team_boundaries_prevent_cross_team_reads_and_writes(self) -> None:
        self.store.register_agent("other", "agent-b", "Other Builder", "coding", "longcat/LongCat-2.0", ["coding"])
        self.store.send_message("demo", "agent-a", "team.notice", {"private": "demo"}, recipient_id="agent-b", idempotency_key="demo-msg-1")
        other_inbox = self.store.read_inbox("other", "agent-b", acknowledge=False)
        self.assertEqual(other_inbox["count"], 0)
        with self.assertRaises(WeftError) as unknown_agent:
            self.store.send_message("demo", "agent-c", "team.notice", {"private": "spoof"}, recipient_id="agent-a")
        self.assertEqual(unknown_agent.exception.code, "agent_not_registered")
        with self.assertRaises(WeftError) as wrong_team:
            self.store.read_inbox("other", "agent-a")
        self.assertEqual(wrong_team.exception.code, "agent_not_registered")

    def test_workspace_containment_and_secret_gate(self) -> None:
        with self.assertRaises(WeftError) as outside:
            self.store.create_task("demo", "agent-a", "Escape", "No", scope=["..\\outside.txt"])
        self.assertEqual(outside.exception.code, "path_outside_workspace")
        secret = self.root / "secret.txt"
        secret.write_text("-----BEGIN PRIVATE KEY-----\nnot-real\n", encoding="utf-8")
        created = self.store.create_task("demo", "agent-a", "Review secret", "Scan", scope=["secret.txt"], preferred_agent="agent-b")
        claimed = self.store.claim_task("demo", "agent-b", created["task"]["task_id"])
        result = self.store.verify_task("demo", "agent-b", claimed["task_id"], claimed["fencing_token"], ["secret.txt"], [{"name": "scan", "status": "passed", "evidence": "claimed pass"}])
        self.assertFalse(result["passed"])
        self.assertEqual(result["details"]["secret_scan"]["status"], "failed")

    def test_deeply_nested_payload_is_rejected_without_internal_error(self) -> None:
        nested: dict[str, object] = {}
        for _ in range(1_100):
            nested = {"next": nested}
        with self.assertRaises(WeftError) as invalid:
            self.store.send_message("demo", "agent-a", "payload.test", nested, idempotency_key="deep-payload-1")
        self.assertEqual(invalid.exception.code, "invalid_json")

    def test_pairing_link_consumption_and_member_bound_session(self) -> None:
        pairing = self.store.create_pairing("agent-a", "demo", capabilities_offered=["read", "comment"], invitee_hint="agent-b")
        parsed_join_url = urlsplit(pairing["join_url"])
        self.assertIn(f"/v1/join/{pairing['pairing_id']}", parsed_join_url.path)
        self.assertTrue(parsed_join_url.fragment.startswith("token="))
        self.assertNotIn(pairing["join_token"], parsed_join_url.path)
        self.assertEqual(pairing["bootstrap_prompt"].count(pairing["join_token"]), 1)
        raw_db = self.store.state_path.read_bytes()
        self.assertNotIn(pairing["join_token"].encode("utf-8"), raw_db)
        self.assertNotIn(pairing["initiator_session_token"].encode("utf-8"), raw_db)
        preview = self.store.pairing_preview(pairing["join_token"])
        self.assertEqual(preview["status"], "issued")
        with self.assertRaises(WeftError) as no_consent:
            self.store.join_pairing(pairing["join_token"], "agent-b", model="longcat/LongCat-2.0")
        self.assertEqual(no_consent.exception.code, "consent_required")
        joined = self.store.join_pairing(pairing["join_token"], "agent-b", model="longcat/LongCat-2.0", consent=True)
        self.assertEqual(joined["state"], "active")
        with self.assertRaises(WeftError) as replay:
            self.store.join_pairing(pairing["join_token"], "agent-c", consent=True)
        self.assertEqual(replay.exception.code, "pairing_unavailable")

        sent_a = self.store.session_send(pairing["initiator_session_token"], "agent-a", "task.dispatch", {"task": "pairing"}, "pairing-event-1")
        sent_a_repeat = self.store.session_send(pairing["initiator_session_token"], "agent-a", "task.dispatch", {"task": "different-but-idempotent"}, "pairing-event-1")
        self.assertTrue(sent_a["sent"])
        self.assertTrue(sent_a_repeat["idempotent"])
        self.assertEqual(sent_a["event"]["seq"], sent_a_repeat["event"]["seq"])
        inbox_b = self.store.session_poll(joined["session_token"], "agent-b", after_seq=0)
        self.assertEqual([event["seq"] for event in inbox_b["events"]], [1])
        self.assertEqual(inbox_b["events"][0]["origin_agent"], "agent-a")
        self.store.session_ack(joined["session_token"], "agent-b", 1)
        with self.assertRaises(WeftError) as spoof:
            self.store.session_poll(joined["session_token"], "agent-a", after_seq=0)
        self.assertEqual(spoof.exception.code, "session_forbidden")
        self.store.close_session(pairing["initiator_session_token"], "agent-a")
        with self.assertRaises(WeftError) as closed:
            self.store.session_send(joined["session_token"], "agent-b", "task.progress", {"progress": 1}, "pairing-event-2")
        self.assertEqual(closed.exception.code, "session_closed")

    def test_session_sequence_is_total_ordered_under_concurrency(self) -> None:
        pairing = self.store.create_pairing("agent-a", "demo")
        self.store.join_pairing(pairing["join_token"], "agent-b", consent=True)

        def send(index: int):
            return self.store.session_send(pairing["initiator_session_token"], "agent-a", "task.progress", {"index": index}, f"concurrent-{index}")["event"]["seq"]

        with ThreadPoolExecutor(max_workers=5) as pool:
            sequences = list(pool.map(send, range(5)))
        self.assertEqual(sorted(sequences), [1, 2, 3, 4, 5])
        self.assertEqual(self.store.session_wait(pairing["initiator_session_token"], "agent-a", after_seq=5, timeout_seconds=0)["timed_out"], True)

    def test_session_poll_is_safe_for_concurrent_readers(self) -> None:
        pairing = self.store.create_pairing("agent-a", "demo")
        joined = self.store.join_pairing(pairing["join_token"], "agent-b", consent=True)
        self.store.session_send(pairing["initiator_session_token"], "agent-a", "task.handoff", {"task": "readers"}, "reader-event-1")

        def poll():
            return self.store.session_poll(joined["session_token"], "agent-b", after_seq=0)["events"][0]["seq"]

        with ThreadPoolExecutor(max_workers=8) as pool:
            sequences = list(pool.map(lambda _: poll(), range(8)))
        self.assertEqual(sequences, [1] * 8)

    def test_pairing_consumption_is_atomic_under_concurrent_joins(self) -> None:
        pairing = self.store.create_pairing("agent-a", "demo")

        def join(index: int):
            try:
                result = self.store.join_pairing(pairing["join_token"], f"joiner-{index}", consent=True)
                return ("ok", result["session_id"])
            except WeftError as exc:
                return (exc.code, None)

        with ThreadPoolExecutor(max_workers=5) as pool:
            results = list(pool.map(join, range(5)))
        self.assertEqual(sum(result[0] == "ok" for result in results), 1)
        self.assertEqual(sum(result[0] == "pairing_unavailable" for result in results), 4)

    def test_restart_preserves_session_events_and_monotonic_cursor(self) -> None:
        pairing = self.store.create_pairing("agent-a", "demo")
        joined = self.store.join_pairing(pairing["join_token"], "agent-b", consent=True)
        self.store.session_send(pairing["initiator_session_token"], "agent-a", "task.handoff", {"task": "restart"}, "restart-event-1")

        reopened = WeftStore(self.store.state_path, self.root, heartbeat_timeout=30)
        replay = reopened.session_poll(joined["session_token"], "agent-b", after_seq=0)
        self.assertEqual([event["seq"] for event in replay["events"]], [1])
        self.assertEqual(reopened.session_ack(joined["session_token"], "agent-b", 1)["last_ack_seq"], 1)
        self.assertEqual(reopened.session_ack(joined["session_token"], "agent-b", 0)["last_ack_seq"], 1)
        self.assertEqual(reopened.health_status()["schema_version"], 3)

    def test_retention_cleanup_is_dry_run_first_and_terminal_only(self) -> None:
        pairing = self.store.create_pairing("agent-a", "demo")
        joined = self.store.join_pairing(pairing["join_token"], "agent-b", consent=True)
        self.store.session_send(pairing["initiator_session_token"], "agent-a", "task.handoff", {"task": "retention"}, "retention-event-1")
        self.store.close_session(pairing["initiator_session_token"], "agent-a")
        with self.store._transaction() as connection:
            connection.execute("UPDATE sessions SET created_at = '2000-01-01T00:00:00.000Z' WHERE session_id = ?", (joined["session_id"],))
            connection.execute("UPDATE events SET created_at = '2000-01-01T00:00:00.000Z'")
            connection.execute("UPDATE pairing_credentials SET created_at = '2000-01-01T00:00:00.000Z' WHERE pairing_id = ?", (pairing["pairing_id"],))
        preview = self.store.prune_expired(86_400, apply=False)
        self.assertFalse(preview["applied"])
        self.assertGreaterEqual(preview["would_delete"]["terminal_sessions"], 1)
        self.assertGreaterEqual(preview["would_delete"]["audit_events"], 1)
        applied = self.store.prune_expired(86_400, apply=True)
        self.assertTrue(applied["applied"])
        self.assertGreaterEqual(applied["deleted"]["terminal_sessions"], 1)
        self.assertGreaterEqual(applied["deleted"]["audit_events"], 1)

    def test_session_poll_does_not_wait_behind_a_writer_transaction(self) -> None:
        pairing = self.store.create_pairing("agent-a", "demo")
        joined = self.store.join_pairing(pairing["join_token"], "agent-b", consent=True)
        connection = self.store._connect()
        connection.execute("BEGIN IMMEDIATE")
        pool = ThreadPoolExecutor(max_workers=1)
        future = pool.submit(self.store.session_poll, joined["session_token"], "agent-b", 0)
        try:
            result = future.result(timeout=1)
            self.assertEqual(result["events"], [])
        except FutureTimeoutError as exc:
            self.fail("session_poll acquired a writer lock and was blocked")
        finally:
            connection.rollback()
            connection.close()
            pool.shutdown(wait=True)

    def test_pairing_expiry_rejects_join_without_leaking_token(self) -> None:
        with patch("weft_mcp.core._epoch", side_effect=[1000.0, 2000.0]):
            pairing = self.store.create_pairing("agent-a", "demo", ttl_seconds=60)
            with self.assertRaises(WeftError) as expired:
                self.store.join_pairing(pairing["join_token"], "agent-b", consent=True)
        self.assertEqual(expired.exception.code, "pairing_expired")
        self.assertNotIn(pairing["join_token"].encode("utf-8"), self.store.state_path.read_bytes())

    def test_public_url_rejects_non_http_and_credential_bearing_values(self) -> None:
        for invalid_url in ("javascript:alert(1)", "https://user:pass@example.com", "https://example.com/path?token=secret", "https://example.com/#token"):
            with self.subTest(invalid_url=invalid_url), self.assertRaises(WeftError) as invalid:
                WeftStore(self.root / "invalid.db", self.root / "invalid-workspace", public_base_url=invalid_url)
            self.assertEqual(invalid.exception.code, "invalid_public_url")


class MCPProtocolTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        root = Path(self.temp.name)
        self.dispatcher = WeftDispatcher(WeftStore(root / "state.db", root))

    def tearDown(self) -> None:
        self.dispatcher.store.close()
        self.temp.cleanup()

    def test_initialize_and_tools_discovery(self) -> None:
        initialize = handle_json_rpc(self.dispatcher, {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {"protocolVersion": "2025-11-25"}})
        self.assertEqual(initialize["result"]["protocolVersion"], "2025-11-25")
        tools = handle_json_rpc(self.dispatcher, {"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}})
        names = [tool["name"] for tool in tools["result"]["tools"]]
        self.assertIn("send_message", names)
        self.assertIn("verify_task", names)
        catalog = self.dispatcher.call_tool("model_catalog", {})
        model_ids = {model["id"] for model in catalog["models"]}
        self.assertIn("qwencloud/qwen3.8-max-preview", model_ids)
        self.assertIn("longcat/LongCat-2.0", model_ids)
        self.assertIn("opencode-go/mimo-v2.5", model_ids)

    def test_pairing_requires_team_id_on_unscoped_dispatcher(self) -> None:
        with self.assertRaises(WeftError) as missing_team:
            self.dispatcher.call_tool("create_pairing", {"initiator_id": "agent-a"})
        self.assertEqual(missing_team.exception.code, "invalid_argument")

    def test_stdio_never_emits_logs(self) -> None:
        request_stream = io.StringIO(json.dumps({"jsonrpc": "2.0", "id": 1, "method": "ping", "params": {}}) + "\n")
        output_stream = io.StringIO()
        run_stdio(self.dispatcher, request_stream, output_stream)
        self.assertEqual(json.loads(output_stream.getvalue())["result"], {})

    def test_streamable_http_post_and_origin_guard(self) -> None:
        handler = type("TestWeftHTTPHandler", (_MCPRequestHandler,), {})
        handler.dispatcher = self.dispatcher
        handler.token = "test-token"
        handler.allowed_origins = {"http://localhost"}
        server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            host, port = server.server_address
            connection = HTTPConnection(host, port, timeout=5)
            connection.request("OPTIONS", "/mcp", headers={"Origin": "http://localhost", "Access-Control-Request-Method": "POST"})
            response = connection.getresponse()
            response.read()
            connection.close()
            self.assertEqual(response.status, 204)
            self.assertEqual(response.getheader("Access-Control-Allow-Origin"), "http://localhost")

            request = json.dumps({"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {"protocolVersion": "2025-11-25"}})
            connection = HTTPConnection(host, port, timeout=5)
            connection.request("POST", "/mcp", request, {"Content-Type": "application/json", "Authorization": "Bearer test-token", "Origin": "http://localhost", "Mcp-Method": "initialize"})
            response = connection.getresponse()
            body = json.loads(response.read())
            connection.close()
            self.assertEqual(response.status, 200)
            self.assertEqual(body["result"]["serverInfo"]["name"], "weft-mcp")

            connection = HTTPConnection(host, port, timeout=5)
            connection.request("POST", "/mcp", request, {"Content-Type": "application/json", "Authorization": "Bearer test-token", "Origin": "https://untrusted.example"})
            response = connection.getresponse()
            response.read()
            connection.close()
            self.assertEqual(response.status, 403)
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=5)

    def test_mcp_rate_limiter_caps_requests_and_concurrency(self) -> None:
        limiter = _WindowRateLimiter(limit=20, window_seconds=60, max_concurrent=2)
        self.assertEqual(limiter.allow("client", reserve=True), (True, 0))
        self.assertEqual(limiter.allow("client", reserve=True), (True, 0))
        allowed, retry_after = limiter.allow("client", reserve=True)
        self.assertFalse(allowed)
        self.assertGreaterEqual(retry_after, 1)
        limiter.release("client")
        limiter.release("client")
        self.assertEqual(limiter.allow("client", reserve=True)[0], True)
        burst_limiter = _WindowRateLimiter(limit=2, window_seconds=60)
        self.assertEqual(burst_limiter.allow("client"), (True, 0))
        self.assertEqual(burst_limiter.allow("client"), (True, 0))
        self.assertFalse(burst_limiter.allow("client")[0])

    def test_http_mcp_endpoint_is_rate_limited(self) -> None:
        handler = type("RateLimitedMCPHandler", (_MCPRequestHandler,), {})
        handler.dispatcher = self.dispatcher
        handler.token = "test-token"
        handler.allowed_origins = {"http://localhost"}
        handler.rate_limiter = _WindowRateLimiter()
        handler.mcp_rate_limiter = _WindowRateLimiter(limit=2, window_seconds=60, now=lambda: 0.0)
        handler.metrics = _Metrics()
        server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            host, port = server.server_address
            request = json.dumps({"jsonrpc": "2.0", "id": 1, "method": "ping", "params": {}})
            statuses = []
            for _ in range(3):
                connection = HTTPConnection(host, port, timeout=5)
                connection.request("POST", "/mcp", request, {"Content-Type": "application/json", "Authorization": "Bearer test-token"})
                response = connection.getresponse()
                response.read()
                statuses.append(response.status)
                connection.close()
            self.assertEqual(statuses, [200, 200, 429])
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=5)

    def test_dispatcher_team_scope_is_a_hard_boundary(self) -> None:
        scoped = WeftDispatcher(self.dispatcher.store, team_scope="demo")
        registered = scoped.call_tool("register_agent", {"team_id": "demo", "agent_id": "scoped-agent", "role": "reviewer"})
        self.assertEqual(registered["agent_id"], "scoped-agent")
        implicit = scoped.call_tool("register_agent", {"agent_id": "scoped-agent-2", "role": "reviewer"})
        self.assertEqual(implicit["agent_id"], "scoped-agent-2")
        scoped_status = scoped.call_tool("team_status", {})
        self.assertEqual(scoped_status["team_id"], "demo")
        with self.assertRaises(WeftError) as forbidden:
            scoped.call_tool("team_status", {"team_id": "other"})
        self.assertEqual(forbidden.exception.code, "team_scope_forbidden")
        with self.assertRaises(WeftError) as pairing_forbidden:
            scoped.call_tool("create_pairing", {"team_id": "other", "initiator_id": "scoped-agent"})
        self.assertEqual(pairing_forbidden.exception.code, "team_scope_forbidden")

        self.dispatcher.call_tool("register_agent", {"team_id": "other", "agent_id": "other-initiator", "role": "architect"})
        other_pairing = self.dispatcher.call_tool("create_pairing", {"team_id": "other", "initiator_id": "other-initiator"})
        with self.assertRaises(WeftError) as pair_capability_forbidden:
            scoped.call_tool("pairing_preview", {"token": other_pairing["join_token"]})
        self.assertEqual(pair_capability_forbidden.exception.code, "team_scope_forbidden")
        other_joined = self.dispatcher.call_tool("join_pairing", {"token": other_pairing["join_token"], "agent_id": "other-joiner", "consent": True})
        with self.assertRaises(WeftError) as session_capability_forbidden:
            scoped.call_tool("session_status", {"session_token": other_joined["session_token"], "agent_id": "other-joiner"})
        self.assertEqual(session_capability_forbidden.exception.code, "team_scope_forbidden")

    def test_mcp_tool_round_trip_dispatch_claim_verify_complete(self) -> None:
        self.dispatcher.call_tool("register_agent", {"team_id": "demo", "agent_id": "agent-a", "role": "architect", "model": "gpt-5.6-luna", "capabilities": ["planning"]})
        self.dispatcher.call_tool("register_agent", {"team_id": "demo", "agent_id": "agent-b", "role": "coding", "model": "longcat-2.0", "capabilities": ["coding", "testing"]})
        created = self.dispatcher.call_tool("create_task", {"team_id": "demo", "created_by": "agent-a", "title": "Build the handoff", "description": "Implement code and tests", "scope": ["handoff.txt"], "preferred_agent": "agent-b"})
        self.assertTrue(created["dispatch"]["sent"])
        task = created["task"]
        inbox = self.dispatcher.call_tool("read_inbox", {"team_id": "demo", "agent_id": "agent-b"})
        self.assertEqual(inbox["messages"][0]["type"], "task.dispatch")
        claimed = self.dispatcher.call_tool("claim_task", {"team_id": "demo", "agent_id": "agent-b", "task_id": task["task_id"]})
        (Path(self.dispatcher.store.workspace) / "handoff.txt").write_text("handoff complete\n", encoding="utf-8")
        verified = self.dispatcher.call_tool("verify_task", {"team_id": "demo", "agent_id": "agent-b", "task_id": task["task_id"], "fencing_token": claimed["fencing_token"], "files": ["handoff.txt"], "checks": [{"name": "unit-tests", "status": "passed", "evidence": "8 tests pass"}]})
        self.assertTrue(verified["passed"])
        completed = self.dispatcher.call_tool("complete_task", {"team_id": "demo", "agent_id": "agent-b", "task_id": task["task_id"], "fencing_token": claimed["fencing_token"], "summary": "Handoff verified"})
        self.assertEqual(completed["status"], "done")

    def test_http_health_preview_and_public_join(self) -> None:
        self.dispatcher.call_tool("register_agent", {"team_id": "demo", "agent_id": "agent-a", "role": "architect", "model": "gpt-5.6-luna", "capabilities": ["planning"]})
        pairing = self.dispatcher.call_tool("create_pairing", {"team_id": "demo", "initiator_id": "agent-a", "capabilities_offered": ["read"]})
        handler = type("TestPairingHTTPHandler", (_MCPRequestHandler,), {})
        handler.dispatcher = self.dispatcher
        handler.token = "test-token"
        handler.allowed_origins = {"http://localhost"}
        handler.rate_limiter = _WindowRateLimiter(limit=3, window_seconds=60, now=lambda: 0.0)
        handler.metrics = _Metrics()
        server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            host, port = server.server_address
            connection = HTTPConnection(host, port, timeout=5)
            connection.request("GET", "/healthz")
            response = connection.getresponse()
            health = json.loads(response.read())
            connection.close()
            self.assertEqual(response.status, 200)
            self.assertEqual(health["status"], "ok")
            self.assertNotIn("active_sessions", health)

            join_path = urlsplit(pairing["join_url"]).path
            connection = HTTPConnection(host, port, timeout=5)
            connection.request("GET", join_path, headers={"Origin": "http://localhost"})
            response = connection.getresponse()
            preview = json.loads(response.read())
            connection.close()
            self.assertEqual(response.status, 200)
            self.assertEqual(preview["pairing"]["status"], "issued")
            self.assertIsNone(preview["pairing"]["team_id"])
            self.assertEqual(response.getheader("Referrer-Policy"), "no-referrer")

            body = json.dumps({"token": pairing["join_token"], "agent_id": "agent-b", "model": "opencode-go/mimo-v2.5", "capabilities": ["coding"], "consent": True})
            connection = HTTPConnection(host, port, timeout=5)
            connection.request("POST", join_path, body=body, headers={"Content-Type": "application/json", "Origin": "http://localhost"})
            response = connection.getresponse()
            joined = json.loads(response.read())
            connection.close()
            self.assertEqual(response.status, 200)
            self.assertTrue(joined["session_token"].startswith("fst_session_"))

            connection = HTTPConnection(host, port, timeout=5)
            connection.request("GET", join_path)
            response = connection.getresponse()
            response.read()
            connection.close()
            self.assertEqual(response.status, 409)

            connection = HTTPConnection(host, port, timeout=5)
            connection.request("GET", join_path)
            response = connection.getresponse()
            response.read()
            connection.close()
            self.assertEqual(response.status, 429)

            connection = HTTPConnection(host, port, timeout=5)
            connection.request("GET", "/v1/metrics")
            response = connection.getresponse()
            response.read()
            connection.close()
            self.assertEqual(response.status, 401)

            connection = HTTPConnection(host, port, timeout=5)
            connection.request("GET", "/v1/metrics", headers={"Authorization": "Bearer test-token"})
            response = connection.getresponse()
            metrics = response.read().decode("utf-8")
            connection.close()
            self.assertEqual(response.status, 200)
            self.assertIn("weft_http_responses_total", metrics)
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=5)

    def test_http_rejects_token_bearing_legacy_join_paths(self) -> None:
        self.dispatcher.call_tool("register_agent", {"team_id": "demo", "agent_id": "agent-a", "role": "architect", "model": "gpt-5.6-luna", "capabilities": ["planning"]})
        pairing = self.dispatcher.call_tool("create_pairing", {"team_id": "demo", "initiator_id": "agent-a"})
        handler = type("LegacyJoinHTTPHandler", (_MCPRequestHandler,), {})
        handler.dispatcher = self.dispatcher
        handler.token = None
        handler.allowed_origins = {"http://localhost"}
        handler.rate_limiter = _WindowRateLimiter()
        handler.metrics = _Metrics()
        server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            host, port = server.server_address
            connection = HTTPConnection(host, port, timeout=5)
            connection.request("GET", f"/join/{pairing['join_token']}")
            response = connection.getresponse()
            body = response.read().decode("utf-8")
            connection.close()
            self.assertEqual(response.status, 410)
            self.assertNotIn(pairing["join_token"], body)
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=5)


if __name__ == "__main__":
    unittest.main()
