"""stdio<->HTTP MCP bridge integration tests.

The hosted Weft service (``weft_cloud``) is only reachable as a Streamable-HTTP
``POST /mcp`` endpoint with Bearer auth. Real MCP hosts (Claude Desktop,
Cursor, Claude Code, Codex) launch servers as ``command`` + ``args`` stdio
subprocesses and have no ``url`` form. This test drives the bridge that makes
the hosted rooms reachable from those hosts, through the EXACT code path a real
client uses: a subprocess running ``scripts/weft-mcp.py --remote ...`` with the
token in an environment variable.

The highest-value test is last: two SEPARATE bridge subprocesses, each with its
own account and token, joining the SAME room via the same link and exchanging a
message. That is the product's entire promise exercised end to end.

Authoritative spec: docs/STDIO_BRIDGE.md.
"""

from __future__ import annotations

import json
import subprocess
import sys
import tempfile
import threading
import unittest
import urllib.request
import urllib.error
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from weft_cloud.service import WeftCloudService, _CloudHTTPHandler  # noqa: E402
from weft_cloud.storage import SqliteWalBackend  # noqa: E402
from weft_mcp.__main__ import build_parser  # noqa: E402
from weft_mcp.stdio_bridge import unwrap_response_body  # noqa: E402


def _post(base: str, path: str, body: dict, token: str | None = None) -> tuple[int, dict]:
    data = json.dumps(body).encode("utf-8")
    req = urllib.request.Request(base + path, data=data, method="POST")
    req.add_header("Content-Type", "application/json")
    if token:
        req.add_header("Authorization", f"Bearer {token}")
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            raw = resp.read()
            return resp.status, json.loads(raw.decode("utf-8")) if raw else {}
    except urllib.error.HTTPError as exc:
        payload = {}
        try:
            raw = exc.read()
            payload = json.loads(raw.decode("utf-8")) if raw else {}
        except Exception:
            pass
        finally:
            exc.close()
        return exc.code, payload


def _rpc(proc: subprocess.Popen, request: dict) -> dict | None:
    """Write one JSON-RPC line to a bridge subprocess and read its reply."""
    proc.stdin.write(json.dumps(request, separators=(",", ":")) + "\n")
    proc.stdin.flush()
    line = proc.stdout.readline()
    return json.loads(line) if line else None


def _spawn_bridge(base: str, token_env_name: str, token: str | None) -> subprocess.Popen:
    env = dict(os_environ_for_token(token_env_name, token))
    proc = subprocess.Popen(
        [sys.executable, "-B", "scripts/weft-mcp.py", "--remote", base,
         "--token-env", token_env_name],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        text=True,
        encoding="utf-8",
        errors="replace",
        cwd=str(PROJECT_ROOT),
        env=env,
        bufsize=1,
    )
    return proc


def os_environ_for_token(token_env_name: str, token: str | None) -> dict:
    import os
    env = dict(os.environ)
    if token is None:
        env.pop(token_env_name, None)
    else:
        env[token_env_name] = token
    return env


def _stop_proc(proc: subprocess.Popen | None) -> None:
    if proc is None:
        return
    try:
        if proc.stdin is not None:
            proc.stdin.close()
        proc.terminate()
        proc.wait(timeout=10)
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.wait(timeout=10)
    finally:
        for stream in (proc.stdout, proc.stderr):
            if stream is not None:
                try:
                    stream.close()
                except Exception:
                    pass


