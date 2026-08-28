"""MPAI-108(c): a joiner may supply a ROOM-SCOPED, self-declared display name.

The name is stored as a membership attribute (cloud_room_members.display_name),
never on the account, and is resolved for room_info WITHOUT any cross-tenant
identity lookup. It is rendered to other users as *claimed* identity, so it must
never be visually indistinguishable from a name the service actually verified.

Red-first: every test here must fail against the tree before the (c) work lands.
The load-bearing one is test_no_cross_tenant_identity_lookup_for_self_declared —
it is the guard that stops a future change from "resolving" cross-tenant names by
quietly dropping the tenant filter, which is the option that was explicitly
rejected.

DRAFT — held in agent workspace during the MP-web hard commit freeze. Move to
tests/test_room_self_declared_name.py and commit ALONE (tests-first) once god
posts FREEZE LIFTED.
"""

from __future__ import annotations

import json
import shutil
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from http import HTTPStatus
from http.server import ThreadingHTTPServer
from pathlib import Path

import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from weft_cloud.service import WeftCloudService, _CloudHTTPHandler
from weft_cloud.storage import SqliteWalBackend


def _request(base, method, path, *, token=None, body=None):
    payload = None if body is None else json.dumps(body).encode("utf-8")
    request = urllib.request.Request(base + path, data=payload, method=method)
    request.add_header("Accept", "application/json")
    if payload is not None:
        request.add_header("Content-Type", "application/json")
    if token:
        request.add_header("Authorization", f"Bearer {token}")
    try:
        with urllib.request.urlopen(request, timeout=10) as response:
            return response.status, json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        try:
            return exc.code, json.loads(exc.read().decode("utf-8"))
        finally:
            exc.close()


