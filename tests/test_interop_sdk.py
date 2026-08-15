"""SDK-tier integration tests: two-party handoff + N-agent room path.

Spawns the coordinator over HTTP, drives it through the SDK, and asserts the
two-party pairing/claim/verify/complete handoff and the N-agent room path
(membership, addressing, ordered replay, reconnect, negative refusals).

Reuses the driver's spawn/teardown helpers to keep the test self-contained.
Run: python -B -m unittest tests.test_interop_sdk -v   (<30s)
"""

from __future__ import annotations

import json
import secrets
import socket
import subprocess
import sys
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from weft_sdk import WeftClient, WeftError
from weft_cloud.service import WeftCloudService, _CloudHTTPHandler
from weft_cloud.storage import SqliteWalBackend


def _pick_free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _wait_tcp(host: str, port: int, timeout_s: float = 10.0) -> None:
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        try:
            with socket.create_connection((host, port), timeout=0.5):
                return
        except OSError:
            time.sleep(0.1)
    raise RuntimeError(f"coordinator did not accept TCP on {host}:{port}")


class SdkInteropTest(unittest.TestCase):
    """End-to-end SDK tier validation against a real coordinator over HTTP."""

    proc: subprocess.Popen | None = None
    workspace: Path | None = None
    base_url: str = ""
    clients: dict[str, WeftClient] = {}
    tokens: dict[str, str] = {}
    room_id: str = ""
    link_token: str = ""

    @classmethod
    def setUpClass(cls) -> None:
        cls.scratch = tempfile.TemporaryDirectory(prefix="weft-sdk-test-")
        cls.workspace = Path(cls.scratch.name)
        port = _pick_free_port()
        cls.base_url = f"http://127.0.0.1:{port}/mcp"
        cls.proc = subprocess.Popen(
            [
                sys.executable,
                "-B",
                "scripts/weft-mcp.py",
                "--transport",
                "http",
                "--host",
                "127.0.0.1",
                "--port",
                str(port),
                "--team-id",
                "demo",
                "--workspace",
                str(cls.workspace),
                "--state",
                str(cls.workspace / ".weft" / "state.db"),
            ],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            cwd=str(PROJECT_ROOT),
            encoding="utf-8",
            errors="replace",
        )
        _wait_tcp("127.0.0.1", port)
        # Register 4 agents (3 room members + 1 outsider) with proper token binding.
        for name in ["agent-a", "agent-b", "agent-c", "agent-outsider"]:
            bootstrap = WeftClient(cls.base_url, name, "demo")
            reg = bootstrap.register(name=name, role="generalist")
            cls.tokens[name] = reg["actor_token"]
            bootstrap.close()
            cls.clients[name] = WeftClient(cls.base_url, name, "demo", actor_token=cls.tokens[name])
        # Create one room shared by the Part B tests; owner auto-joins.
        room = cls.clients["agent-a"]._call(
            "room_create", owner_agent_id="agent-a", cap=4, name="t-sdk",
        )
        cls.room_id = room["room_id"]
        cls.link_token = room["link_token"]
        # Join the other two members once.
        for name in ["agent-b", "agent-c"]:
            cls.clients[name]._call(
                "room_join", room_id=cls.room_id, link_token=cls.link_token,
                agent_id=name, consent=True, capabilities=["read"],
            )

    @classmethod
    def tearDownClass(cls) -> None:
        for c in cls.clients.values():
            c.close()
        if cls.proc is not None:
            cls.proc.terminate()
            try:
                cls.proc.wait(timeout=10)
            except subprocess.TimeoutExpired:
                cls.proc.kill()
                cls.proc.wait(timeout=10)
        cls.scratch.cleanup()

    # ---- helpers ----------------------------------------------------------

    def _join_pairing_via_call(self, link: str, agent: str) -> dict:
        from urllib.parse import urlsplit, parse_qs
        token = parse_qs(urlsplit(link).fragment).get("token", [""])[0]
        return self.clients[agent]._call(
            "join_pairing",
            token=token,
            agent_id=agent,
            name=agent,
            role="generalist",
            consent=True,
        )

    # ---- Part A: two-party handoff ---------------------------------------

    def test_01_two_party_handoff(self) -> None:
        """Pairing -> claim -> evidence -> complete through the SDK."""
        a, b = self.clients["agent-a"], self.clients["agent-b"]

        pairing = a.create_pairing_link(capabilities=["read", "comment"])
        self.assertIsNotNone(pairing.join_token)

        jr = self._join_pairing_via_call(pairing.join_url, "agent-b")
        self.assertIn(jr.get("state"), ("active", "open"))

        task_id = a.create_task(
            scope=["handoff.txt"],
            description="Verify handoff artifact",
            title="Interop SDK handoff",
            idempotency_key="sdk-test-handoff-v1",
        )
        self.assertTrue(task_id)

        claimed = b.claim(task_id)
        self.assertIsNotNone(claimed.fencing_token)

        (self.workspace / "handoff.txt").write_text("SDK test evidence\n", encoding="utf-8")
        verified = b.submit_evidence(
            task_id,
            artifact_paths=["handoff.txt"],
            checks=[{"name": "sdk-check", "status": "passed", "evidence": "present"}],
            fencing_token=claimed.fencing_token,
        )
        self.assertTrue(verified.get("passed"))

        completed = b.complete(task_id, fencing_token=claimed.fencing_token, summary="done")
        self.assertEqual(completed.status, "done")

    # ---- Part B: N-agent room --------------------------------------------

    def test_02_room_n_agents_membership(self) -> None:
        """3 agents join one room; roster shows all 3; state is active."""
        info = self.clients["agent-a"]._call("room_info", room_id=self.room_id)
        self.assertEqual(info["member_count"], 3)
        self.assertEqual(sorted(m["agent_id"] for m in info["members"]),
                         ["agent-a", "agent-b", "agent-c"])
        self.assertEqual(info["state"], "active")

    def test_03_room_addressing(self) -> None:
        """Unicast, group, and broadcast produce correct receipts."""
        room_id = self.room_id
        self.assertTrue(room_id)

        # Unicast A -> B
        send_b = self.clients["agent-a"]._call(
            "room_send", room_id=room_id, sender_agent_id="agent-a",
            target_spec="agent-b", payload={"text": "hi B"},
        )
        self.assertEqual([r["agent_id"] for r in send_b["receipts"]], ["agent-b"])

        # Group: add B+C, send
        self.clients["agent-a"]._call(
            "room_groups", room_id=room_id, agent_id="agent-a",
            group_name="builders", action="add", members=["agent-b", "agent-c"],
        )
        send_g = self.clients["agent-a"]._call(
            "room_send", room_id=room_id, sender_agent_id="agent-a",
            target_spec="builders", payload={"text": "hi group"},
        )
        self.assertEqual(sorted(r["agent_id"] for r in send_g["receipts"]),
                         ["agent-b", "agent-c"])

        # Broadcast
        send_bc = self.clients["agent-a"]._call(
            "room_send", room_id=room_id, sender_agent_id="agent-a",
            target_spec="*", payload={"text": "hi all"},
        )
        self.assertEqual(sorted(r["agent_id"] for r in send_bc["receipts"]),
                         ["agent-b", "agent-c"])

    def test_04_room_ordered_replay_and_reconnect(self) -> None:
        """Ordered event log, per-member cursors, reconnect with no loss/dupes."""
        room_id = self.room_id
        # Each member polls from 0, acks head.
        cursors: dict[str, int] = {}
        for name in ["agent-a", "agent-b", "agent-c"]:
            poll = self.clients[name]._call(
                "room_poll", room_id=room_id, agent_id=name, after_seq=0,
            )
            seqs = [e["seq"] for e in poll["events"]]
            self.assertEqual(seqs, sorted(set(seqs)), f"{name} events ordered, no dup")
            head = poll["cursor_head"]
            acked = self.clients[name]._call(
                "room_ack", room_id=room_id, agent_id=name, seq=head,
            )
            cursors[name] = acked["last_ack_seq"]
            self.assertEqual(acked["last_ack_seq"], head)

        # Reconnect: fresh client reusing agent-b's identity+token.
        reconnected = WeftClient(
            self.base_url, "agent-b", "demo", actor_token=self.tokens["agent-b"],
        )
        try:
            replay = reconnected._call(
                "room_poll", room_id=room_id, agent_id="agent-b", after_seq=0,
            )
            replay_seqs = [e["seq"] for e in replay["events"]]
            self.assertEqual(replay_seqs, sorted(set(replay_seqs)),
                             "reconnect replay ordered, no dup")
            # From cursor: no already-acked events replayed.
            from_cursor = reconnected._call(
                "room_poll", room_id=room_id, agent_id="agent-b",
                after_seq=cursors["agent-b"],
            )
            self.assertEqual(len(from_cursor["events"]), 0,
                             "no already-acked events replayed from cursor")
        finally:
            reconnected.close()

    def test_05_room_negative_cases(self) -> None:
        """Non-member refused (room_not_found, no existence oracle); actor-overwrite refused (actor_auth_invalid)."""
        room_id = self.room_id
        # Non-member poll
        with self.assertRaises(WeftError) as ctx:
            self.clients["agent-outsider"]._call(
                "room_poll", room_id=room_id, agent_id="agent-outsider",
            )
        self.assertEqual(ctx.exception.code, "room_not_found")

        # Non-member info
        with self.assertRaises(WeftError) as ctx:
            self.clients["agent-outsider"]._call(
                "room_info", room_id=room_id, agent_id="agent-outsider",
            )
        self.assertEqual(ctx.exception.code, "room_not_found")

        # Actor-overwrite: existing agent-b identity with a DIFFERENT token.
        bogus = "rm_" + secrets.token_urlsafe(32)
        impostor = WeftClient(self.base_url, "agent-b", "demo", actor_token=bogus)
        try:
            with self.assertRaises(WeftError) as ctx:
                impostor._call(
                    "room_join", room_id=room_id, link_token=self.link_token,
                    agent_id="agent-b", consent=True,
                )
            self.assertEqual(ctx.exception.code, "actor_auth_invalid")
        finally:
            impostor.close()


