"""Integration tests for the Weft outbox MCP surface.

RED-only deliverable: the ``outbox_*`` tools are not wired into the
MCP dispatcher yet, so every ``call_tool`` below must fail. The orchestrator
registers these tools after this file lands.

Contracts asserted (mirrored from the outbox module + PROTOCOL.md v1 envelope):
  - outbox_enqueue  {team_id, envelope, recipients}
        -> {entry_ids: [str, ...], envelope_id: str}
  - outbox_claim    {team_id, limit, now}
        -> {entries: [{entry_id, envelope_id, recipient, payload, attempts, next_attempt_at}]}
  - outbox_delivered {team_id, entry_id}
        -> {entry_id, status: "delivered"}
  - outbox_retry    {team_id, entry_id}
        -> {entry_id, attempts, next_attempt_at}
  - outbox_stats    {team_id}
        -> {queued, in_flight, delivered, dead}
"""

from __future__ import annotations

import sys
import tempfile
import time
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from weft_mcp.core import WeftStore
from weft_mcp.server import WeftDispatcher, TOOLS


def _v1_envelope(sender: str = "agent-a", recipient: str = "agent-b", type_: str = "task.progress") -> dict:
    """Build a PROTOCOL.md v1 envelope shape (minimal valid fields)."""
    return {
        "protocol": "weft.a2a",
        "version": "1.0",
        "message_id": f"msg_{sender}_{recipient}_{int(time.time() * 1000)}",
        "type": type_,
        "sender": {"agent_id": sender},
        "recipient": {"agent_id": recipient},
        "payload": {"progress": 10, "note": "outbox integration probe"},
    }


