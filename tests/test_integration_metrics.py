"""Integration tests for the metrics_activation module via the REAL MCP surface.

These tests drive metrics through WeftDispatcher.call_tool — never through
metrics_activation.py's Python API directly. The metrics_* tools do
not exist yet; this file is the RED phase of TDD. The orchestrator will wire
the tools after this test is written.

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


if __name__ == "__main__":
    unittest.main()
