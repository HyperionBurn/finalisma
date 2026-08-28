"""MPAI-22: room owners can recover the existing join link.

The join link is a bearer capability, so the recovery surface is deliberately
owner-only. Recovery must return the same token (not silently rotate it): a
link already shared with an agent must remain valid.
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
from pathlib import Path

import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from weft_cloud.service import WeftCloudService, _CloudHTTPHandler
from weft_cloud.storage import SqliteWalBackend


def _request(base: str, method: str, path: str, *, token: str | None = None,
             body: dict | None = None) -> tuple[int, dict]:
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


class RoomLinkRecoveryTests(unittest.TestCase):
    def setUp(self) -> None:
        from http.server import ThreadingHTTPServer

        self.tmpdir = tempfile.mkdtemp(prefix="weft-room-link-")
        self.db_path = str(Path(self.tmpdir) / "cloud.db")
        self.service = WeftCloudService(
            SqliteWalBackend(self.db_path), origin="http://127.0.0.1:0"
        )
        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), _CloudHTTPHandler)
        self._bind_service()

        self.owner = self._signup("owner@example.com")
        self.member = self._signup("member@example.com", self.owner["tenant_id"])

    def tearDown(self) -> None:
        try:
            self.httpd.shutdown()
        finally:
            self.httpd.server_close()
        self.service.backend.close()
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def _bind_service(self) -> None:
        _CloudHTTPHandler.service = self.service
        self.base = f"http://127.0.0.1:{self.httpd.server_address[1]}"
        self.service.origin = self.base
        self.thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)
        self.thread.start()

    def _signup(self, email: str, tenant_id: str | None = None) -> dict:
        body = {"email": email, "password": "CorrectHorse!1"}
        if tenant_id is not None:
            body["tenant_id"] = tenant_id
        status, response = _request(self.base, "POST", "/v1/auth/signup", body=body)
        self.assertEqual(status, HTTPStatus.CREATED, response)
        return response

    def _create_room(self) -> dict:
        status, response = _request(
            self.base, "POST", "/v1/rooms/create",
            token=self.owner["session_token"], body={"cap": 4, "name": "Recoverable"},
        )
        self.assertEqual(status, HTTPStatus.CREATED, response)
        return response

    def _join(self, account: dict, room: dict) -> dict:
        status, response = _request(
            self.base, "POST", "/v1/rooms/join", token=account["session_token"],
            body={"room_id": room["room_id"], "link_token": room["link_token"], "consent": True},
        )
        self.assertEqual(status, HTTPStatus.OK, response)
        return response

    def test_owner_recovers_same_link_and_non_owner_gets_403(self) -> None:
        room = self._create_room()
        self._join(self.member, room)

        with self.service.backend.transaction() as tx:
            stored = tx.execute(
                "SELECT token_hash, token_ciphertext FROM cloud_room_links WHERE room_id = ?",
                (room["room_id"],),
            ).fetchone()
        self.assertIsNotNone(stored)
        self.assertNotIn(room["link_token"], json.dumps(dict(stored)))
        self.assertTrue(stored["token_ciphertext"].startswith("v1."))

        owner_status, recovered = _request(
            self.base, "GET", f"/v1/rooms/link?room_id={room['room_id']}",
            token=self.owner["session_token"],
        )
        self.assertEqual(owner_status, HTTPStatus.OK, recovered)
        self.assertEqual(recovered["room_id"], room["room_id"])
        self.assertEqual(recovered["link_id"], room["link_id"])
        self.assertEqual(recovered["link_token"], room["link_token"])
        self.assertEqual(
            recovered["shareable_link"],
            f"{self.base}/j/{room['link_token']}",
        )

        member_status, member_error = _request(
            self.base, "GET", f"/v1/rooms/link?room_id={room['room_id']}",
            token=self.member["session_token"],
        )
        self.assertEqual(member_status, HTTPStatus.FORBIDDEN, member_error)
        self.assertEqual(member_error["error"]["code"], "owner_required")
        self.assertNotIn(room["link_token"], json.dumps(member_error))

        # Recovery did not rotate or invalidate the existing bearer link.
        newcomer = self._signup("newcomer@example.com")
        self._join(newcomer, room)

    def test_existing_link_survives_service_restart(self) -> None:
        room = self._create_room()
        old_token = room["link_token"]
        old_link_id = room["link_id"]
        # The key sidecar is binary even on Windows, where text-mode writes
        # would expand a random LF byte and break the next restart.
        self.assertEqual(Path(self.db_path + ".room-link-key").stat().st_size, 32)

        self.httpd.shutdown()
        self.httpd.server_close()
        self.thread.join(timeout=5)
        self.service.backend.close()

        self.service = WeftCloudService(
            SqliteWalBackend(self.db_path), origin="http://127.0.0.1:0"
        )
        from http.server import ThreadingHTTPServer

        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), _CloudHTTPHandler)
        self._bind_service()

        status, recovered = _request(
            self.base, "GET", f"/v1/rooms/link?room_id={room['room_id']}",
            token=self.owner["session_token"],
        )
        self.assertEqual(status, HTTPStatus.OK, recovered)
        self.assertEqual(recovered["link_id"], old_link_id)
        self.assertEqual(recovered["link_token"], old_token)

    def test_owner_panel_and_creation_copy_are_truthful(self) -> None:
        room_view = (ROOT / "web/src/components/app/RoomView.tsx").read_text(encoding="utf-8")
        new_room = (ROOT / "web/src/components/app/NewRoom.tsx").read_text(encoding="utf-8")
        api = (ROOT / "web/src/lib/api.ts").read_text(encoding="utf-8")

        self.assertIn("roomLink", api)
        self.assertIn("roomLink", room_view)
        self.assertIn("Only the room owner can view or copy", room_view)
        self.assertIn("Your room link", new_room)
        self.assertNotIn("not shown again", new_room.lower())
        self.assertNotIn("cannot be looked up later", new_room.lower())


if __name__ == "__main__":
    unittest.main()