class OutboxIntegrationTests(unittest.TestCase):
    """Drive the outbox through the REAL MCP JSON-RPC surface (call_tool)."""

    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        root = Path(self.temp.name)
        # Mirror test_actor_credentials_server.py harness exactly.
        self.store = WeftStore(root / "state.db", root, require_actor_auth=True)
        self.dispatcher = WeftDispatcher(self.store)
        self.team_id = "team-outbox"

    def tearDown(self) -> None:
        self.store.close()
        self.temp.cleanup()

    # ------------------------------------------------------------------
    # Contract 1: enqueue one envelope to 2 recipients -> 2 entry_ids, 1 envelope_id
    # ------------------------------------------------------------------
    def test_enqueue_fans_out_per_recipient_with_shared_envelope_id(self) -> None:
        envelope = _v1_envelope(recipient="agent-b")
        result = self.dispatcher.call_tool(
            "outbox_enqueue",
            {"team_id": self.team_id, "envelope": envelope, "recipients": ["agent-b", "agent-c"]},
        )
        self.assertIn("entry_ids", result)
        self.assertIn("envelope_id", result)
        self.assertEqual(len(result["entry_ids"]), 2)
        # Both entry ids are distinct strings.
        self.assertNotEqual(result["entry_ids"][0], result["entry_ids"][1])
        for entry_id in result["entry_ids"]:
            self.assertIsInstance(entry_id, str)
        self.assertIsInstance(result["envelope_id"], str)

    # ------------------------------------------------------------------
    # Contract 2: claim_due returns entries; re-claim returns empty (no double claim)
    # ------------------------------------------------------------------
    def test_claim_due_returns_entries_and_reclaim_is_empty(self) -> None:
        envelope = _v1_envelope(recipient="agent-b")
        enqueued = self.dispatcher.call_tool(
            "outbox_enqueue",
            {"team_id": self.team_id, "envelope": envelope, "recipients": ["agent-b", "agent-c"]},
        )
        claim = self.dispatcher.call_tool(
            "outbox_claim",
            {"team_id": self.team_id, "limit": 10, "now": time.time()},
        )
        self.assertIn("entries", claim)
        self.assertEqual(len(claim["entries"]), 2)
        returned_ids = {e["entry_id"] for e in claim["entries"]}
        self.assertEqual(returned_ids, set(enqueued["entry_ids"]))
        for entry in claim["entries"]:
            for key in ("entry_id", "envelope_id", "recipient", "payload", "attempts", "next_attempt_at"):
                self.assertIn(key, entry, f"claim entry missing field: {key}")

        # Second claim with same entries already in_flight -> empty.
        second = self.dispatcher.call_tool(
            "outbox_claim",
            {"team_id": self.team_id, "limit": 10, "now": time.time()},
        )
        self.assertEqual(second["entries"], [])

    # ------------------------------------------------------------------
    # Contract 3: mark_delivered -> stats reflects delivered >= 1
    # ------------------------------------------------------------------
    def test_mark_delivered_updates_stats(self) -> None:
        envelope = _v1_envelope(recipient="agent-b")
        enqueued = self.dispatcher.call_tool(
            "outbox_enqueue",
            {"team_id": self.team_id, "envelope": envelope, "recipients": ["agent-b", "agent-c"]},
        )
        claim = self.dispatcher.call_tool(
            "outbox_claim",
            {"team_id": self.team_id, "limit": 10, "now": time.time()},
        )
        target = claim["entries"][0]["entry_id"]

        delivered = self.dispatcher.call_tool(
            "outbox_delivered",
            {"team_id": self.team_id, "entry_id": target},
        )
        self.assertEqual(delivered["entry_id"], target)
        self.assertEqual(delivered["status"], "delivered")

        stats = self.dispatcher.call_tool("outbox_stats", {"team_id": self.team_id})
        self.assertIn("delivered", stats)
        self.assertGreaterEqual(stats["delivered"], 1)

    # ------------------------------------------------------------------
    # Contract 4: mark_retry increments attempts and schedules later next_attempt_at
    # ------------------------------------------------------------------
    def test_mark_retry_increments_attempts_and_reschedules(self) -> None:
        envelope = _v1_envelope(recipient="agent-b")
        enqueued = self.dispatcher.call_tool(
            "outbox_enqueue",
            {"team_id": self.team_id, "envelope": envelope, "recipients": ["agent-b"]},
        )
        claim = self.dispatcher.call_tool(
            "outbox_claim",
            {"team_id": self.team_id, "limit": 10, "now": time.time()},
        )
        entry_id = claim["entries"][0]["entry_id"]
        before = claim["entries"][0]["attempts"]
        before_next = claim["entries"][0]["next_attempt_at"]

        retried = self.dispatcher.call_tool(
            "outbox_retry",
            {"team_id": self.team_id, "entry_id": entry_id},
        )
        self.assertEqual(retried["entry_id"], entry_id)
        self.assertEqual(retried["attempts"], before + 1)
        # next_attempt_at must be strictly later than the prior value.
        self.assertGreater(retried["next_attempt_at"], before_next)

    # ------------------------------------------------------------------
    # Contract 5: tool schemas present in TOOLS for all five tool names
    # ------------------------------------------------------------------
    def test_tool_schemas_registered_in_tools_list(self) -> None:
        schemas = {tool["name"]: tool["inputSchema"] for tool in TOOLS}
        expected_tools = {
            "outbox_enqueue",
            "outbox_claim",
            "outbox_delivered",
            "outbox_retry",
            "outbox_stats",
        }
        for tool_name in expected_tools:
            self.assertIn(tool_name, schemas, f"missing schema for {tool_name}")
        # Envelope arg must accept an object payload.
        enqueue_props = schemas["outbox_enqueue"]["properties"]
        self.assertIn("team_id", enqueue_props)
        self.assertIn("envelope", enqueue_props)
        self.assertIn("recipients", enqueue_props)

    # ------------------------------------------------------------------
    # Contract 6: durability — reopen a NEW store on the same state.db; entries survive
    # ------------------------------------------------------------------
    def test_durability_survives_store_reopen(self) -> None:
        envelope = _v1_envelope(recipient="agent-b")
        self.dispatcher.call_tool(
            "outbox_enqueue",
            {"team_id": self.team_id, "envelope": envelope, "recipients": ["agent-b", "agent-c"]},
        )
        # Close current store to flush WAL, then reopen a fresh one on the same db.
        self.store.close()
        root = Path(self.temp.name)
        reopened = WeftStore(root / "state.db", root, require_actor_auth=True)
        new_dispatcher = WeftDispatcher(reopened)

        claim = new_dispatcher.call_tool(
            "outbox_claim",
            {"team_id": self.team_id, "limit": 10, "now": time.time()},
        )
        try:
            self.assertEqual(len(claim["entries"]), 2)
        finally:
            reopened.close()


if __name__ == "__main__":
    unittest.main()