class SdkHostedInteropTests(unittest.TestCase):
    """The SDK must be able to drive the HOSTED MCP surface with a bearer
    credential — the same endpoint the stdio bridge and the web app use.

    RED: ``WeftClient._call`` injects ``team_id`` and ``agent_id`` into every
    tool call, and the hosted dispatcher rejects client-supplied identity
    arguments (``weft_cloud/mcp.py`` ``_FORBIDDEN_IDENTITY_ARGS``), so a room
    call is refused with ``invalid_argument`` instead of succeeding.
    """

    @classmethod
    def setUpClass(cls) -> None:
        cls.tmpdir = tempfile.mkdtemp(prefix="weft-sdk-hosted-")
        cls.db_path = str(Path(cls.tmpdir) / "test.db")
        cls.service = WeftCloudService(SqliteWalBackend(cls.db_path))
        cls._httpd = ThreadingHTTPServer(("127.0.0.1", 0), _CloudHTTPHandler)
        _CloudHTTPHandler.service = cls.service
        cls.port = cls._httpd.server_address[1]
        cls.base = f"http://127.0.0.1:{cls.port}"
        cls.thread = threading.Thread(target=cls._httpd.serve_forever, daemon=True)
        cls.thread.start()

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

    def _post(self, path: str, body: dict, token: str | None = None) -> tuple[int, dict]:
        data = json.dumps(body).encode("utf-8")
        req = urllib.request.Request(self.base + path, data=data, method="POST")
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

    def test_sdk_room_poll_drives_hosted_surface(self) -> None:
        """Sign up, create a room over the hosted REST surface, then drive
        room_poll through the SDK against the hosted /mcp endpoint."""
        status, acct = self._post("/v1/auth/signup", {
            "email": "sdk-hosted@example.com", "password": "password-123",
        })
        self.assertEqual(status, 201, f"signup failed: {acct}")
        status, room = self._post(
            "/v1/rooms/create", {"cap": 4}, token=acct["session_token"],
        )
        self.assertEqual(status, 201, f"room create failed: {room}")

        client = WeftClient(
            f"{self.base}/mcp",
            acct["account_id"],
            "",
            bearer_token=acct["session_token"],
        )
        try:
            try:
                poll = client.room_poll(room["room_id"])
            except WeftError as exc:
                self.fail(
                    f"SDK room call against the hosted surface was refused: "
                    f"code={exc.code!r} message={exc.message!r} — the SDK must "
                    "not inject identity arguments (team_id/agent_id/"
                    "actor_token) that the hosted dispatcher rejects"
                )
            self.assertEqual(poll.room_id, room["room_id"])
            self.assertIn("room.created",
                          [e.kind for e in poll.events],
                          "owner must see the room.created lifecycle event")
        finally:
            client.close()


if __name__ == "__main__":
    unittest.main()
