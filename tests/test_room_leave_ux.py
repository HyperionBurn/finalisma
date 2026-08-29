"""MPAI-125: a non-owner must have a safe, discoverable way to leave a room.

The leave capability already exists end to end (POST /v1/rooms/leave, MCP
room_leave, CloudRoomService.leave_room); the defect found cold on production
was that the web UI never surfaced it for a guest. These tests pin the
*contract the UI depends on*, so the confirm-dialog copy and the backend
cannot drift apart:

* a non-owner leave frees the seat and emits ``room.left``;
* that same member can rejoin on the SAME link and lands active with the
  MPAI-110 removal marker still NULL - i.e. the dialog's "you can rejoin with
  the same link" is a promise the code actually keeps (subject only to the
  link still being valid, which is why the copy says exactly that);
* the room OWNER cannot leave - that would orphan the room (owner_agent_id
  pointing at a non-member, nobody able to close or manage it). The owner's
  exit is Close room. Rejection carries a distinct code, ``owner_cannot_leave``.

Red-first: owner-leave currently succeeds, and there is no ``owner_cannot_leave``
code, so test_room_owner_cannot_leave fails until the guard lands.

Boundaries (per the card): this does NOT touch close-room semantics or the
MPAI-110 removal-marker / restore behaviour.
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
from http.server import ThreadingHTTPServer
from pathlib import Path

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


class RoomLeaveUxTests(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.mkdtemp(prefix="weft-leaveux-")
        self.service = WeftCloudService(
            SqliteWalBackend(str(Path(self.tmpdir) / "cloud.db")),
            origin="http://127.0.0.1:0",
        )
        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), _CloudHTTPHandler)
        _CloudHTTPHandler.service = self.service
        self.base = f"http://127.0.0.1:{self.httpd.server_address[1]}"
        self.service.origin = self.base
        self.thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)
        self.thread.start()

        self.owner = self._signup("owner@acme.test")
        self.room = self._create_room(cap=2)  # owner takes one seat -> one guest fits

    def tearDown(self):
        try:
            self.httpd.shutdown()
        finally:
            self.httpd.server_close()
        self.service.backend.close()
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    # -- helpers --------------------------------------------------------
    def _signup(self, email):
        status, response = _request(
            self.base, "POST", "/v1/auth/signup",
            body={"email": email, "password": "CorrectHorse!1"},
        )
        self.assertEqual(status, HTTPStatus.CREATED, response)
        return response

    def _create_room(self, cap=2, name="Room"):
        status, response = _request(
            self.base, "POST", "/v1/rooms/create",
            token=self.owner["session_token"], body={"cap": cap, "name": name},
        )
        self.assertEqual(status, HTTPStatus.CREATED, response)
        return response

    def _join(self, account, expect=HTTPStatus.OK):
        status, response = _request(
            self.base, "POST", "/v1/rooms/join",
            token=account["session_token"],
            body={
                "room_id": self.room["room_id"],
                "link_token": self.room["link_token"],
                "consent": True,
            },
        )
        self.assertEqual(status, expect, response)
        return response

    def _leave(self, account, expect=HTTPStatus.OK):
        status, response = _request(
            self.base, "POST", "/v1/rooms/leave",
            token=account["session_token"],
            body={"room_id": self.room["room_id"]},
        )
        self.assertEqual(status, expect, response)
        return response

    def _poll_as_owner(self):
        status, response = _request(
            self.base, "POST", "/v1/rooms/poll",
            token=self.owner["session_token"],
            body={"room_id": self.room["room_id"], "after_seq": 0, "limit": 200},
        )
        self.assertEqual(status, HTTPStatus.OK, response)
        return response["events"]

    def _member_row(self, account):
        with self.service.backend.transaction() as tx:
            return tx.execute(
                "SELECT status, removed_at FROM cloud_room_members "
                "WHERE room_id = ? AND agent_id = ?",
                (self.room["room_id"], account["account_id"]),
            ).fetchone()

    # -- tests --------------------------------------------------------
    def test_non_owner_leave_frees_seat_and_emits_room_left(self):
        guest_a = self._signup("guest-a@corp.test")
        self._join(guest_a)                       # room now full (owner + guest_a, cap 2)

        guest_b = self._signup("guest-b@corp.test")
        self._join(guest_b, expect=HTTPStatus.CONFLICT)  # room_full

        left = self._leave(guest_a)
        self.assertEqual(left["status"], "left")

        # Seat is genuinely freed: the previously-refused guest now fits.
        self._join(guest_b, expect=HTTPStatus.OK)

        events = self._poll_as_owner()
        left_events = [
            e for e in events
            if e["kind"] == "room.left"
            and e.get("payload", {}).get("agent_id") == guest_a["account_id"]
        ]
        self.assertEqual(len(left_events), 1, events)

    def test_voluntary_leaver_rejoins_on_same_link_with_no_removal_marker(self):
        guest = self._signup("guest@corp.test")
        self._join(guest)

        self._leave(guest)
        row = self._member_row(guest)
        self.assertEqual(row["status"], "left")
        self.assertIsNone(row["removed_at"], "voluntary leave must not set the ejection marker")

        # The exact promise the confirm dialog makes: rejoin on the SAME link.
        rejoined = self._join(guest, expect=HTTPStatus.OK)
        self.assertEqual(rejoined["status"], "active")

        row = self._member_row(guest)
        self.assertEqual(row["status"], "active")
        self.assertIsNone(row["removed_at"])

    def test_room_owner_cannot_leave(self):
        # Leaving as owner would orphan the room. The owner's exit is Close room.
        _, body = self._leave(self.owner, expect=HTTPStatus.FORBIDDEN)
        self.assertEqual(body["error"]["code"], "owner_cannot_leave")
        self.assertIn("close", body["error"]["message"].lower())

        # The owner is untouched by the refused call.
        self.assertEqual(self._member_row(self.owner)["status"], "active")


if __name__ == "__main__":
    unittest.main()