class StdioBridgeHostedTestBase(unittest.TestCase):
    """Runs the real hosted cloud service on a background thread.

    One server per test CLASS, shared by its tests, exactly like
    ``test_hosted_mcp.py``. Every signup mints a fresh account/tenant.
    """

    @classmethod
    def setUpClass(cls) -> None:
        cls.tmpdir = tempfile.mkdtemp(prefix="weft-stdio-bridge-")
        cls.db_path = str(Path(cls.tmpdir) / "test.db")
        cls._httpd = ThreadingHTTPServer(("127.0.0.1", 0), _CloudHTTPHandler)
        cls.port = cls._httpd.server_address[1]
        cls.base = f"http://127.0.0.1:{cls.port}"
        cls.service = WeftCloudService(SqliteWalBackend(cls.db_path))
        _CloudHTTPHandler.service = cls.service
        cls.server_thread = threading.Thread(target=cls._httpd.serve_forever, daemon=True)
        cls.server_thread.start()

    @classmethod
    def tearDownClass(cls) -> None:
        if cls._httpd is not None:
            try:
                cls._httpd.shutdown()
            finally:
                cls._httpd.server_close()
        try:
            cls.service.backend.close()
        except Exception:
            pass
        import shutil
        shutil.rmtree(cls.tmpdir, ignore_errors=True)

    def _signup(self, email: str, password: str = "password-123") -> dict:
        status, resp = _post(self.base, "/v1/auth/signup", {"email": email, "password": password})
        self.assertEqual(status, HTTPStatus.CREATED, f"signup failed: {resp}")
        return resp

    def _init_and_list(self, proc: subprocess.Popen) -> tuple[dict, list[str]]:
        init = _rpc(proc, {"jsonrpc": "2.0", "id": 1, "method": "initialize",
                           "params": {"protocolVersion": "2025-03-26", "capabilities": {}}})
        self.assertIsNotNone(init)
        self.assertIn("serverInfo", init["result"])
        listing = _rpc(proc, {"jsonrpc": "2.0", "id": 2, "method": "tools/list"})
        self.assertIsNotNone(listing)
        names = [t["name"] for t in listing["result"]["tools"]]
        return init, names


class MissingTokenTests(unittest.TestCase):
    """A misconfiguration must surface as a JSON-RPC error the client displays,
    plus a non-zero exit — not a silent exit or a hang."""

    def test_missing_token_returns_json_rpc_error_naming_env_var(self) -> None:
        proc = _spawn_bridge("http://127.0.0.1:1", "WEFT_STDIO_TEST_TOKEN", None)
        try:
            reply = _rpc(proc, {"jsonrpc": "2.0", "id": 1, "method": "initialize",
                                "params": {"protocolVersion": "2025-11-25"}})
            self.assertIsNotNone(reply)
            self.assertEqual(reply.get("id"), 1)
            error = reply.get("error") or {}
            self.assertIn("WEFT_STDIO_TEST_TOKEN", error.get("message", ""),
                          f"error must name the env var, got: {error}")
            proc.stdin.close()
            proc.wait(timeout=10)
            self.assertNotEqual(proc.returncode, 0, "missing token must exit non-zero")
        finally:
            _stop_proc(proc)


class UpstreamAuthTests(StdioBridgeHostedTestBase):
    """An upstream 401 must produce a renderable JSON-RPC error and not hang."""

    def test_upstream_401_returns_renderable_error(self) -> None:
        token = self._signup("bridge-401@example.com")["session_token"]
        proc = _spawn_bridge(self.base, "WEFT_STDIO_TEST_TOKEN", "fss_bogus-expired-token")
        try:
            reply = _rpc(proc, {"jsonrpc": "2.0", "id": 7, "method": "initialize",
                                "params": {"protocolVersion": "2025-11-25"}})
            self.assertIsNotNone(reply, "must respond, not hang")
            self.assertEqual(reply.get("id"), 7)
            error = reply.get("error") or {}
            message = error.get("message", "").lower()
            self.assertTrue(
                "invalid" in message or "expired" in message or "unauthorized" in message,
                f"401 error must name token validity, got: {error}",
            )
        finally:
            _stop_proc(proc)

    def test_upstream_connection_refused_names_origin(self) -> None:
        """Point at a port nobody is listening on; the error names the origin."""
        # Bind an ephemeral port then immediately close it so nothing listens.
        import socket
        sock = socket.socket()
        sock.bind(("127.0.0.1", 0))
        dead_port = sock.getsockname()[1]
        sock.close()
        proc = _spawn_bridge(f"http://127.0.0.1:{dead_port}", "WEFT_STDIO_TEST_TOKEN",
                             self._signup("bridge-dns@example.com")["session_token"])
        try:
            reply = _rpc(proc, {"jsonrpc": "2.0", "id": 9, "method": "initialize",
                                "params": {"protocolVersion": "2025-11-25"}})
            self.assertIsNotNone(reply)
            error = reply.get("error") or {}
            self.assertIn(f"127.0.0.1:{dead_port}", error.get("message", ""),
                          f"connection-refused error must name the origin, got: {error}")
        finally:
            _stop_proc(proc)


