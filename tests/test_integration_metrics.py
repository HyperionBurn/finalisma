"""Integration tests for the metrics_activation module via the REAL MCP surface.

These tests drive metrics through WeftDispatcher.call_tool — never through
metrics_activation.py's Python API directly. The dispatcher must derive the
activation stages from the real pairing, task, and evidence lifecycle.

Activation funnel stages (in order):
    link_created → link_previewed → link_accepted → first_task_claimed → first_evidence_verified

Headline metric: time-to-first-verified-handoff (ttfvh_ms).
"""

from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from weft_mcp.core import WeftStore
from weft_mcp.server import WeftDispatcher, TOOLS


# The five activation funnel stages, in funnel order.
FUNNEL_STAGES = (
    "link_created",
    "link_previewed",
    "link_accepted",
    "first_task_claimed",
    "first_evidence_verified",
)

# The four tool names the orchestrator is expected to register.
METRIC_TOOL_NAMES = (
    "metrics_event",
    "metrics_funnel",
    "metrics_ttfvh",
    "metrics_retention",
)


class MetricsIntegrationTests(unittest.TestCase):
    """Drive metrics through the MCP JSON-RPC surface (call_tool)."""

    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        root = Path(self.temp.name)
        self.store = WeftStore(root / "state.db", root, require_actor_auth=True)
        self.dispatcher = WeftDispatcher(self.store)
        self.team_id = "team-metrics-1"
        self.agent_id = "agent-metrics-1"
        self.workspace_id = "ws-1"

    def tearDown(self) -> None:
        self.store.close()
        self.temp.cleanup()

    # ------------------------------------------------------------------
    # Helper: record one activation event through the MCP surface.
    # ------------------------------------------------------------------
    def _record_event(
        self,
        event_type: str,
        *,
        team_id: str | None = None,
        agent_id: str | None = None,
        workspace_id: str | None = None,
    ) -> dict:
        args = {
            "team_id": team_id or self.team_id,
            "agent_id": agent_id or self.agent_id,
            "event_type": event_type,
            "metadata": {"workspace_id": workspace_id or self.workspace_id},
        }
        return self.dispatcher.call_tool("metrics_event", args)

    # ------------------------------------------------------------------
    # Test 1 — Full funnel: all five stages, ttfvh positive.
    # ------------------------------------------------------------------
    def test_full_funnel_records_all_stages_and_derives_positive_ttfvh(self) -> None:
        # Record each funnel stage exactly once.
        for stage in FUNNEL_STAGES:
            result = self._record_event(stage)
            self.assertTrue(result["recorded"], f"event {stage} should be recorded")
            self.assertIn("event_id", result)

        # Funnel snapshot: every stage count >= 1.
        funnel = self.dispatcher.call_tool(
            "metrics_funnel", {"team_id": self.team_id}
        )
        funnel_stages = funnel["funnel"]
        for stage in FUNNEL_STAGES:
            self.assertGreaterEqual(
                funnel_stages[stage], 1,
                f"stage {stage} should have count >= 1 after full funnel",
            )

        # derive ttfvh: must be a positive int ms.
        ttfvh_result = self.dispatcher.call_tool(
            "metrics_ttfvh",
            {"team_id": self.team_id, "workspace_id": self.workspace_id},
        )
        ttfvh_ms = ttfvh_result["ttfvh_ms"]
        self.assertIsInstance(ttfvh_ms, int, "ttfvh_ms must be an int when funnel complete")
        self.assertGreater(ttfvh_ms, 0, "ttfvh_ms must be positive for a completed funnel")

    # ------------------------------------------------------------------
    # Test 2 — Partial funnel: ttfvh returns null.
    # ------------------------------------------------------------------
    def test_partial_funnel_returns_null_ttfvh(self) -> None:
        # Only link_created and link_accepted — never reaches first_evidence_verified.
        self._record_event("link_created")
        self._record_event("link_accepted")

        funnel = self.dispatcher.call_tool(
            "metrics_funnel", {"team_id": self.team_id}
        )
        self.assertEqual(funnel["funnel"]["link_created"], 1)
        self.assertEqual(funnel["funnel"]["link_accepted"], 1)
        self.assertEqual(funnel["funnel"]["first_evidence_verified"], 0)

        ttfvh_result = self.dispatcher.call_tool(
            "metrics_ttfvh",
            {"team_id": self.team_id, "workspace_id": self.workspace_id},
        )
        self.assertIsNone(
            ttfvh_result["ttfvh_ms"],
            "ttfvh_ms must be null when first_evidence_verified never occurred",
        )

    # ------------------------------------------------------------------
    # Test 3 — Event idempotency: same event recorded twice doesn't double count.
    # ------------------------------------------------------------------
    def test_event_idempotency_same_event_not_double_counted(self) -> None:
        # Record the same (team, agent, event_type, metadata) twice.
        args = {
            "team_id": self.team_id,
            "agent_id": self.agent_id,
            "event_type": "link_created",
            "metadata": {"workspace_id": self.workspace_id},
        }
        first = self.dispatcher.call_tool("metrics_event", args)
        second = self.dispatcher.call_tool("metrics_event", args)
        self.assertTrue(first["recorded"])
        self.assertTrue(second["recorded"])

        funnel = self.dispatcher.call_tool(
            "metrics_funnel", {"team_id": self.team_id}
        )
        self.assertEqual(
            funnel["funnel"]["link_created"],
            1,
            "duplicate event must not double the funnel count",
        )

    # ------------------------------------------------------------------
    # Test 4 — Retention returns ints >= 0.
    # ------------------------------------------------------------------
    def test_retention_returns_ints(self) -> None:
        # Record a verified event so retention has data.
        for stage in FUNNEL_STAGES:
            self._record_event(stage)

        result = self.dispatcher.call_tool(
            "metrics_retention",
            {"team_id": self.team_id, "week_start": "2026-08-03"},
        )
        self.assertIsInstance(result["retained_workspaces"], int)
        self.assertIsInstance(result["active_workspaces"], int)
        self.assertGreaterEqual(result["retained_workspaces"], 0)
        self.assertGreaterEqual(result["active_workspaces"], 0)

    # ------------------------------------------------------------------
    # Test 5 — Tool schemas present in TOOLS list for all four tool names.
    # ------------------------------------------------------------------
    def test_metric_tool_schemas_registered_in_tools_list(self) -> None:
        schemas = {tool["name"]: tool["inputSchema"] for tool in TOOLS}
        for tool_name in METRIC_TOOL_NAMES:
            self.assertIn(
                tool_name, schemas,
                f"{tool_name} must be registered in the TOOLS list",
            )

    def test_real_dispatcher_lifecycle_populates_funnel_transactionally(self) -> None:
        """Real pairing/task/evidence operations populate metrics without manual events."""
        initiator = self.dispatcher.call_tool(
            "register_agent",
            {"team_id": self.team_id, "agent_id": "agent-a", "name": "Initiator"},
        )
        initiator_token = initiator["actor_token"]
        pairing = self.dispatcher.call_tool(
            "create_pairing",
            {
                "team_id": self.team_id,
                "initiator_id": "agent-a",
                "metadata": {"workspace_id": self.workspace_id},
                "actor_token": initiator_token,
            },
        )

        # Preview twice. The same pairing must count once in the first-stage
        # funnel, even though the core records both reads.
        self.dispatcher.call_tool("pairing_preview", {"token": pairing["join_token"]})
        self.dispatcher.call_tool("pairing_preview", {"token": pairing["join_token"]})
        joined = self.dispatcher.call_tool(
            "join_pairing",
            {
                "token": pairing["join_token"],
                "agent_id": "agent-b",
                "consent": True,
            },
        )
        agent_b_token = joined["actor_token"]

        created = self.dispatcher.call_tool(
            "create_task",
            {
                "team_id": self.team_id,
                "created_by": "agent-a",
                "title": "Verify lifecycle metrics",
                "description": "Exercise the evidence gate",
                "scope": ["metric-artifact.txt"],
                "preferred_agent": "agent-b",
                "metadata": {"workspace_id": self.workspace_id},
                "actor_token": initiator_token,
            },
        )
        task_id = created["task"]["task_id"]
        claimed = self.dispatcher.call_tool(
            "claim_task",
            {
                "team_id": self.team_id,
                "agent_id": "agent-b",
                "task_id": task_id,
                "actor_token": agent_b_token,
            },
        )
        (self.store.workspace / "metric-artifact.txt").write_text("verified", encoding="utf-8")

        failed = self.dispatcher.call_tool(
            "verify_task",
            {
                "team_id": self.team_id,
                "agent_id": "agent-b",
                "task_id": task_id,
                "fencing_token": claimed["fencing_token"],
                "files": ["metric-artifact.txt"],
                "checks": [{"name": "release-check", "status": "failed"}],
                "actor_token": agent_b_token,
            },
        )
        self.assertFalse(failed["passed"])
        failed_funnel = self.dispatcher.call_tool("metrics_funnel", {"team_id": self.team_id})
        self.assertEqual(failed_funnel["funnel"]["first_evidence_verified"], 0)

        reclaimed = self.dispatcher.call_tool(
            "claim_task",
            {
                "team_id": self.team_id,
                "agent_id": "agent-b",
                "task_id": task_id,
                "actor_token": agent_b_token,
            },
        )
        passed = self.dispatcher.call_tool(
            "verify_task",
            {
                "team_id": self.team_id,
                "agent_id": "agent-b",
                "task_id": task_id,
                "fencing_token": reclaimed["fencing_token"],
                "files": ["metric-artifact.txt"],
                "checks": [{"name": "release-check", "status": "passed"}],
                "actor_token": agent_b_token,
            },
        )
        self.assertTrue(passed["passed"])

        funnel = self.dispatcher.call_tool("metrics_funnel", {"team_id": self.team_id})["funnel"]
        for stage in FUNNEL_STAGES:
            self.assertEqual(funnel[stage], 1, f"automatic stage {stage} must be recorded once")
        ttfvh = self.dispatcher.call_tool(
            "metrics_ttfvh",
            {"team_id": self.team_id, "workspace_id": self.workspace_id},
        )["ttfvh_ms"]
        self.assertIsInstance(ttfvh, int)
        self.assertGreater(ttfvh, 0)

        import sqlite3
        connection = sqlite3.connect(str(self.store.state_path))
        connection.row_factory = sqlite3.Row
        materialized = connection.execute(
            "SELECT * FROM metrics_funnels WHERE team_id = ? AND workspace_id = ?",
            (self.team_id, self.workspace_id),
        ).fetchone()
        claim_metadata = connection.execute(
            "SELECT metadata_json FROM metrics_events WHERE team_id = ? AND event_type = 'first_task_claimed'",
            (self.team_id,),
        ).fetchone()
        connection.close()
        self.assertIsNotNone(materialized)
        self.assertEqual(materialized["ttfvh_ms"], ttfvh)
        self.assertIsNotNone(claim_metadata)
        self.assertNotIn("fencing_token", claim_metadata["metadata_json"])
        self.assertNotIn("scope", claim_metadata["metadata_json"])

        # Reopen the actual store to prove persistence across process-style
        # shutdown, not only re-initialization around a live connection.
        state_path = self.store.state_path
        workspace_path = self.store.workspace
        self.store.close()
        self.store = WeftStore(
            state_path,
            workspace_path,
            require_actor_auth=True,
        )
        restarted = WeftDispatcher(self.store)
        restarted_funnel = restarted.call_tool("metrics_funnel", {"team_id": self.team_id})["funnel"]
        self.assertEqual(restarted_funnel, funnel)


if __name__ == "__main__":
    unittest.main()
