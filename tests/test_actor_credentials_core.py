from __future__ import annotations

import hashlib
import sqlite3
import sys
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from weft_mcp.core import FinalismaError, FinalismaStore


class ActorCredentialCoreTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.state_path = self.root / ".finalisma" / "state.db"
        self.stores: list[FinalismaStore] = []

    def tearDown(self) -> None:
        for store in self.stores:
            store.close()
        self.temp.cleanup()

    def store(self, *, require_actor_auth: bool = False) -> FinalismaStore:
        store = FinalismaStore(
            self.state_path,
            self.root,
            heartbeat_timeout=30,
            require_actor_auth=require_actor_auth,
        )
        self.stores.append(store)
        return store

    def assert_error(self, code: str, call, *args, **kwargs) -> FinalismaError:
        with self.assertRaises(FinalismaError) as caught:
            call(*args, **kwargs)
        self.assertEqual(caught.exception.code, code)
        return caught.exception

    def test_new_registration_issues_once_and_stores_only_hash(self) -> None:
        store = self.store()
        registered = store.register_agent("team", "agent-a", "A")
        token = registered["actor_token"]
        self.assertTrue(token.startswith("fst_actor_"))

        with store._read() as connection:
            credential = connection.execute(
                "SELECT * FROM agent_credentials WHERE team_id = ? AND agent_id = ?",
                ("team", "agent-a"),
            ).fetchone()
            events = connection.execute("SELECT payload_json FROM events").fetchall()
        self.assertEqual(credential["token_hash"], hashlib.sha256(token.encode("utf-8")).hexdigest())
        self.assertNotEqual(credential["token_hash"], token)
        self.assertTrue(all(token not in row["payload_json"] for row in events))
        self.assertNotIn(token.encode("utf-8"), self.state_path.read_bytes())

        repeated = store.register_agent("team", "agent-a", "A2")
        self.assertNotIn("actor_token", repeated)

    def test_auth_required_registration_prevents_silent_overwrite(self) -> None:
        trusted = self.store()
        token = trusted.register_agent("team", "agent-a", "Original")["actor_token"]
        secured = self.store(require_actor_auth=True)

        self.assert_error(
            "actor_auth_required", secured.register_agent, "team", "agent-a", "No proof"
        )
        self.assert_error(
            "actor_auth_invalid",
            secured.register_agent,
            "team",
            "agent-a",
            "Wrong proof",
            actor_token="fst_actor_wrong_wrong_wrong",
        )
        with secured._read() as connection:
            name = connection.execute(
                "SELECT name FROM agents WHERE team_id = ? AND agent_id = ?",
                ("team", "agent-a"),
            ).fetchone()["name"]
        self.assertEqual(name, "Original")

        updated = secured.register_agent("team", "agent-a", "Updated", actor_token=token)
        self.assertEqual(updated["name"], "Updated")
        self.assertNotIn("actor_token", updated)

    def test_wrong_agent_and_team_tokens_are_denied(self) -> None:
        trusted = self.store()
        token_a = trusted.register_agent("team", "agent-a")["actor_token"]
        trusted.register_agent("team", "agent-b")
        trusted.register_agent("other", "agent-a")
        secured = self.store(require_actor_auth=True)

        self.assert_error(
            "actor_auth_invalid", secured.heartbeat, "team", "agent-b", actor_token=token_a
        )
        self.assert_error(
            "actor_auth_invalid", secured.heartbeat, "other", "agent-a", actor_token=token_a
        )

    def test_auth_required_work_plane_and_message_methods(self) -> None:
        trusted = self.store()
        token_a = trusted.register_agent("team", "agent-a", capabilities=["planning"])["actor_token"]
        token_b = trusted.register_agent("team", "agent-b", capabilities=["coding"])["actor_token"]
        secured = self.store(require_actor_auth=True)

        self.assert_error(
            "actor_auth_required", secured.create_task, "team", "agent-a", "Missing proof"
        )
        route = secured.route_task(
            "team", "Implement artifact", "Write code", agent_id="agent-a", actor_token=token_a
        )
        self.assertEqual(route["team_id"], "team")
        status = secured.team_status("team", agent_id="agent-a", actor_token=token_a)
        self.assertEqual(status["team_id"], "team")

        created = secured.create_task(
            "team",
            "agent-a",
            "Implement artifact",
            "Write and verify it",
            scope=["artifact.txt"],
            preferred_agent="agent-b",
            actor_token=token_a,
        )
        task_id = created["task"]["task_id"]
        claimed = secured.claim_task("team", "agent-b", task_id, actor_token=token_b)
        secured.update_task(
            "team",
            "agent-b",
            task_id,
            progress=50,
            fencing_token=claimed["fencing_token"],
            actor_token=token_b,
        )
        secured.heartbeat(
            "team",
            "agent-b",
            task_ids=[task_id],
            fencing_tokens={task_id: claimed["fencing_token"]},
            actor_token=token_b,
        )
        (self.root / "artifact.txt").write_text("verified\n", encoding="utf-8")
        verified = secured.verify_task(
            "team",
            "agent-b",
            task_id,
            claimed["fencing_token"],
            ["artifact.txt"],
            [{"name": "unit", "status": "passed", "evidence": "ok"}],
            actor_token=token_b,
        )
        self.assertTrue(verified["passed"])
        completed = secured.complete_task(
            "team",
            "agent-b",
            task_id,
            claimed["fencing_token"],
            actor_token=token_b,
        )
        self.assertEqual(completed["status"], "done")

        sent = secured.send_message(
            "team", "agent-a", "team.notice", {"ok": True}, recipient_id="agent-b", actor_token=token_a
        )
        inbox = secured.read_inbox(
            "team", "agent-b", acknowledge=False, actor_token=token_b
        )
        self.assertEqual(inbox["count"], 1)
        acknowledged = secured.acknowledge_message(
            "team", "agent-b", sent["message"]["message_id"], actor_token=token_b
        )
        self.assertTrue(acknowledged["acknowledged"])

    def test_pairing_new_identity_bootstrap_and_existing_identity_protection(self) -> None:
        secured = self.store(require_actor_auth=True)
        initiator_token = secured.register_agent("team", "initiator")["actor_token"]
        existing_token = secured.register_agent("team", "existing")["actor_token"]

        self.assert_error(
            "actor_auth_required", secured.create_pairing, "initiator", "team"
        )
        pairing = secured.create_pairing("initiator", "team", actor_token=initiator_token)
        joined = secured.join_pairing(pairing["join_token"], "new-agent", consent=True)
        self.assertIn("actor_token", joined)

        second = secured.create_pairing("initiator", "team", actor_token=initiator_token)
        self.assert_error(
            "actor_auth_required",
            secured.join_pairing,
            second["join_token"],
            "existing",
            consent=True,
        )
        self.assert_error(
            "actor_auth_invalid",
            secured.join_pairing,
            second["join_token"],
            "existing",
            consent=True,
            actor_token=joined["actor_token"],
        )
        joined_existing = secured.join_pairing(
            second["join_token"], "existing", consent=True, actor_token=existing_token
        )
        self.assertNotIn("actor_token", joined_existing)

    def test_rotation_invalidates_old_token_and_is_atomic(self) -> None:
        trusted = self.store()
        old_token = trusted.register_agent("team", "agent-a")["actor_token"]
        secured = self.store(require_actor_auth=True)

        self.assert_error(
            "actor_auth_required", secured.rotate_agent_credential, "team", "agent-a"
        )

        def rotate() -> tuple[str, str | None]:
            try:
                result = secured.rotate_agent_credential("team", "agent-a", old_token)
                return ("ok", result["actor_token"])
            except FinalismaError as exc:
                return (exc.code, None)

        with ThreadPoolExecutor(max_workers=2) as pool:
            outcomes = list(pool.map(lambda _: rotate(), range(2)))
        successes = [item for item in outcomes if item[0] == "ok"]
        self.assertEqual(len(successes), 1)
        self.assertEqual(sum(item[0] == "actor_auth_invalid" for item in outcomes), 1)
        new_token = successes[0][1]
        assert new_token is not None

        self.assert_error(
            "actor_auth_invalid", secured.heartbeat, "team", "agent-a", actor_token=old_token
        )
        self.assertEqual(
            secured.heartbeat("team", "agent-a", actor_token=new_token)["agent_id"], "agent-a"
        )
        with secured._read() as connection:
            row = connection.execute(
                "SELECT token_hash, rotation_count FROM agent_credentials WHERE team_id = ? AND agent_id = ?",
                ("team", "agent-a"),
            ).fetchone()
        self.assertEqual(row["token_hash"], hashlib.sha256(new_token.encode("utf-8")).hexdigest())
        self.assertEqual(row["rotation_count"], 1)

    def test_v2_migration_does_not_claim_credentials_and_trusted_rotation_recovers(self) -> None:
        self.state_path.parent.mkdir(parents=True, exist_ok=True)
        connection = sqlite3.connect(self.state_path)
        try:
            connection.executescript(
                """
                CREATE TABLE teams (
                    team_id TEXT PRIMARY KEY, name TEXT NOT NULL, created_at TEXT NOT NULL, settings_json TEXT NOT NULL
                );
                CREATE TABLE agents (
                    team_id TEXT NOT NULL, agent_id TEXT NOT NULL, name TEXT NOT NULL, role TEXT NOT NULL,
                    model TEXT, capabilities_json TEXT NOT NULL, status TEXT NOT NULL, last_seen REAL NOT NULL,
                    metadata_json TEXT NOT NULL, PRIMARY KEY (team_id, agent_id),
                    FOREIGN KEY (team_id) REFERENCES teams(team_id) ON DELETE CASCADE
                );
                CREATE TABLE schema_meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
                INSERT INTO teams VALUES ('team', 'team', '2026-01-01T00:00:00.000Z', '{}');
                INSERT INTO agents VALUES ('team', 'migrated', 'Migrated', 'worker', NULL, '[]', 'active', 1, '{}');
                INSERT INTO schema_meta VALUES ('schema_version', '2');
                """
            )
            connection.commit()
        finally:
            connection.close()

        trusted = self.store()
        self.assertEqual(trusted.health_status()["schema_version"], 3)
        with trusted._read() as connection:
            self.assertEqual(connection.execute("SELECT COUNT(*) FROM agent_credentials").fetchone()[0], 0)

        secured = self.store(require_actor_auth=True)
        self.assert_error(
            "actor_auth_required", secured.rotate_agent_credential, "team", "migrated"
        )
        recovered = trusted.rotate_agent_credential("team", "migrated")
        self.assertTrue(recovered["bootstrapped"])
        self.assertEqual(
            secured.heartbeat("team", "migrated", actor_token=recovered["actor_token"])["agent_id"],
            "migrated",
        )

    def test_default_trusted_mode_keeps_existing_calls_working(self) -> None:
        store = self.store()
        store.register_agent("team", "agent-a")
        store.register_agent("team", "agent-a", "Updated without proof")
        store.register_agent("team", "agent-b")
        task = store.create_task("team", "agent-a", "Trusted task")
        store.claim_task("team", "agent-b", task["task"]["task_id"])
        pairing = store.create_pairing("agent-a", "team")
        joined = store.join_pairing(pairing["join_token"], "agent-c", consent=True)
        self.assertIn("actor_token", joined)


if __name__ == "__main__":
    unittest.main()
