"""Regression coverage for create-room quota atomicity."""

from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from weft_cloud.rooms import CloudRoomService, RoomError
from weft_cloud.storage import SqliteWalBackend


class FailingLateExecuteTransaction:
    """Transaction proxy that fails at a chosen late insert statement."""

    def __init__(self, wrapped, fail_on_sql: str):
        self._wrapped = wrapped
        self._fail_on_sql = fail_on_sql

    def execute(self, sql: str, params: tuple = ()):
        if self._fail_on_sql in " ".join(sql.split()):
            raise RuntimeError("injected late create-room failure")
        return self._wrapped.execute(sql, params)

    def commit(self) -> None:
        self._wrapped.commit()

    def rollback(self) -> None:
        self._wrapped.rollback()

    def executescript(self, sql: str) -> None:
        self._wrapped.executescript(sql)


class LateFailingBackend(SqliteWalBackend):
    def __init__(self, db_path: str):
        super().__init__(db_path)
        self.fail_on_sql: str | None = None

    def transaction(self):
        parent_cm = super().transaction()
        backend = self

        class _Context:
            def __enter__(self):
                tx = parent_cm.__enter__()
                if backend.fail_on_sql:
                    return FailingLateExecuteTransaction(tx, backend.fail_on_sql)
                return tx

            def __exit__(self, exc_type, exc, tb):
                return parent_cm.__exit__(exc_type, exc, tb)

        return _Context()


class CreateRoomQuotaAtomicityTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmpdir = tempfile.mkdtemp(prefix="weft-create-quota-")
        self.db_path = str(Path(self.tmpdir) / "cloud.db")
        self.backend = LateFailingBackend(self.db_path)
        self.backend.initialize()
        self.backend.create_tenant("tenant-create", "Tenant Create", plan_id="free")
        self.rooms = CloudRoomService(self.backend)
        self.owner = "acct_owner"
        self.token = "fss_" + ("x" * 32)

    def tearDown(self) -> None:
        try:
            self.backend.close()
        finally:
            import shutil
            shutil.rmtree(self.tmpdir, ignore_errors=True)

    def _scalar(self, sql: str, params: tuple = ()) -> int:
        with self.backend.transaction() as tx:
            row = tx.execute(sql, params).fetchone()
        return int(row["c"])

    def _room_counter(self) -> int:
        with self.backend.transaction() as tx:
            row = tx.execute(
                "SELECT value FROM cloud_counters "
                "WHERE tenant_id = ? AND counter = 'rooms'",
                ("tenant-create",),
            ).fetchone()
        return int(row["value"]) if row else 0

    def _member_counters_total(self) -> int:
        with self.backend.transaction() as tx:
            row = tx.execute(
                "SELECT COALESCE(SUM(value), 0) AS c FROM cloud_room_counters "
                "WHERE tenant_id = ? AND counter = 'members'",
                ("tenant-create",),
            ).fetchone()
        return int(row["c"])

    def assert_no_create_side_effects(self) -> None:
        self.assertEqual(self._room_counter(), 0)
        self.assertEqual(self._member_counters_total(), 0)
        self.assertEqual(self._scalar("SELECT COUNT(*) AS c FROM cloud_tenant_rooms"), 0)
        self.assertEqual(self._scalar("SELECT COUNT(*) AS c FROM cloud_rooms"), 0)
        self.assertEqual(self._scalar("SELECT COUNT(*) AS c FROM cloud_room_links"), 0)
        self.assertEqual(self._scalar("SELECT COUNT(*) AS c FROM cloud_room_members"), 0)
        self.assertEqual(self._scalar("SELECT COUNT(*) AS c FROM cloud_room_cursors"), 0)
        self.assertEqual(self._scalar("SELECT COUNT(*) AS c FROM cloud_room_event_log"), 0)

    def test_malformed_name_is_rejected_before_quota_mutation(self) -> None:
        with self.assertRaises(RoomError) as ctx:
            self.rooms.create_room(
                "tenant-create", self.owner, self.token, cap=4, name={"bad": "name"},
            )

        self.assertEqual(ctx.exception.code, "invalid_argument")
        self.assert_no_create_side_effects()

    def test_late_failure_rolls_back_room_and_owner_counter_invariants(self) -> None:
        self.backend.fail_on_sql = "INSERT INTO cloud_room_cursors"

        with self.assertRaises(RuntimeError) as ctx:
            self.rooms.create_room(
                "tenant-create", self.owner, self.token, cap=4, name="late-fail",
            )

        self.assertIn("injected late create-room failure", str(ctx.exception))
        self.assert_no_create_side_effects()

        self.backend.fail_on_sql = None
        created = self.rooms.create_room(
            "tenant-create", self.owner, self.token, cap=4, name="after-fail",
        )

        self.assertEqual(self._room_counter(), 1)
        self.assertEqual(
            self._room_counter(),
            self._scalar(
                "SELECT COUNT(*) AS c FROM cloud_rooms "
                "WHERE tenant_id = ? AND state != 'closed'",
                ("tenant-create",),
            ),
        )
        self.assertEqual(self._member_counters_total(), 1)
        self.assertEqual(
            self._member_counters_total(),
            self._scalar(
                "SELECT COUNT(*) AS c FROM cloud_room_members "
                "WHERE tenant_id = ? AND room_id = ? AND status = 'active'",
                ("tenant-create", created["room_id"]),
            ),
        )


if __name__ == "__main__":
    unittest.main()
