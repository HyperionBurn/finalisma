"""SDK-tier integration tests for the Wave SDK-ROOMS public room API.

Drives a REAL coordinator over HTTP using ONLY the public WeftClient
room methods — the private `_call` is never used here. These tests lock the
contract the SDK-ROOMS orchestrator must implement on WeftClient.

Run:  python -B -m unittest tests.test_sdk_rooms -v   (<30s)

Expected: pass. This file protects the public room API contract on WeftClient.
"""

from __future__ import annotations

import socket
import subprocess
import sys
import tempfile
import time
import unittest
import atexit
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "src"))

# --- These imports are the contract. They WILL raise ImportError until the
#     orchestrator adds the room API surface + dataclasses to the SDK. ---
from weft_sdk import (  # noqa: E402  (expected AttributeError/ImportError)
    WeftClient,
    WeftError,
    RoomResult,
    RoomJoinResult,
    RoomInfo,
    RoomMember,
    RoomSendResult,
    RoomPoll,
    RoomEvent,
)
from tests._process_cleanup import cleanup_tempdir, stop_subprocess

# Names that MUST be exported from weft_sdk for the tests to even load.
_REQUIRED_EXPORTS = [
    "RoomResult",
    "RoomJoinResult",
    "RoomInfo",
    "RoomMember",
    "RoomSendResult",
    "RoomPoll",
    "RoomEvent",
]


def _pick_free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _wait_tcp(host: str, port: int, timeout_s: float = 15.0) -> None:
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        try:
            with socket.create_connection((host, port), timeout=0.5):
                return
        except OSError:
            time.sleep(0.1)
    raise RuntimeError(f"coordinator did not accept TCP on {host}:{port}")


class _RoomHarness:
    """Shared setup: spawn coordinator, register agents, expose public API only.

    Registered with atexit so a coordinator is never orphaned even if
    setUpClass raises before tearDownClass can run.
    """

    _live: list["_RoomHarness"] = []

    def __init__(self) -> None:
        self.scratch = tempfile.TemporaryDirectory(prefix="weft-sdk-rooms-")
        self.workspace = Path(self.scratch.name)
        self.port = _pick_free_port()
        self.base_url = f"http://127.0.0.1:{self.port}/mcp"
        self.proc: subprocess.Popen | None = None
        self.clients: dict[str, WeftClient] = {}
        self.tokens: dict[str, str] = {}
        self._live.append(self)

    def start(self, n_agents: int = 4) -> None:
        self.proc = subprocess.Popen(
            [
                sys.executable,
                "-B",
                "scripts/weft-mcp.py",
                "--transport",
                "http",
                "--host",
                "127.0.0.1",
                "--port",
                str(self.port),
                "--team-id",
                "demo",
                "--workspace",
                str(self.workspace),
                "--state",
                str(self.workspace / ".weft" / "state.db"),
            ],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            cwd=str(PROJECT_ROOT),
            encoding="utf-8",
            errors="replace",
        )
        _wait_tcp("127.0.0.1", self.port)
        # Register N distinct agents; each registers to get its own actor_token.
        for i in range(n_agents):
            name = f"agent-{i}"
            bootstrap = WeftClient(self.base_url, name, "demo")
            reg = bootstrap.register(name=name, role="generalist")
            self.tokens[name] = reg["actor_token"]
            bootstrap.close()
            self.clients[name] = WeftClient(
                self.base_url, name, "demo", actor_token=self.tokens[name],
            )

    def teardown(self) -> None:
        for c in self.clients.values():
            c.close()
        if self.proc is not None:
            stop_subprocess(self.proc)
        cleanup_tempdir(self.scratch)


def _kill_orphaned_coordinators() -> None:
    for h in list(_RoomHarness._live):
        if h.proc is not None and h.proc.poll() is None:
            stop_subprocess(h.proc)
        h.proc = None
    _RoomHarness._live.clear()


atexit.register(_kill_orphaned_coordinators)


# ---------------------------------------------------------------------------
# Test suite
# ---------------------------------------------------------------------------