class UnwrapTests(unittest.TestCase):
    """SSE-framed and plain-JSON Streamable HTTP bodies both parse."""

    def test_plain_json_body_parses(self) -> None:
        body = b'{"jsonrpc":"2.0","id":1,"result":{"ok":true}}'
        parsed = unwrap_response_body(body, "application/json")
        self.assertEqual(parsed["result"]["ok"], True)

    def test_sse_framed_body_parses(self) -> None:
        body = (
            b"event: message\ndata: {\"jsonrpc\":\"2.0\",\"id\":2,\"result\":{\"ok\":true}}\n\n"
        )
        parsed = unwrap_response_body(body, "text/event-stream")
        self.assertEqual(parsed["result"]["ok"], True)
        self.assertEqual(parsed["id"], 2)

    def test_sse_multiple_data_lines_parse(self) -> None:
        body = (
            b"event: message\n"
            b"data: {\"jsonrpc\":\"2.0\",\"id\":3,\n"
            b"data: \"result\":{\"ok\":true}}\n\n"
        )
        parsed = unwrap_response_body(body, "text/event-stream")
        self.assertEqual(parsed["result"]["ok"], True)

    def test_empty_body_returns_none(self) -> None:
        self.assertIsNone(unwrap_response_body(b"", "application/json"))


class EndToEndSseStubTests(unittest.TestCase):
    """Drive the bridge against an SSE-framing stub /mcp and assert the client
    sees unwrapped JSON-RPC. Also asserts Mcp-Session-Id preservation."""

    class _StubHandler(BaseHTTPRequestHandler):
        seen_session_ids: list = []
        request_count = 0
        stub_token = "fss_stub-token"

        def log_message(self, format, *args):  # noqa: N802
            return

        def do_POST(self):  # noqa: N802
            length = int(self.headers.get("Content-Length", "0"))
            raw = self.rfile.read(length)
            request = json.loads(raw)
            type(self).request_count += 1
            type(self).seen_session_ids.append(self.headers.get("Mcp-Session-Id"))

            if request.get("method") == "notifications/initialized":
                self.send_response(HTTPStatus.ACCEPTED)
                self.send_header("Content-Length", "0")
                self.end_headers()
                return

            supplied = self.headers.get("Authorization", "")
            if supplied != f"Bearer {type(self).stub_token}":
                payload = {"jsonrpc": "2.0", "id": request.get("id"),
                           "error": {"code": -32001, "message": "Unauthorized"}}
                body = json.dumps(payload, separators=(",", ":")).encode("utf-8")
                self.send_response(HTTPStatus.UNAUTHORIZED)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
                return

            resp = {"jsonrpc": "2.0", "id": request.get("id"),
                    "result": {"echoed_method": request.get("method"), "n": type(self).request_count}}
            data_json = json.dumps(resp, separators=(",", ":"))
            body = f"event: message\ndata: {data_json}\n\n".encode("utf-8")
            self.send_response(HTTPStatus.OK)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Content-Length", str(len(body)))
            if type(self).request_count == 1:
                self.send_header("Mcp-Session-Id", "ses-stub-abc123")
            self.end_headers()
            self.wfile.write(body)

    @classmethod
    def setUpClass(cls) -> None:
        cls._httpd = ThreadingHTTPServer(("127.0.0.1", 0), cls._StubHandler)
        cls.port = cls._httpd.server_address[1]
        cls.base = f"http://127.0.0.1:{cls.port}"
        cls._StubHandler.seen_session_ids = []
        cls._StubHandler.request_count = 0
        cls.thread = threading.Thread(target=cls._httpd.serve_forever, daemon=True)
        cls.thread.start()

    @classmethod
    def tearDownClass(cls) -> None:
        cls._httpd.shutdown()
        cls._httpd.server_close()

    def test_bridge_unwraps_sse_and_preserves_session_id(self) -> None:
        proc = _spawn_bridge(self.base, "WEFT_STDIO_TEST_TOKEN", "fss_stub-token")
        try:
            first = _rpc(proc, {"jsonrpc": "2.0", "id": 1, "method": "initialize",
                                "params": {"protocolVersion": "2025-11-25"}})
            self.assertIsNotNone(first)
            self.assertEqual(first["result"]["echoed_method"], "initialize")

            # notifications must not produce a reply line
            proc.stdin.write(json.dumps({"jsonrpc": "2.0", "method": "notifications/initialized"},
                                        separators=(",", ":")) + "\n")
            proc.stdin.flush()

            second = _rpc(proc, {"jsonrpc": "2.0", "id": 2, "method": "tools/list"})
            self.assertIsNotNone(second)
            self.assertEqual(second["result"]["echoed_method"], "tools/list")

            # The stub issued Mcp-Session-Id on request 1; request 2 must carry it back.
            self.assertEqual(self._StubHandler.seen_session_ids[0], None)
            self.assertEqual(self._StubHandler.seen_session_ids[1], "ses-stub-abc123")
        finally:
            _stop_proc(proc)


