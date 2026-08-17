from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from weft_mcp.metrics_activation import (
    derive_ttfvh,
    funnel_snapshot,
    handoffs_per_workspace,
    init,
    invite_to_activated_conversion,
    record_event,
    record_from_store_event,
    weekly_retention,
)


class MetricsActivationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.db_path = Path(self.temp.name) / "metrics.db"
        init(self.db_path)

    def tearDown(self) -> None:
        self.temp.cleanup()

    # ------------------------------------------------------------------
    # Funnel progression → ttfvh
    # ------------------------------------------------------------------
    def test_full_funnel_produces_ttfvh(self) -> None:
        """A complete activation funnel yields a positive ttfvh_ms."""
        team_id = "team-1"
        workspace_id = "ws-1"
        agent_id = "agent-a"

        record_event(team_id, agent_id, "link_created", {"workspace_id": workspace_id})
        record_event(team_id, agent_id, "link_previewed", {"workspace_id": workspace_id})
        record_event(team_id, agent_id, "link_accepted", {"workspace_id": workspace_id})
        record_event(team_id, agent_id, "first_task_claimed", {"workspace_id": workspace_id})
        record_event(team_id, agent_id, "first_evidence_verified", {"workspace_id": workspace_id})

        ttfvh = derive_ttfvh(team_id, workspace_id)
        self.assertIsNotNone(ttfvh)
        assert ttfvh is not None
        self.assertGreater(ttfvh, 0)

    def test_partial_funnel_gives_none_ttfvh(self) -> None:
        """Incomplete funnel (missing evidence verification) → None."""
        team_id = "team-1"
        workspace_id = "ws-1"

        record_event(team_id, "agent-a", "link_created", {"workspace_id": workspace_id})
        record_event(team_id, "agent-a", "link_previewed", {"workspace_id": workspace_id})
        record_event(team_id, "agent-a", "link_accepted", {"workspace_id": workspace_id})

        self.assertIsNone(derive_ttfvh(team_id, workspace_id))

    def test_ttfvh_measures_link_created_to_evidence_verified(self) -> None:
        """ttfvh = first_evidence_verified_at - link_created_at for the workspace."""
        team_id = "team-timing"
        workspace_id = "ws-timing"

        # Manually insert with known timestamps to verify math
        import sqlite3
        conn = sqlite3.connect(str(self.db_path))
        conn.execute("PRAGMA journal_mode = WAL")
        conn.execute(
            "INSERT INTO metrics_events(event_id, team_id, agent_id, event_type, occurred_at, metadata_json) VALUES (?, ?, ?, ?, ?, ?)",
            ("evt_a", team_id, "agent-a", "link_created", "2026-01-01T00:00:00.000Z", '{"workspace_id": "ws-timing"}'),
        )
        conn.execute(
            "INSERT INTO metrics_events(event_id, team_id, agent_id, event_type, occurred_at, metadata_json) VALUES (?, ?, ?, ?, ?, ?)",
            ("evt_b", team_id, "agent-a", "first_evidence_verified", "2026-01-01T00:02:30.000Z", '{"workspace_id": "ws-timing"}'),
        )
        conn.commit()
        conn.close()

        ttfvh = derive_ttfvh(team_id, workspace_id)
        self.assertIsNotNone(ttfvh)
        assert ttfvh is not None
        # 2 minutes 30 seconds = 150,000 ms
        self.assertEqual(ttfvh, 150_000)

    # ------------------------------------------------------------------
    # Funnel snapshot — per-stage counts and deltas
    # ------------------------------------------------------------------
    def test_funnel_snapshot_counts_and_deltas(self) -> None:
        team_id = "team-snap"
        # 10 links created, 6 previewed, 4 accepted, 3 claimed, 2 verified
        for i in range(10):
            record_event(team_id, "agent-a", "link_created", {"workspace_id": f"ws-{i}"})
        for i in range(6):
            record_event(team_id, "agent-a", "link_previewed", {"workspace_id": f"ws-{i}"})
        for i in range(4):
            record_event(team_id, "agent-a", "link_accepted", {"workspace_id": f"ws-{i}"})
        for i in range(3):
            record_event(team_id, "agent-a", "first_task_claimed", {"workspace_id": f"ws-{i}"})
        for i in range(2):
            record_event(team_id, "agent-a", "first_evidence_verified", {"workspace_id": f"ws-{i}"})

        snap = funnel_snapshot(team_id)
        self.assertEqual(snap["stages"]["link_created"], 10)
        self.assertEqual(snap["stages"]["link_previewed"], 6)
        self.assertEqual(snap["stages"]["link_accepted"], 4)
        self.assertEqual(snap["stages"]["first_task_claimed"], 3)
        self.assertEqual(snap["stages"]["first_evidence_verified"], 2)

        # Deltas: preview→accept = 4/6
        self.assertAlmostEqual(snap["deltas"]["preview_to_accept"], 4 / 6, places=4)
        # accept→claim = 3/4
        self.assertAlmostEqual(snap["deltas"]["accept_to_claim"], 3 / 4, places=4)
        # claim→verify = 2/3
        self.assertAlmostEqual(snap["deltas"]["claim_to_verify"], 2 / 3, places=4)

    # ------------------------------------------------------------------
    # Weekly retention windowing
    # ------------------------------------------------------------------
    def test_weekly_retention_counts_workspaces_with_evidence_in_week(self) -> None:
        team_id = "team-ret"
        import sqlite3
        conn = sqlite3.connect(str(self.db_path))
        conn.execute("PRAGMA journal_mode = WAL")
        # Week of 2026-01-05 (Monday) — two workspaces verified that week
        conn.execute(
            "INSERT INTO metrics_events(event_id, team_id, agent_id, event_type, occurred_at, metadata_json) VALUES (?, ?, ?, ?, ?, ?)",
            ("evt_r1", team_id, "agent-a", "first_evidence_verified", "2026-01-06T10:00:00.000Z", '{"workspace_id": "ws-ret-1"}'),
        )
        conn.execute(
            "INSERT INTO metrics_events(event_id, team_id, agent_id, event_type, occurred_at, metadata_json) VALUES (?, ?, ?, ?, ?, ?)",
            ("evt_r2", team_id, "agent-a", "first_evidence_verified", "2026-01-07T10:00:00.000Z", '{"workspace_id": "ws-ret-2"}'),
        )
        # Previous week — should NOT count
        conn.execute(
            "INSERT INTO metrics_events(event_id, team_id, agent_id, event_type, occurred_at, metadata_json) VALUES (?, ?, ?, ?, ?, ?)",
            ("evt_r3", team_id, "agent-a", "first_evidence_verified", "2026-01-04T10:00:00.000Z", '{"workspace_id": "ws-ret-3"}'),
        )
        conn.commit()
        conn.close()

        result = weekly_retention(team_id, "2026-01-05")
        self.assertEqual(result["retained_workspaces"], 2)
        self.assertIn("ws-ret-1", result["workspace_ids"])
        self.assertIn("ws-ret-2", result["workspace_ids"])

    # ------------------------------------------------------------------
    # Handoffs per workspace
    # ------------------------------------------------------------------
    def test_handoffs_per_workspace(self) -> None:
        team_id = "team-hpw"
        import sqlite3
        conn = sqlite3.connect(str(self.db_path))
        conn.execute("PRAGMA journal_mode = WAL")
        # ws-hpw-1: 3 evidence-verified handoffs
        for i in range(3):
            conn.execute(
                "INSERT INTO metrics_events(event_id, team_id, agent_id, event_type, occurred_at, metadata_json) VALUES (?, ?, ?, ?, ?, ?)",
                (f"evt_hpw_1_{i}", team_id, "agent-a", "first_evidence_verified", f"2026-01-0{i+1}T10:00:00.000Z", '{"workspace_id": "ws-hpw-1"}'),
            )
        # ws-hpw-2: 1 evidence-verified handoff
        conn.execute(
            "INSERT INTO metrics_events(event_id, team_id, agent_id, event_type, occurred_at, metadata_json) VALUES (?, ?, ?, ?, ?, ?)",
            ("evt_hpw_2_0", team_id, "agent-a", "first_evidence_verified", "2026-01-01T10:00:00.000Z", '{"workspace_id": "ws-hpw-2"}'),
        )
        conn.commit()
        conn.close()

        result = handoffs_per_workspace(team_id)
        self.assertEqual(result["active_workspaces"], 2)
        self.assertEqual(result["total_handoffs"], 4)
        self.assertAlmostEqual(result["handoffs_per_workspace_mean"], 2.0, places=2)

    # ------------------------------------------------------------------
    # Invite-to-activated conversion
    # ------------------------------------------------------------------
    def test_invite_to_activated_conversion(self) -> None:
        team_id = "team-inv"
        # 5 links created (invites), 2 reached evidence-verified (activated)
        for i in range(5):
            record_event(team_id, "agent-a", "link_created", {"workspace_id": f"ws-inv-{i}"})
        for i in range(2):
            record_event(team_id, "agent-a", "link_previewed", {"workspace_id": f"ws-inv-{i}"})
            record_event(team_id, "agent-a", "link_accepted", {"workspace_id": f"ws-inv-{i}"})
            record_event(team_id, "agent-a", "first_task_claimed", {"workspace_id": f"ws-inv-{i}"})
            record_event(team_id, "agent-a", "first_evidence_verified", {"workspace_id": f"ws-inv-{i}"})

        result = invite_to_activated_conversion(team_id)
        self.assertEqual(result["invites"], 5)
        self.assertEqual(result["activated"], 2)
        self.assertAlmostEqual(result["conversion_rate"], 2 / 5, places=4)

    # ------------------------------------------------------------------
    # Event idempotency
    # ------------------------------------------------------------------
    def test_duplicate_event_id_does_not_double_count(self) -> None:
        team_id = "team-idem"
        workspace_id = "ws-idem"

        # Insert same event_id twice — second should be ignored
        import sqlite3
        conn = sqlite3.connect(str(self.db_path))
        conn.execute("PRAGMA journal_mode = WAL")
        conn.execute(
            "INSERT INTO metrics_events(event_id, team_id, agent_id, event_type, occurred_at, metadata_json) VALUES (?, ?, ?, ?, ?, ?)",
            ("evt_idem_1", team_id, "agent-a", "link_created", "2026-01-01T00:00:00.000Z", '{"workspace_id": "ws-idem"}'),
        )
        # Duplicate — should violate PRIMARY KEY and be ignored
        try:
            conn.execute(
                "INSERT INTO metrics_events(event_id, team_id, agent_id, event_type, occurred_at, metadata_json) VALUES (?, ?, ?, ?, ?, ?)",
                ("evt_idem_1", team_id, "agent-a", "link_created", "2026-01-01T00:00:00.000Z", '{"workspace_id": "ws-idem"}'),
            )
        except Exception:
            pass
        conn.commit()
        conn.close()

        snap = funnel_snapshot(team_id)
        self.assertEqual(snap["stages"]["link_created"], 1)

    # ------------------------------------------------------------------
    # Restart durability
    # ------------------------------------------------------------------
    def test_restart_durability(self) -> None:
        team_id = "team-dur"
        workspace_id = "ws-dur"

        record_event(team_id, "agent-a", "link_created", {"workspace_id": workspace_id})
        record_event(team_id, "agent-a", "link_previewed", {"workspace_id": workspace_id})

        # Reopen the file (simulate restart)
        init(self.db_path)

        record_event(team_id, "agent-a", "link_accepted", {"workspace_id": workspace_id})
        snap = funnel_snapshot(team_id)
        self.assertEqual(snap["stages"]["link_created"], 1)
        self.assertEqual(snap["stages"]["link_previewed"], 1)
        self.assertEqual(snap["stages"]["link_accepted"], 1)

    # ------------------------------------------------------------------
    # No-PII contract
    # ------------------------------------------------------------------
    def test_no_pii_columns_in_events_table(self) -> None:
        """The events table must not contain PII-bearing columns."""
        import sqlite3
        conn = sqlite3.connect(str(self.db_path))
        conn.row_factory = sqlite3.Row
        record_event("team-pii", "agent-a", "link_created", {"workspace_id": "ws-pii"})
        cols = [row[1] for row in conn.execute("PRAGMA table_info(metrics_events)").fetchall()]
        conn.close()

        # Must-have columns
        for required in ("event_id", "team_id", "agent_id", "event_type", "occurred_at", "metadata_json"):
            self.assertIn(required, cols)

        # Must-NOT-have columns (PII)
        for forbidden in ("email", "ip_address", "name", "phone", "user_agent", "geo"):
            self.assertNotIn(forbidden, cols)

    # ------------------------------------------------------------------
    # record_from_store_event integration seam
    # ------------------------------------------------------------------
    def test_record_from_store_event_maps_known_types(self) -> None:
        """The integration hook maps store events to activation events."""
        team_id = "team-hook"
        workspace_id = "ws-hook"

        # Simulate a store event for pairing creation (link_created)
        store_event = {
            "event_type": "pairing.created",
            "team_id": team_id,
            "actor_id": "agent-a",
            "object_id": "pairing-1",
            "payload": {"workspace_id": workspace_id},
        }
        record_from_store_event(store_event)

        snap = funnel_snapshot(team_id)
        self.assertEqual(snap["stages"]["link_created"], 1)

    def test_record_from_store_event_ignores_unknown_types(self) -> None:
        """Unknown store events are silently dropped (no crash, no insert)."""
        store_event = {
            "event_type": "some.unknown.event",
            "team_id": "team-x",
            "actor_id": "agent-x",
            "object_id": "obj-x",
            "payload": {},
        }
        # Should not raise
        record_from_store_event(store_event)
        snap = funnel_snapshot("team-x")
        self.assertEqual(sum(snap["stages"].values()), 0)

    # ------------------------------------------------------------------
    # Idempotent init
    # ------------------------------------------------------------------
    def test_init_is_idempotent(self) -> None:
        """Calling init() multiple times does not error or lose data."""
        init(self.db_path)
        record_event("team-idem2", "agent-a", "link_created", {"workspace_id": "ws-idem2"})
        init(self.db_path)
        snap = funnel_snapshot("team-idem2")
        self.assertEqual(snap["stages"]["link_created"], 1)


if __name__ == "__main__":
    unittest.main()