class SdkRoomsContractTest(unittest.TestCase):
    """Public-room-API contract tests."""

    harness: _RoomHarness | None = None

    @classmethod
    def setUpClass(cls) -> None:
        # Fail fast with a clear message if the SDK hasn't exported the names.
        missing = [n for n in _REQUIRED_EXPORTS if not hasattr(sys.modules["weft_sdk"], n)]
        if missing:
            raise ImportError(
                f"weft_sdk is missing room dataclasses: {missing} — "
                "the public SDK room contract is incomplete.",
            )
        cls.harness = _RoomHarness()
        cls.harness.start(n_agents=4)

    @classmethod
    def tearDownClass(cls) -> None:
        if cls.harness is not None:
            cls.harness.teardown()

    # -- 1. create_room -----------------------------------------------------

    def test_01_create_room_returns_contract_dataclass(self) -> None:
        """create_room returns RoomResult; room_id starts 'room_'; owner auto-joined."""
        a = self.harness.clients["agent-0"]
        res: RoomResult = a.create_room(cap=4, name="alpha", ttl_seconds=86400)

        self.assertIsInstance(res, RoomResult)
        self.assertTrue(res.room_id.startswith("room_"), f"bad room_id: {res.room_id}")
        self.assertTrue(res.link_id.startswith("link_"), f"bad link_id: {res.link_id}")
        self.assertTrue(res.link_token.startswith("rm_"), f"bad link_token: {res.link_token}")
        self.assertIsInstance(res.expires_at, float)
        self.assertGreater(res.expires_at, time.time())
        self.assertEqual(res.cap, 4)
        self.assertEqual(res.state, "forming")
        self.assertEqual(res.owner_agent_id, "agent-0")
        # Shareable link is an absolute URL embedding the token.
        self.assertTrue(res.shareable_link.startswith(("http://", "https://")),
                        f"bad shareable_link: {res.shareable_link}")
        self.assertTrue(res.shareable_link.endswith(f"/j/{res.link_token}"))

        # Owner auto-joined: info shows member_count 1, roster lists owner.
        info: RoomInfo = a.room_info(res.room_id)
        self.assertEqual(info.member_count, 1)
        self.assertEqual(info.owner_agent_id, "agent-0")
        roster = a.roster(res.room_id)
        self.assertEqual(len(roster), 1)
        self.assertEqual(roster[0].agent_id, "agent-0")
        self.assertEqual(roster[0].status, "active")

    # -- 2. join_room: three distinct agents --------------------------------

    def test_02_join_room_three_agents(self) -> None:
        """Three distinct agents join one room link; info + roster reflect all 3."""
        a = self.harness.clients["agent-0"]
        res = a.create_room(cap=4, name="three")

        # Each agent registers (already done in harness) then joins via public API.
        join_tokens: dict[str, RoomJoinResult] = {}
        for name in ["agent-1", "agent-2"]:
            jr = self.harness.clients[name].join_room(
                res.room_id, res.link_token, consent=True, capabilities=["read"],
            )
            self.assertIsInstance(jr, RoomJoinResult)
            self.assertEqual(jr.room_id, res.room_id)
            self.assertEqual(jr.agent_id, name)
            self.assertEqual(jr.status, "active")
            self.assertIsInstance(jr.joined_at, str)
            self.assertEqual(jr.cursor, 0)
            join_tokens[name] = jr

        info = a.room_info(res.room_id)
        self.assertEqual(info.member_count, 3)  # owner + 2 joiners
        self.assertEqual(info.state, "active")
        roster = a.roster(res.room_id)
        self.assertEqual(sorted(m.agent_id for m in roster),
                         ["agent-0", "agent-1", "agent-2"])
        for m in roster:
            self.assertIsInstance(m, RoomMember)
        # The two agents that JOINED with capabilities=["read"] carry them;
        # the owner auto-joins with an empty manifest.
        caps = {m.agent_id: m.capabilities for m in roster}
        self.assertIn("read", caps["agent-1"])
        self.assertIn("read", caps["agent-2"])

    # -- 3. send: unicast / group / broadcast -------------------------------

    def test_03_send_unicast_group_broadcast(self) -> None:
        """Unicast → 1 receipt; group → N group receipts; broadcast → all others."""
        a = self.harness.clients["agent-0"]
        res = a.create_room(cap=4, name="send")
        for name in ["agent-1", "agent-2"]:
            self.harness.clients[name].join_room(res.room_id, res.link_token, consent=True)

        # --- Unicast A -> B ---
        sb: RoomSendResult = a.send(
            res.room_id, target="agent-1", payload={"text": "hi B"},
        )
        self.assertIsInstance(sb, RoomSendResult)
        self.assertEqual(sb.room_id, res.room_id)
        self.assertEqual([r["agent_id"] for r in sb.receipts], ["agent-1"])
        self.assertIsInstance(sb.envelope, dict)
        self.assertIsInstance(sb.seq, int)

        # --- Group: add B+C to "builders", send to group ---
        a.add_to_group(res.room_id, "builders", ["agent-1", "agent-2"])
        sg = a.send(res.room_id, target="builders", payload={"text": "hi group"})
        self.assertEqual(sorted(r["agent_id"] for r in sg.receipts),
                         ["agent-1", "agent-2"])

        # --- Broadcast "*" → all OTHER active members (sender excluded) ---
        sbc = a.send(res.room_id, target="*", payload={"text": "hi all"})
        self.assertEqual(sorted(r["agent_id"] for r in sbc.receipts),
                         ["agent-1", "agent-2"])

        # --- exclude_sender=False includes sender ---
        sbc_incl = a.send(res.room_id, target="*", payload={"text": "incl"},
                          exclude_sender=False)
        self.assertEqual(sorted(r["agent_id"] for r in sbc_incl.receipts),
                         ["agent-0", "agent-1", "agent-2"])

    # -- 4. poll + ack: ordered events, cursor advancement ------------------

    def test_04_poll_and_ack_cursor(self) -> None:
        """Poll returns ordered strictly-increasing seq; ack advances cursor."""
        a = self.harness.clients["agent-0"]
        res = a.create_room(cap=4, name="poll")
        for name in ["agent-1", "agent-2"]:
            self.harness.clients[name].join_room(res.room_id, res.link_token, consent=True)

        # Poll from 0: must include room.created + 2 joins, strictly increasing seq.
        rp: RoomPoll = a.room_poll(res.room_id, after_seq=0)
        self.assertIsInstance(rp, RoomPoll)
        self.assertEqual(rp.room_id, res.room_id)
        seqs = [e.seq for e in rp.events]
        self.assertEqual(seqs, sorted(set(seqs)), "events ordered, no dupes")
        self.assertEqual(seqs, sorted(seqs), "strictly increasing seq")
        kinds = [e.kind for e in rp.events]
        self.assertIn("room.created", kinds)
        self.assertEqual(kinds.count("room.joined"), 3)  # owner + 2

        # Ack to head.
        head = rp.cursor_head
        acked = a.room_ack(res.room_id, head)
        self.assertEqual(acked, head, "room_ack returns last_ack_seq")

        # Polling from last_ack_seq returns nothing new.
        rp2 = a.room_poll(res.room_id, after_seq=acked)
        self.assertEqual(len(rp2.events), 0, "no already-acked events replayed")
        self.assertFalse(rp2.has_more)

    # -- 5. group_members / remove_from_group -------------------------------

    def test_05_group_members_and_remove(self) -> None:
        """group_members lists group; remove_from_group removes."""
        a = self.harness.clients["agent-0"]
        res = a.create_room(cap=4, name="groups")
        for name in ["agent-1", "agent-2"]:
            self.harness.clients[name].join_room(res.room_id, res.link_token, consent=True)

        a.add_to_group(res.room_id, "builders", ["agent-1", "agent-2"])
        members = a.group_members(res.room_id, "builders")
        self.assertEqual(sorted(members), ["agent-1", "agent-2"])

        a.remove_from_group(res.room_id, "builders", ["agent-1"])
        members_after = a.group_members(res.room_id, "builders")
        self.assertEqual(members_after, ["agent-2"])

    # -- 6. leave + re-join -------------------------------------------------

    def test_06_leave_then_rejoin(self) -> None:
        """leave_room marks left; re-join reactivates."""
        a = self.harness.clients["agent-0"]
        res = a.create_room(cap=4, name="leave")
        self.harness.clients["agent-1"].join_room(res.room_id, res.link_token, consent=True)

        info_before = a.room_info(res.room_id)
        self.assertEqual(info_before.member_count, 2)

        left = self.harness.clients["agent-1"].leave_room(res.room_id)
        self.assertEqual(left["status"], "left")

        info_after = a.room_info(res.room_id)
        self.assertEqual(info_after.member_count, 1)

        # Re-join reactivates.
        jr = self.harness.clients["agent-1"].join_room(res.room_id, res.link_token, consent=True)
        self.assertEqual(jr.status, "active")
        info_rejoin = a.room_info(res.room_id)
        self.assertEqual(info_rejoin.member_count, 2)

    # -- 7. close_room + revoke_link ----------------------------------------

    def test_07_close_and_revoke(self) -> None:
        """close_room sets state closed; revoke_link returns revoked:True."""
        a = self.harness.clients["agent-0"]
        res = a.create_room(cap=4, name="close")

        # Revoke BEFORE close: owner revokes the link.
        rv = a.revoke_link(res.room_id, res.link_id)
        self.assertTrue(rv["revoked"])

        closed = a.close_room(res.room_id)
        self.assertEqual(closed["state"], "closed")

        info = a.room_info(res.room_id)
        self.assertEqual(info.state, "closed")

    # -- 8. heartbeat + receipts --------------------------------------------

    def test_08_heartbeat_and_receipts(self) -> None:
        """room_heartbeat refreshes presence; room_receipts returns status list."""
        a = self.harness.clients["agent-0"]
        res = a.create_room(cap=4, name="hb")
        self.harness.clients["agent-1"].join_room(res.room_id, res.link_token, consent=True)

        hb = a.room_heartbeat(res.room_id)
        self.assertEqual(hb["status"], "active")

        sb = a.send(res.room_id, target="agent-1", payload={"text": "x"})
        entry_ids = [r["entry_id"] for r in sb.receipts]
        receipts = a.room_receipts(res.room_id, entry_ids)
        self.assertIsInstance(receipts, list)
        self.assertEqual(len(receipts), 1)
        self.assertIn("status", receipts[0])

    # -- 9. negative: non-member refused ------------------------------------

    def test_09_non_member_refused(self) -> None:
        """A non-member agent is refused with room_not_found (no existence oracle)."""
        a = self.harness.clients["agent-0"]
        res = a.create_room(cap=4, name="neg")

        # agent-3 never joins — must be refused on room_info.
        with self.assertRaises(WeftError) as ctx:
            self.harness.clients["agent-3"].room_info(res.room_id)
        self.assertEqual(ctx.exception.code, "room_not_found")

    # -- 10. the private _call is never needed ------------------------------

    def test_10_public_api_only(self) -> None:
        """The whole suite must drive rooms through the public API only.

        This test asserts that every public room method exists on the client
        (the orchestrator must implement all of them). It intentionally does
        NOT call `_call` — that is the acceptance criterion for the wave.
        """
        a = self.harness.clients["agent-0"]
        required_methods = [
            "create_room",
            "join_room",
            "room_info",
            "roster",
            "leave_room",
            "close_room",
            "revoke_link",
            "send",
            "group_members",
            "add_to_group",
            "remove_from_group",
            "room_poll",
            "room_ack",
            "room_heartbeat",
            "room_receipts",
        ]
        for m in required_methods:
            self.assertTrue(
                callable(getattr(a, m, None)),
                f"WeftClient missing public room method: {m}",
            )

    # -- 11. message_kind via the public SDK ---------------------------------

    def test_11_message_kind_round_trip_through_public_sdk(self) -> None:
        """send(message_kind=...) + room_poll(message_kinds=[...]) round-trip."""
        a = self.harness.clients["agent-0"]
        res = a.create_room(cap=4, name="mk")
        for name in ["agent-1", "agent-2"]:
            self.harness.clients[name].join_room(res.room_id, res.link_token, consent=True)

        # Broadcast a status and a result.
        a.send(res.room_id, target="*", payload={"text": "liveness"}, message_kind="status")
        a.send(res.room_id, target="*", payload={"text": "the result"}, message_kind="result")

        # Filter on ["result"] — only the result event, with message_kind set.
        filtered: RoomPoll = a.room_poll(res.room_id, after_seq=0, message_kinds=["result"])
        result_events = [e for e in filtered.events if e.kind == "room.message"]
        self.assertEqual(len(result_events), 1)
        self.assertEqual(result_events[0].message_kind, "result")
        self.assertEqual(result_events[0].payload["payload"]["text"], "the result")

        # Unfiltered poll sees both, each carrying its message_kind.
        all_events: RoomPoll = a.room_poll(res.room_id, after_seq=0)
        msg_events = [e for e in all_events.events if e.kind == "room.message"]
        self.assertEqual(len(msg_events), 2)
        kinds = {e.message_kind for e in msg_events}
        self.assertEqual(kinds, {"status", "result"})


if __name__ == "__main__":
    unittest.main()