class EndToEndLocalCloudTests(StdioBridgeHostedTestBase):
    """Full stdio session against a LOCAL instance of our own hosted service."""

    def test_full_stdio_session_initialize_list_create(self) -> None:
        acct = self._signup("bridge-e2e@example.com")
        proc = _spawn_bridge(self.base, "WEFT_STDIO_TEST_TOKEN", acct["session_token"])
        try:
            init, names = self._init_and_list(proc)
            self.assertEqual(init["result"]["serverInfo"]["name"], "weft-cloud")
            # Assert the expected tools are PRESENT rather than pinning an exact list.
            # Exact equality breaks on every additive change (room_wait did exactly that)
            # while still not catching the failure that matters — a tool going MISSING.
            # Subset containment catches removal and tolerates growth.
            expected = {"room_create", "room_join", "room_send", "room_poll",
                        "room_info", "room_ack", "room_heartbeat", "room_event_log"}
            missing = expected - set(names)
            self.assertEqual(missing, set(),
                             f"tools missing from the bridged surface: {sorted(missing)}")

            created = _rpc(proc, {"jsonrpc": "2.0", "id": 3, "method": "tools/call",
                                  "params": {"name": "room_create", "arguments": {"cap": 4}}})
            self.assertIsNotNone(created)
            result = created["result"]
            self.assertFalse(result.get("isError"), f"room_create failed: {created}")
            structured = result.get("structuredContent") or {}
            self.assertIn("room_id", structured)
            self.assertTrue(structured["link_token"].startswith("rm_"))
        finally:
            _stop_proc(proc)


