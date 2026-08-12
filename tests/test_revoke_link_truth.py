"""Revoke-link truth contract — RED deliverable for the 2026-08-12 finding.

A room link is a BEARER CREDENTIAL — anyone holding it can join, cross-tenant.
``revoke_link`` is the ONLY control for cutting off a leaked link. Two failures
compound on the live surface:

  1. SILENT FALSE SUCCESS. revoke_link reported 200 while revoking nothing: an
     unknown / malformed / wrong-room link_id returned ``{link_id, revoked:
     True}`` because the UPDATE touched zero rows yet was reported as success.
     A control that lies about having acted manufactures false confidence at
     exactly the wrong moment.
  2. UNREACHABLE IDENTIFIER. link_id is returned exactly once (room_create) and
     is absent from room_info, so an owner who lost the create response can
     NEVER revoke — their only fallback is closing the whole room.

This suite asserts the repaired contract on BOTH room surfaces (the coordinator
MCP dispatcher and the hosted cloud HTTP service), because the identical defect
existed in both and the live transcript was measured against the hosted plane:

  1. revoke_link with an unknown link_id does NOT return 200 and does NOT revoke.
  2. revoke_link with a link_id from ANOTHER room/tenant is refused, BYTE-
     IDENTICALLY to an unknown link_id (no link-id existence oracle — this
     codebase has had three enumeration oracles; this must not be a fourth).
  3. revoke_link with the correct link_id revokes, and a later join is refused
     with link_revoked (HTTP 410 on the cloud plane).
  4. a non-owner member cannot revoke.
  5. the owner can DISCOVER link_id after creation (the exact gap that made
     revocation unusable) and confirm the action landed.
  6. a non-owner member cannot see link_id (least exposure).
  7. link_token is NEVER present in room_info or any error body.

Every test must FAIL against the pre-fix code; the assertions compare
refusals to EACH OTHER first so a future re-split into two NEW distinct codes
still fails this suite even if both differ from today's literal.
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

from weft_mcp.core import WeftError, WeftStore
from weft_mcp.server import WeftDispatcher


class _Refusal(Exception):
    """A normalized refusal from either surface (code + message + optional status)."""

    def __init__(self, code: str, message: str, status: int | None = None):
        super().__init__(message)
        self.code = code
        self.message = message
        self.status = status


class RevokeLinkTruthMixin:
    """The seven required tests, expressed against a surface adapter.

    Concrete subclasses implement the adapter primitives and bind the owner /
    member / joiner identities to their surface. The assertions are written so
    the refusals are compared to each other before any literal is trusted.
    """

    # -- adapter primitives -------------------------------------------------
    def _create_room(self, caller=None, cap: int = 6) -> dict:
        raise NotImplementedError

    def _info(self, caller, room_id: str) -> dict:
        raise NotImplementedError

    def _revoke(self, caller, room_id: str, link_id: str) -> dict:
        raise NotImplementedError

    def _join(self, caller, room_id: str, link_token: str) -> dict:
        raise NotImplementedError

    def _owner(self):
        raise NotImplementedError

    def _member(self):
        raise NotImplementedError

    def _joiner(self):
        raise NotImplementedError

    # -- shared helpers -----------------------------------------------------
    def _refusal_payload(self, fn) -> str:
        """Run ``fn``; return the SERIALIZED refusal (code+message) it raises.

        Used for byte-identical assertions: two paths must produce the exact
        same wire bytes, not merely the same code.
        """
        try:
            fn()
        except _Refusal as exc:
            return json.dumps(
                {"code": exc.code, "message": exc.message}, sort_keys=True
            )
        raise AssertionError("expected the call to be refused")

    def _assert_link_revoked_refusal(self, fn) -> None:
        with self.assertRaises(_Refusal) as ctx:
            fn()
        self.assertEqual(ctx.exception.code, "link_revoked")
        if ctx.exception.status is not None:
            self.assertEqual(ctx.exception.status, HTTPStatus.GONE)

    # -- 1. unknown link_id is refused and revokes nothing ------------------
    def test_unknown_link_id_is_refused_and_revokes_nothing(self) -> None:
        created = self._create_room()
        unknown = "link_" + "0" * 32  # well-formed but never created
        with self.assertRaises(_Refusal) as ctx:
            self._revoke(self._owner(), created["room_id"], unknown)
        self.assertEqual(ctx.exception.code, "link_not_found")
        # Nothing was revoked: the real link still admits a new identity.
        joined = self._join(self._joiner(), created["room_id"], created["link_token"])
        self.assertEqual(joined["status"], "active")

    # -- 2. wrong-room link_id refused byte-identically to unknown ----------
    def test_wrong_room_link_refused_byte_identical_to_unknown(self) -> None:
        room_a = self._create_room()
        # A link that REALLY exists, but in someone else's room/tenant.
        room_b = self._create_room(caller=self._member())
        unknown = "link_" + "0" * 32

        def revoke_wrong_room() -> None:
            self._revoke(self._owner(), room_a["room_id"], room_b["link_id"])

        def revoke_unknown() -> None:
            self._revoke(self._owner(), room_a["room_id"], unknown)

        wrong = self._refusal_payload(revoke_wrong_room)
        never = self._refusal_payload(revoke_unknown)
        # BYTE-IDENTICAL: an oracle that confirms which link_ids exist would
        # require these two wire bodies to differ. They must not.
        self.assertEqual(wrong, never)
        self.assertEqual(json.loads(wrong)["code"], "link_not_found")

    # -- 3. correct link_id revokes; later join -> link_revoked (410) -------
    def test_correct_link_revokes_and_later_join_is_refused(self) -> None:
        created = self._create_room()
        result = self._revoke(self._owner(), created["room_id"], created["link_id"])
        self.assertTrue(result["revoked"])
        self._assert_link_revoked_refusal(
            lambda: self._join(self._joiner(), created["room_id"], created["link_token"])
        )

    # -- 4. a non-owner member cannot revoke --------------------------------
    def test_non_owner_member_cannot_revoke(self) -> None:
        created = self._create_room()
        self._join(self._member(), created["room_id"], created["link_token"])
        with self.assertRaises(_Refusal) as ctx:
            self._revoke(self._member(), created["room_id"], created["link_id"])
        self.assertEqual(ctx.exception.code, "owner_required")
        # The refusal is not a silent no-op with a stolen success: the link is
        # still live for a brand-new identity.
        joined = self._join(self._joiner(), created["room_id"], created["link_token"])
        self.assertEqual(joined["status"], "active")

    # -- 5. the owner can DISCOVER link_id after creation -------------------
    def test_owner_can_discover_link_id_after_creation(self) -> None:
        created = self._create_room()
        # Simulate an owner who did NOT save the create response: the only
        # surviving handle is room_id. The link_id must be recoverable.
        info = self._info(self._owner(), created["room_id"])
        self.assertIn("link_id", info)
        self.assertEqual(info["link_id"], created["link_id"])
        self.assertIn("link_revoked", info)
        self.assertIs(info["link_revoked"], False)
        # The discovered identifier is actually USABLE: revoking with it works.
        result = self._revoke(self._owner(), created["room_id"], info["link_id"])
        self.assertTrue(result["revoked"])
        self._assert_link_revoked_refusal(
            lambda: self._join(self._joiner(), created["room_id"], created["link_token"])
        )

    # -- 6. a non-owner member cannot see link_id ---------------------------
    def test_non_owner_member_cannot_see_link_id(self) -> None:
        created = self._create_room()
        self._join(self._member(), created["room_id"], created["link_token"])
        member_info = self._info(self._member(), created["room_id"])
        self.assertNotIn("link_id", member_info)
        self.assertNotIn("link_revoked", member_info)
        owner_info = self._info(self._owner(), created["room_id"])
        self.assertIn("link_id", owner_info)

    # -- 7. link_token is never in room_info or any error body --------------
    def test_link_token_never_in_room_info_or_error_body(self) -> None:
        created = self._create_room()
        token = created["link_token"]
        self.assertTrue(token.startswith("rm_"))

        owner_info = self._info(self._owner(), created["room_id"])
        self.assertNotIn("link_token", owner_info)
        self.assertNotIn(token, json.dumps(owner_info, sort_keys=True))

        # An unknown-id refusal body must not echo the credential.
        with self.assertRaises(_Refusal) as ctx:
            self._revoke(self._owner(), created["room_id"], "link_" + "0" * 32)
        self.assertNotIn(
            token,
            json.dumps({"code": ctx.exception.code, "message": ctx.exception.message}),
        )

        # A revoked-link join refusal body must not echo the credential either.
        self._revoke(self._owner(), created["room_id"], created["link_id"])
        with self.assertRaises(_Refusal) as ctx:
            self._join(self._joiner(), created["room_id"], created["link_token"])
        self.assertNotIn(
            token,
            json.dumps({"code": ctx.exception.code, "message": ctx.exception.message}),
        )


class CoordinatorRevokeLinkTruthTests(RevokeLinkTruthMixin, unittest.TestCase):
    """The coordinator MCP surface (WeftDispatcher over real WeftStore)."""

    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        root = Path(self.temp.name)
        self.store = WeftStore(root / "state.db", root, require_actor_auth=True)
        self.dispatcher = WeftDispatcher(self.store)
        self.tokens: dict[str, str] = {}
        for agent_id in ("owner-1", "member-2", "joiner-3"):
            result = self.dispatcher.call_tool(
                "register_agent", {"team_id": "team-1", "agent_id": agent_id}
            )
            self.tokens[agent_id] = result["actor_token"]

    def tearDown(self) -> None:
        self.store.close()
        self.temp.cleanup()

    def _owner(self) -> str:
        return "owner-1"

    def _member(self) -> str:
        return "member-2"

    def _joiner(self) -> str:
        return "joiner-3"

    def _call(self, tool: str, args: dict):
        try:
            return self.dispatcher.call_tool(tool, args)
        except WeftError as exc:
            raise _Refusal(exc.code, exc.message) from exc

    def _create_room(self, caller=None, cap: int = 6) -> dict:
        caller = caller or self._owner()
        return self._call(
            "room_create",
            {"team_id": "team-1", "owner_agent_id": caller, "cap": cap},
        )

    def _info(self, caller, room_id: str) -> dict:
        return self._call(
            "room_info",
            {"team_id": "team-1", "room_id": room_id, "agent_id": caller,
             "actor_token": self.tokens[caller]},
        )

    def _revoke(self, caller, room_id: str, link_id: str) -> dict:
        return self._call(
            "room_revoke_link",
            {"team_id": "team-1", "room_id": room_id, "owner_agent_id": caller,
             "link_id": link_id, "actor_token": self.tokens[caller]},
        )

    def _join(self, caller, room_id: str, link_token: str) -> dict:
        return self._call(
            "room_join",
            {"team_id": "team-1", "room_id": room_id, "link_token": link_token,
             "agent_id": caller, "consent": True,
             "actor_token": self.tokens[caller]},
        )


def _post(base: str, path: str, body: dict, token: str | None = None) -> tuple[int, dict]:
    data = json.dumps(body).encode("utf-8")
    req = urllib.request.Request(base + path, data=data, method="POST")
    req.add_header("Content-Type", "application/json")
    if token:
        req.add_header("Authorization", f"Bearer {token}")
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            return resp.status, json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        payload: dict = {}
        try:
            payload = json.loads(exc.read().decode("utf-8"))
        except Exception:
            pass
        finally:
            exc.close()
        return exc.code, payload


def _get(base: str, path: str, token: str | None = None) -> tuple[int, dict]:
    req = urllib.request.Request(base + path, method="GET")
    if token:
        req.add_header("Authorization", f"Bearer {token}")
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            return resp.status, json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        payload: dict = {}
        try:
            payload = json.loads(exc.read().decode("utf-8"))
        except Exception:
            pass
        finally:
            exc.close()
        return exc.code, payload


class CloudRevokeLinkTruthTests(RevokeLinkTruthMixin, unittest.TestCase):
    """The hosted cloud HTTP surface (real WeftCloudService over real HTTP)."""

    def setUp(self) -> None:
        from http.server import ThreadingHTTPServer

        from weft_cloud.service import WeftCloudService, _CloudHTTPHandler
        from weft_cloud.storage import SqliteWalBackend

        self.tmpdir = tempfile.mkdtemp(prefix="weft-revoke-")
        self.db_path = str(Path(self.tmpdir) / "test.db")
        self._httpd = ThreadingHTTPServer(("127.0.0.1", 0), _CloudHTTPHandler)
        self.port = self._httpd.server_address[1]
        self.base = f"http://127.0.0.1:{self.port}"
        self.service = WeftCloudService(SqliteWalBackend(self.db_path), origin=self.base)
        _CloudHTTPHandler.service = self.service
        self.server = threading.Thread(target=self._httpd.serve_forever, daemon=True)
        self.server.start()
        self.owner_token = self._signup("owner@example.com")["session_token"]
        self.member_token = self._signup("member@example.com")["session_token"]
        self.joiner_token = self._signup("joiner@example.com")["session_token"]

    def tearDown(self) -> None:
        if hasattr(self, "_httpd"):
            try:
                self._httpd.shutdown()
            finally:
                self._httpd.server_close()
        try:
            self.service.backend.close()
        except Exception:
            pass
        try:
            shutil.rmtree(self.tmpdir, ignore_errors=True)
        except Exception:
            pass

    def _signup(self, email: str) -> dict:
        status, body = _post(self.base, "/v1/auth/signup", {
            "email": email, "password": "SecurePass!1",
        })
        self.assertEqual(status, 201, f"signup failed: {body}")
        return body

    def _owner(self) -> str:
        return self.owner_token

    def _member(self) -> str:
        return self.member_token

    def _joiner(self) -> str:
        return self.joiner_token

    def _request(self, status_ok: int, token: str, path: str, body: dict | None = None):
        if body is not None:
            status, resp = _post(self.base, path, body, token)
        else:
            status, resp = _get(self.base, path, token)
        if status != status_ok:
            raise _Refusal(
                resp["error"]["code"], resp["error"]["message"], status=status
            )
        return resp

    def _create_room(self, caller=None, cap: int = 6) -> dict:
        caller = caller or self._owner()
        return self._request(HTTPStatus.CREATED, caller, "/v1/rooms/create", {"cap": cap})

    def _info(self, caller, room_id: str) -> dict:
        return self._request(HTTPStatus.OK, caller, f"/v1/rooms/info?room_id={room_id}")

    def _revoke(self, caller, room_id: str, link_id: str) -> dict:
        return self._request(
            HTTPStatus.OK, caller, "/v1/rooms/revoke_link",
            {"room_id": room_id, "link_id": link_id},
        )

    def _join(self, caller, room_id: str, link_token: str) -> dict:
        return self._request(
            HTTPStatus.OK, caller, "/v1/rooms/join",
            {"room_id": room_id, "link_token": link_token, "consent": True},
        )


if __name__ == "__main__":
    unittest.main()
