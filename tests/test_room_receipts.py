"""Cloud room receipt lifecycle tests over the real hosted HTTP surface.

Delivery state lives in ``cloud_outbox.status``.  Recipient consumption is a
separate durable ``cloud_room_receipts.read_status`` state, transitioned by
that recipient's cursor acknowledgement.
"""

from __future__ import annotations

import json
import shutil
import sys
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from http import HTTPStatus
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from weft_cloud.service import WeftCloudService, _CloudHTTPHandler
from weft_cloud.storage import SqliteWalBackend


def _post(base: str, path: str, body: dict) -> tuple[int, dict]:
    request = urllib.request.Request(
        base + path,
        data=json.dumps(body).encode("utf-8"),
        method="POST",
        headers={"Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(request, timeout=15) as response:
            raw = response.read()
            return response.status, json.loads(raw.decode("utf-8")) if raw else {}
    except urllib.error.HTTPError as exc:
        try:
            raw = exc.read()
            return exc.code, json.loads(raw.decode("utf-8")) if raw else {}
        finally:
            exc.close()


def _mcp(base: str, token: str, name: str, args: dict, request_id: int = 1) -> dict:
    body = {
        "jsonrpc": "2.0",
        "id": request_id,
        "method": "tools/call",
        "params": {"name": name, "arguments": args},
    }
    request = urllib.request.Request(
        base + "/mcp",
        data=json.dumps(body).encode("utf-8"),
        method="POST",
        headers={"Content-Type": "application/json", "Authorization": f"Bearer {token}"},
    )
    with urllib.request.urlopen(request, timeout=15) as response:
        payload = json.loads(response.read().decode("utf-8"))
    result = payload["result"]
    if result.get("isError"):
        text = result.get("content", [{}])[0].get("text", "{}")
        return {"error": json.loads(text).get("error", {})}
    return {"result": result.get("structuredContent")}


class RoomReceiptLifecycleTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        from http.server import ThreadingHTTPServer

        cls.tmpdir = tempfile.mkdtemp(prefix="weft-receipts-test-")
        cls.db_path = str(Path(cls.tmpdir) / "test.db")
        cls.httpd = ThreadingHTTPServer(("127.0.0.1", 0), _CloudHTTPHandler)
        cls.base = f"http://127.0.0.1:{cls.httpd.server_address[1]}"
        cls.service = WeftCloudService(SqliteWalBackend(cls.db_path))
        _CloudHTTPHandler.service = cls.service
        cls.thread = threading.Thread(target=cls.httpd.serve_forever, daemon=True)
        cls.thread.start()

    @classmethod
    def tearDownClass(cls) -> None:
        cls.httpd.shutdown()
        cls.httpd.server_close()
        try:
            cls.service.backend.close()
        finally:
            shutil.rmtree(cls.tmpdir, ignore_errors=True)

    def _signup(self, email: str, tenant_id: str | None = None) -> dict:
        body = {"email": email, "password": "password-123"}
        if tenant_id is not None:
            body["tenant_id"] = tenant_id
        status, response = _post(self.base, "/v1/auth/signup", body)
        self.assertEqual(status, HTTPStatus.CREATED, response)
        return response

    def _call(self, member: dict, name: str, args: dict, request_id: int = 1) -> dict:
        response = _mcp(self.base, member["session_token"], name, args, request_id)
        self.assertNotIn("error", response, response)
        return response["result"]

    def _room(self, prefix: str) -> tuple[dict, dict, dict, dict]:
        owner = self._signup(f"{prefix}-owner@example.com")
        first = self._signup(f"{prefix}-first@example.com", owner["tenant_id"])
        second = self._signup(f"{prefix}-second@example.com", owner["tenant_id"])
        created = self._call(owner, "room_create", {"cap": 5}, 1)
        for member in (first, second):
            self._call(member, "room_join", {
                "room_id": created["room_id"],
                "link_token": created["link_token"],
                "consent": True,
            }, 2)
        return owner, first, second, created

    def _rows(self, tenant_id: str, room_id: str, recipient: str, seq: int) -> list:
        with self.service.backend.transaction() as tx:
            return list(tx.execute(
                "SELECT * FROM cloud_room_receipts "
                "WHERE tenant_id = ? AND room_id = ? AND recipient_agent_id = ? AND seq = ?",
                (tenant_id, room_id, recipient, seq),
            ).fetchall())

    def test_send_persists_one_read_receipt_per_target(self) -> None:
        owner, first, second, room = self._room("rows")
        sent = self._call(owner, "room_send", {
            "room_id": room["room_id"], "target_spec": "*",
            "payload": {"text": "broadcast"},
        }, 10)
        with self.service.backend.transaction() as tx:
            rows = list(tx.execute(
                "SELECT * FROM cloud_room_receipts WHERE tenant_id = ? AND room_id = ? AND seq = ?",
                (owner["tenant_id"], room["room_id"], sent["seq"]),
            ).fetchall())
        self.assertEqual(sorted(r["recipient_agent_id"] for r in rows),
                         sorted((first["account_id"], second["account_id"])))
        self.assertTrue(all(r["read_status"] == "queued" for r in rows))
        self.assertTrue(all(r["sender_agent_id"] == owner["account_id"] for r in rows))
        self.assertTrue(all(r["read_status"] == "queued" for r in sent["receipts"]))

    def test_ack_marks_only_callers_receipts_read(self) -> None:
        owner, first, second, room = self._room("scoped")
        sent = self._call(owner, "room_send", {
            "room_id": room["room_id"], "target_spec": "*",
            "payload": {"text": "scoped"},
        }, 10)
        acked = self._call(first, "room_ack", {
            "room_id": room["room_id"], "seq": sent["seq"],
        }, 11)
        self.assertEqual(acked["receipts_read"], 1)
        self.assertEqual(self._rows(owner["tenant_id"], room["room_id"], first["account_id"], sent["seq"])[0]["read_status"], "read")
        self.assertEqual(self._rows(owner["tenant_id"], room["room_id"], second["account_id"], sent["seq"])[0]["read_status"], "queued")

    def test_ack_does_not_read_later_sequence(self) -> None:
        owner, first, _, room = self._room("partial")
        first_sent = self._call(owner, "room_send", {
            "room_id": room["room_id"], "target_spec": first["account_id"],
            "payload": {"text": "first"},
        }, 10)
        second_sent = self._call(owner, "room_send", {
            "room_id": room["room_id"], "target_spec": first["account_id"],
            "payload": {"text": "second"},
        }, 11)
        self._call(first, "room_ack", {
            "room_id": room["room_id"], "seq": first_sent["seq"],
        }, 12)
        self.assertEqual(self._rows(owner["tenant_id"], room["room_id"], first["account_id"], first_sent["seq"])[0]["read_status"], "read")
        self.assertEqual(self._rows(owner["tenant_id"], room["room_id"], first["account_id"], second_sent["seq"])[0]["read_status"], "queued")

    def test_read_status_survives_backend_restart_and_idempotent_replay(self) -> None:
        owner, first, _, room = self._room("durable")
        sent = self._call(owner, "room_send", {
            "room_id": room["room_id"], "target_spec": first["account_id"],
            "payload": {"text": "durable"}, "idempotency_key": "durable-receipt-key",
        }, 10)
        self._call(first, "room_ack", {
            "room_id": room["room_id"], "seq": sent["seq"],
        }, 11)
        self.service.backend.close()
        self.service.backend = SqliteWalBackend(self.db_path)
        replay = self._call(owner, "room_send", {
            "room_id": room["room_id"], "target_spec": first["account_id"],
            "payload": {"text": "durable"}, "idempotency_key": "durable-receipt-key",
        }, 12)
        self.assertEqual(replay["seq"], sent["seq"])
        self.assertEqual(replay["receipts"][0]["read_status"], "read")
        self.assertEqual(self._rows(owner["tenant_id"], room["room_id"], first["account_id"], sent["seq"])[0]["read_status"], "read")

    def test_unknown_target_creates_no_read_receipt(self) -> None:
        owner, _, _, room = self._room("ghost")
        response = _mcp(self.base, owner["session_token"], "room_send", {
            "room_id": room["room_id"], "target_spec": "key_ghost_0000",
            "payload": {"text": "ghost"},
        }, 10)
        self.assertEqual(response["error"]["code"], "recipient_not_found")
        with self.service.backend.transaction() as tx:
            count = tx.execute(
                "SELECT COUNT(*) AS c FROM cloud_room_receipts WHERE tenant_id = ? AND room_id = ?",
                (owner["tenant_id"], room["room_id"]),
            ).fetchone()["c"]
        self.assertEqual(count, 0)


_INTERIM_CLOUD_013_BODY = """
CREATE TABLE IF NOT EXISTS cloud_room_receipts (
    tenant_id TEXT NOT NULL,
    room_id TEXT NOT NULL,
    seq INTEGER NOT NULL,
    recipient_agent_id TEXT NOT NULL,
    sender_agent_id TEXT NOT NULL,
    entry_id TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'queued'
        CHECK(status IN ('queued','read')),
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    PRIMARY KEY (tenant_id, room_id, seq, recipient_agent_id)
);
CREATE INDEX IF NOT EXISTS idx_room_receipts_recipient
    ON cloud_room_receipts(tenant_id, room_id, recipient_agent_id, status);
CREATE INDEX IF NOT EXISTS idx_room_receipts_sender
    ON cloud_room_receipts(tenant_id, room_id, sender_agent_id, seq);
"""


class ReceiptSchemaUpgradeTests(unittest.TestCase):
    """Repair an already-applied interim cloud_013 without losing data."""

    def test_interim_shape_converges_to_read_status_with_data(self) -> None:
        import sqlite3

        from weft_cloud import migrations

        with tempfile.TemporaryDirectory() as temp:
            db_path = str(Path(temp) / "interim.db")
            conn = sqlite3.connect(db_path)
            conn.row_factory = sqlite3.Row
            try:
                conn.execute(
                    "CREATE TABLE schema_migrations "
                    "(migration_id TEXT PRIMARY KEY, applied_at TEXT NOT NULL)"
                )
                conn.executescript(_INTERIM_CLOUD_013_BODY)
                for migration in migrations.MIGRATIONS:
                    if migration.migration_id == "cloud_014_room_receipts_status_rename":
                        continue
                    conn.execute(
                        "INSERT INTO schema_migrations(migration_id, applied_at) VALUES (?, ?)",
                        (migration.migration_id, "2026-08-13T00:00:00Z"),
                    )
                conn.execute(
                    "INSERT INTO cloud_room_receipts(" 
                    "tenant_id, room_id, seq, recipient_agent_id, sender_agent_id, "
                    "entry_id, status, created_at, updated_at) "
                    "VALUES ('t', 'r', 7, 'recv', 'send', 'oeb_1', 'read', 'x', 'x')"
                )
                conn.commit()
            finally:
                conn.close()

            backend = SqliteWalBackend(db_path)
            try:
                migrations.apply_migrations(backend)
                with backend.transaction() as tx:
                    columns = {
                        row["name"] for row in tx.execute(
                            "SELECT name FROM pragma_table_info('cloud_room_receipts')"
                        ).fetchall()
                    }
                    self.assertIn("read_status", columns)
                    self.assertNotIn("status", columns)
                    row = tx.execute(
                        "SELECT read_status FROM cloud_room_receipts WHERE seq = 7"
                    ).fetchone()
                    self.assertEqual(row["read_status"], "read")
                    applied = tx.execute(
                        "SELECT COUNT(*) AS c FROM schema_migrations "
                        "WHERE migration_id = 'cloud_014_room_receipts_status_rename'"
                    ).fetchone()["c"]
                    self.assertEqual(applied, 1)
            finally:
                backend.close()

    def test_canonical_shape_is_a_no_op(self) -> None:
        from weft_cloud import migrations

        with tempfile.TemporaryDirectory() as temp:
            backend = SqliteWalBackend(str(Path(temp) / "canonical.db"))
            try:
                migrations.apply_migrations(backend)
                migrations.apply_migrations(backend)
                with backend.transaction() as tx:
                    columns = {
                        row["name"] for row in tx.execute(
                            "SELECT name FROM pragma_table_info('cloud_room_receipts')"
                        ).fetchall()
                    }
                    self.assertIn("read_status", columns)
                    self.assertNotIn("status", columns)
            finally:
                backend.close()


if __name__ == "__main__":
    unittest.main()