class TwoBridgeSharedRoomTests(StdioBridgeHostedTestBase):
    """THE test: two separate bridge processes, each with its own account and
    token, join the SAME room via the same link and exchange a message."""

    def test_two_bridge_processes_share_a_room(self) -> None:
        owner = self._signup("bridge-owner@example.com")
        joiner = self._signup("bridge-joiner@example.com")
        proc_a = _spawn_bridge(self.base, "WEFT_STDIO_TEST_TOKEN_A", owner["session_token"])
        proc_b = _spawn_bridge(self.base, "WEFT_STDIO_TEST_TOKEN_B", joiner["session_token"])
        try:
            # A: initialize + tools/list + room_create
            _, _ = self._init_and_list(proc_a)
            created = _rpc(proc_a, {"jsonrpc": "2.0", "id": 3, "method": "tools/call",
                                    "params": {"name": "room_create", "arguments": {"cap": 4}}})
            structured = created["result"].get("structuredContent") or {}
            room_id = structured["room_id"]
            link_token = structured["link_token"]

            # B: initialize + tools/list + room_join through the same link
            _, _ = self._init_and_list(proc_b)
            joined = _rpc(proc_b, {"jsonrpc": "2.0", "id": 3, "method": "tools/call",
                                   "params": {"name": "room_join", "arguments": {
                                       "room_id": room_id, "link_token": link_token,
                                       "consent": True}}})
            self.assertIsNotNone(joined)
            join_result = joined["result"]
            self.assertFalse(join_result.get("isError"), f"room_join failed: {joined}")
            self.assertEqual(join_result.get("structuredContent", {}).get("status"), "active")

            # A sends a message to the whole room.
            sent = _rpc(proc_a, {"jsonrpc": "2.0", "id": 4, "method": "tools/call",
                                 "params": {"name": "room_send", "arguments": {
                                     "room_id": room_id, "target_spec": "*",
                                     "payload": {"kind": "message", "text": "hello from bridge A"}}}})
            self.assertIsNotNone(sent)
            self.assertFalse(sent["result"].get("isError"), f"room_send failed: {sent}")

            # B polls and must see A's message — the product's whole promise.
            polled = _rpc(proc_b, {"jsonrpc": "2.0", "id": 4, "method": "tools/call",
                                   "params": {"name": "room_poll", "arguments": {"room_id": room_id}}})
            self.assertIsNotNone(polled)
            events = polled["result"].get("structuredContent", {}).get("events") or []
            texts = [e.get("payload", {}).get("payload", {}).get("text")
                     for e in events if e.get("kind") == "room.message"]
            self.assertIn("hello from bridge A", texts,
                          f"joiner did not receive owner's message; events: {events}")
        finally:
            _stop_proc(proc_a)
            _stop_proc(proc_b)


class StrictUtf8ToolsListTests(StdioBridgeHostedTestBase):
    """Bridge output must be valid UTF-8 on every platform.

    The hosted tool set includes a non-ASCII em-dash (U+2014) in the
    ``room_wait`` description. On Windows the bridge child's stdout defaults
    to the ANSI codepage (cp1252), so ``ensure_ascii=False`` JSON containing
    that character is emitted as byte 0x97 — invalid UTF-8 that a real MCP
    host reading stdout strictly cannot parse. The bridge must emit UTF-8
    regardless of the console codepage.
    """

    def test_tools_list_round_trips_as_strict_utf8(self) -> None:
        acct = self._signup("bridge-utf8@example.com")
        # Deliberately strict: errors="replace" would mask a cp1252 byte.
        env = dict(os_environ_for_token("WEFT_STDIO_TEST_TOKEN", acct["session_token"]))
        proc = subprocess.Popen(
            [sys.executable, "-B", "scripts/weft-mcp.py", "--remote", self.base,
             "--token-env", "WEFT_STDIO_TEST_TOKEN"],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
            encoding="utf-8",
            errors="strict",
            cwd=str(PROJECT_ROOT),
            env=env,
            bufsize=1,
        )
        try:
            init = _rpc(proc, {"jsonrpc": "2.0", "id": 1, "method": "initialize",
                               "params": {"protocolVersion": "2025-11-25"}})
            self.assertIsNotNone(init)
            listing = _rpc(proc, {"jsonrpc": "2.0", "id": 2, "method": "tools/list",
                                  "params": {}})
            self.assertIsNotNone(listing, "tools/list must parse under strict UTF-8")
            names = [t["name"] for t in listing["result"]["tools"]]
            self.assertIn("room_wait", names)
        finally:
            _stop_proc(proc)


class ParserTests(unittest.TestCase):
    """The new --remote flag is additive; local defaults are untouched."""

    def test_remote_flag_parses_and_local_defaults_unchanged(self) -> None:
        args = build_parser().parse_args([])
        self.assertIsNone(args.remote)
        self.assertEqual(args.transport, "stdio")
        self.assertEqual(args.token_env, "WEFT_HTTP_TOKEN")

        args_remote = build_parser().parse_args(
            ["--remote", "https://weft.example.com", "--token-env", "WEFT_TOKEN"])
        self.assertEqual(args_remote.remote, "https://weft.example.com")
        self.assertEqual(args_remote.token_env, "WEFT_TOKEN")


if __name__ == "__main__":
    unittest.main()
