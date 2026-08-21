"""Owner-scoped room member removal over the real cloud HTTP service.

The room owner may evict an active member, immediately releasing its seat.
Removal is not a ban: a member that still holds a valid room link may rejoin.
The suite also locks down authorization and no-enumeration behavior.
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

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from weft_cloud.service import WeftCloudService, _CloudHTTPHandler
from weft_cloud.storage import SqliteWalBackend


def _post_raw(base: str, path: str, body: dict, token: str | None = None) -> tuple[int, bytes]:
    data = json.dumps(body).encode("utf-8")
    request = urllib.request.Request(base + path, data=data, method="POST")
    request.add_header("Content-Type", "application/json")
    if token:
        request.add_header("Authorization", f"Bearer {token}")
    try:
        with urllib.request.urlopen(request, timeout=15) as response:
            return response.status, response.read()
    except urllib.error.HTTPError as exc:
        try:
            return exc.code, exc.read()
        finally:
            exc.close()


def _post(base: str, path: str, body: dict, token: str | None = None) -> tuple[int, dict]:
    status, raw = _post_raw(base, path, body, token=token)
    try:
        return status, json.loads(raw.decode("utf-8")) if raw else {}
    except (UnicodeDecodeError, json.JSONDecodeError):
        return status, {}


def _get(base: str, path: str, token: str | None = None, query: str = "") -> tuple[int, dict]:
    request = urllib.request.Request(base + path + query, method="GET")
    if token:
        request.add_header("Authorization", f"Bearer {token}")
    try:
        with urllib.request.urlopen(request, timeout=15) as response:
            raw = response.read()
            return response.status, json.loads(raw.decode("utf-8")) if raw else {}
    except urllib.error.HTTPError as exc:
        try:
            raw = exc.read()
            return exc.code, json.loads(raw.decode("utf-8")) if raw else {}
        except (UnicodeDecodeError, json.JSONDecodeError):
            return exc.code, {}
        finally:
            exc.close()


class OwnerRemoveMemberTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.tmpdir = tempfile.mkdtemp(prefix="weft-remove-member-test-")
        cls.db_path = str(Path(cls.tmpdir) / "test.db")
        from http.server import ThreadingHTTPServer

        cls.httpd = ThreadingHTTPServer(("127.0.0.1", 0), _CloudHTTPHandler)
        cls.base = f"http://127.0.0.1:{cls.httpd.server_address[1]}"
        cls.service = WeftCloudService(SqliteWalBackend(cls.db_path))
        _CloudHTTPHandler.service = cls.service
        cls.server_thread = threading.Thread(target=cls.httpd.serve_forever, daemon=True)
        cls.server_thread.start()

    @classmethod
    def tearDownClass(cls) -> None:
        cls.httpd.shutdown()
        cls.httpd.server_close()
        cls.service.backend.close()
        shutil.rmtree(cls.tmpdir, ignore_errors=True)

    def _signup(self, email: str) -> dict:
        status, response = _post(
            self.base, "/v1/auth/signup",
            {"email": email, "password": "password-123"},
        )
        self.assertEqual(status, HTTPStatus.CREATED, response)
        return response

    def _mint_key(self, session: str, label: str) -> dict:
        status, response = _post(self.base, "/v1/agent-keys", {"label": label}, session)
        self.assertEqual(status, HTTPStatus.CREATED, response)
        return response

    def _create_room(self, session: str, cap: int = 10) -> dict:
        status, response = _post(self.base, "/v1/rooms/create", {"cap": cap}, session)
        self.assertEqual(status, HTTPStatus.CREATED, response)
        return response

    def _join(self, token: str, room: dict) -> None:
        status, response = _post(
            self.base, "/v1/rooms/join",
            {"room_id": room["room_id"], "link_token": room["link_token"], "consent": True},
            token,
        )
        self.assertEqual(status, HTTPStatus.OK, response)

    def _remove(self, token: str, room_id: str, member_id: str) -> tuple[int, dict]:
        return _post(
            self.base, "/v1/rooms/remove_member",
            {"room_id": room_id, "member_id": member_id},
            token,
        )

    def _remove_raw(self, token: str, room_id: str, member_id: str) -> tuple[int, bytes]:
        return _post_raw(
            self.base, "/v1/rooms/remove_member",
            {"room_id": room_id, "member_id": member_id},
            token,
        )

    def _info(self, token: str, room_id: str) -> tuple[int, dict]:
        return _get(self.base, "/v1/rooms/info", token, f"?room_id={room_id}")

    def _owner_with_members(self, prefix: str) -> tuple[dict, dict, dict, dict]:
        owner = self._signup(f"{prefix}-owner@example.com")
        first = self._mint_key(owner["session_token"], "first")
        second = self._mint_key(owner["session_token"], "second")
        room = self._create_room(owner["session_token"])
        self._join(first["agent_key"], room)
        self._join(second["agent_key"], room)
        return owner, first, second, room

    def test_owner_removes_member_immediately_and_releases_seat(self) -> None:
        owner, first, second, room = self._owner_with_members("remove-owner")
        session = owner["session_token"]

        status, response = self._remove(session, room["room_id"], first["key_id"])
        self.assertEqual(status, HTTPStatus.OK, response)
        self.assertEqual(response["status"], "left")

        status, refused = self._info(first["agent_key"], room["room_id"])
        self.assertEqual(status, HTTPStatus.NOT_FOUND, refused)
        self.assertEqual(refused["error"]["code"], "room_not_found")

        replacement = self._mint_key(session, "replacement")
        self._join(replacement["agent_key"], room)
        self._join(first["agent_key"], room)

        status, info = self._info(session, room["room_id"])
        self.assertEqual(status, HTTPStatus.OK, info)
        self.assertEqual(info["member_count"], 4)
        self.assertIn(second["key_id"], {member["agent_id"] for member in info["members"]})

    def test_non_owner_cannot_remove_a_member(self) -> None:
        _owner, first, _second, room = self._owner_with_members("remove-non-owner")
        outsider = self._signup("remove-non-owner-outsider@example.com")
        outsider_key = self._mint_key(outsider["session_token"], "outsider")
        self._join(outsider_key["agent_key"], room)

        status, response = self._remove(outsider_key["agent_key"], room["room_id"], first["key_id"])
        self.assertEqual(status, HTTPStatus.FORBIDDEN, response)
        self.assertEqual(response["error"]["code"], "owner_required")

        status, info = self._info(outsider_key["agent_key"], room["room_id"])
        self.assertEqual(status, HTTPStatus.OK, info)
        self.assertIn(first["key_id"], {member["agent_id"] for member in info["members"]})

    def test_owner_cannot_remove_themselves(self) -> None:
        owner, _first, _second, room = self._owner_with_members("remove-self")
        status, response = self._remove(owner["session_token"], room["room_id"], owner["account_id"])
        self.assertEqual(status, HTTPStatus.FORBIDDEN, response)
        self.assertEqual(response["error"]["code"], "owner_required")

        status, info = self._info(owner["session_token"], room["room_id"])
        self.assertEqual(status, HTTPStatus.OK, info)
        self.assertEqual(info["owner_agent_id"], owner["account_id"])

    def test_unknown_and_cross_room_member_refusals_are_byte_identical(self) -> None:
        owner = self._signup("remove-oracle-owner@example.com")
        room = self._create_room(owner["session_token"])
        other = self._mint_key(owner["session_token"], "other")
        other_room = self._create_room(owner["session_token"])
        self._join(other["agent_key"], other_room)

        ghost = "key_" + "0" * 32
        status_a, body_a = self._remove_raw(owner["session_token"], room["room_id"], ghost)
        status_b, body_b = self._remove_raw(owner["session_token"], room["room_id"], other["key_id"])
        self.assertEqual(status_a, HTTPStatus.NOT_FOUND)
        self.assertEqual(status_a, status_b)
        self.assertEqual(body_a, body_b)
        self.assertIn(b"member_not_found", body_a)


if __name__ == "__main__":
    unittest.main()