class SelfDeclaredNameTests(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.mkdtemp(prefix="weft-selfname-")
        self.db_path = str(Path(self.tmpdir) / "cloud.db")
        self.service = WeftCloudService(
            SqliteWalBackend(self.db_path), origin="http://127.0.0.1:0"
        )
        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), _CloudHTTPHandler)
        _CloudHTTPHandler.service = self.service
        self.base = f"http://127.0.0.1:{self.httpd.server_address[1]}"
        self.service.origin = self.base
        self.thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)
        self.thread.start()

        self.owner = self._signup("owner@acme.test")               # tenant A
        self.stranger = self._signup("stranger@other-corp.test")   # tenant B (cross-tenant)
        self.room = self._create_room()

    def tearDown(self):
        try:
            self.httpd.shutdown()
        finally:
            self.httpd.server_close()
        self.service.backend.close()
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def _signup(self, email, tenant_id=None):
        body = {"email": email, "password": "CorrectHorse!1"}
        if tenant_id is not None:
            body["tenant_id"] = tenant_id
        status, response = _request(self.base, "POST", "/v1/auth/signup", body=body)
        self.assertEqual(status, HTTPStatus.CREATED, response)
        return response

    def _create_room(self, name="Cross-tenant room"):
        status, response = _request(
            self.base, "POST", "/v1/rooms/create",
            token=self.owner["session_token"], body={"cap": 6, "name": name},
        )
        self.assertEqual(status, HTTPStatus.CREATED, response)
        return response

    def _join(self, account, room=None, display_name=None, expect=HTTPStatus.OK):
        room = room or self.room
        body = {"room_id": room["room_id"], "link_token": room["link_token"], "consent": True}
        if display_name is not None:
            body["display_name"] = display_name
        status, response = _request(
            self.base, "POST", "/v1/rooms/join",
            token=account["session_token"], body=body,
        )
        self.assertEqual(status, expect, response)
        return response

    def _members(self, viewer=None, room=None):
        viewer = viewer or self.owner
        room = room or self.room
        status, response = _request(
            self.base, "GET", f"/v1/rooms/info?room_id={room['room_id']}",
            token=viewer["session_token"],
        )
        self.assertEqual(status, HTTPStatus.OK, response)
        return {m["agent_id"]: m for m in response["members"]}

    # ---- T1 -------------------------------------------------------------
    def test_cross_tenant_self_declared_name_renders_to_owner(self):
        self._join(self.stranger, display_name="Ada from Acme")
        members = self._members()
        mine = members[self.stranger["account_id"]]
        self.assertEqual(mine["display_name"], "Ada from Acme")
        self.assertEqual(mine["display_name_source"], "self_declared")

    # ---- T2  GUARD (load-bearing) -------------------------------------
    def test_no_cross_tenant_identity_lookup_for_self_declared(self):
        """With a self-declared name present, room_info must resolve it from the
        membership row alone — no SELECT against cloud_identity_agent_keys /
        cloud_identity_accounts for that member. This is the guard against a
        future 'fix' that drops the tenant filter to resolve cross-tenant names.
        """
        from weft_cloud import storage

        self._join(self.stranger, display_name="Ada from Acme")
        stranger_id = self.stranger["account_id"]

        seen = []
        original = storage._SqliteTransaction.execute

        def _spy(self, sql, params=()):
            seen.append((sql, tuple(params) if params else ()))
            return original(self, sql, params)

        storage._SqliteTransaction.execute = _spy
        try:
            members = self._members()
        finally:
            storage._SqliteTransaction.execute = original

        # No identity-table read may be keyed by THIS member's id while their
        # self-declared name is present. (Same-tenant members with no declared
        # name are still resolved that way — that is fine and not asserted here.)
        offenders = [
            sql for (sql, params) in seen
            if ("cloud_identity_agent_keys" in sql or "cloud_identity_accounts" in sql)
            and stranger_id in params
        ]
        self.assertEqual(
            offenders, [],
            f"room_info ran a cross-tenant identity lookup for {stranger_id} "
            f"despite a self-declared name: {offenders}",
        )
        self.assertEqual(members[stranger_id]["display_name"], "Ada from Acme")
        self.assertEqual(members[stranger_id]["display_name_source"], "self_declared")

    # ---- T3  room-scoped, not account-scoped -------------------------
    def test_name_is_room_scoped_not_account_scoped(self):
        room2 = self._create_room(name="Second room")
        self._join(self.stranger, room=self.room, display_name="Ada in room 1")
        self._join(self.stranger, room=room2, display_name="Ada in room 2")

        self.assertEqual(
            self._members(room=self.room)[self.stranger["account_id"]]["display_name"],
            "Ada in room 1",
        )
        self.assertEqual(
            self._members(room=room2)[self.stranger["account_id"]]["display_name"],
            "Ada in room 2",
        )
        with self.service.backend.transaction() as tx:
            acct = tx.execute(
                "SELECT email FROM cloud_identity_accounts WHERE account_id = ?",
                (self.stranger["account_id"],),
            ).fetchone()
        self.assertEqual(acct["email"], "stranger@other-corp.test")

    # ---- T4  validation --------------------------------------------
    def test_validation_and_rejoin_semantics(self):
        self._join(self.stranger, display_name="x" * 81, expect=HTTPStatus.BAD_REQUEST)

        self._join(self.stranger, display_name="   ")
        m = self._members()[self.stranger["account_id"]]
        self.assertNotIn("display_name", m)  # whitespace-only == absent

        self._join(self.stranger, display_name="Ada")
        self.assertEqual(self._members()[self.stranger["account_id"]]["display_name"], "Ada")

        self._join(self.stranger)  # bare re-join keeps the stored name
        self.assertEqual(self._members()[self.stranger["account_id"]]["display_name"], "Ada")

        self._join(self.stranger, display_name="Ada v2")  # explicit update
        self.assertEqual(self._members()[self.stranger["account_id"]]["display_name"], "Ada v2")

    # ---- T5  impersonation ---------------------------------------
    def test_impersonation_name_is_flagged_not_identical(self):
        # The owner always resolves within their own tenant, so use them as the
        # member being impersonated. The cross-tenant stranger claims that exact
        # name.
        owner_name = "owner@acme.test"
        self._join(self.stranger, display_name=owner_name)

        members = self._members()
        owner_m = members[self.owner["account_id"]]
        faker_m = members[self.stranger["account_id"]]

        # Same visible string...
        self.assertEqual(owner_m["display_name"], owner_name)
        self.assertEqual(faker_m["display_name"], owner_name)
        # ...but the source field is what stops them rendering identically: the
        # client marks a self_declared name as unverified.
        self.assertEqual(owner_m["display_name_source"], "resolved")
        self.assertEqual(faker_m["display_name_source"], "self_declared")
        self.assertNotEqual(
            faker_m["display_name_source"], owner_m["display_name_source"],
            "a claimed name is indistinguishable from a verified one",
        )
        # NOTE: the "must not render identical text" assertion also lives in the
        # RoomView component test (quotes + collision disambiguation). This pins
        # the server contract that test relies on.

    # ---- T6  injection ----------------------------------------
    def test_markup_in_name_is_stored_verbatim_and_flagged(self):
        payload = '<img src=x onerror=alert(1)>'
        self._join(self.stranger, display_name=payload)
        m = self._members()[self.stranger["account_id"]]
        self.assertEqual(m["display_name"], payload)       # server does not mangle
        self.assertEqual(m["display_name_source"], "self_declared")


if __name__ == "__main__":
    unittest.main()
