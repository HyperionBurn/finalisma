"""Agent keys carry their OWN room identity — one key = one agent.

The product promise is many agents on one link. An agent key is minted with a
label (``planner``, ``coder``, ``reviewer``) and the feature name implies one
key = one agent identity. This suite locks in that behaviour over the REAL
``WeftCloudService`` on REAL HTTP:

  - three keys from ONE account joining one room produce THREE distinct members
  - each key's messages carry a DISTINCT ``origin_agent``
  - unicast from key A to key B is seen by B and NOT by key C — same account
  - a key cannot spoof another key's identity via any argument
  - a revoked key's identity stops working on the very next request
  - a pre-existing session-based membership still resolves (no regression)
  - the room cap counts distinct key identities

Every test drives the wire surface — never an internal function — so it fails
if the wiring is ever removed again. Each test is written to FAIL against the
pre-change behaviour (all keys from one account collapsing to one member).
"""

from __future__ import annotations

import json
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


def _post(base: str, path: str, body: dict, token: str | None = None) -> tuple[int, dict]:
    data = json.dumps(body).encode("utf-8")
    req = urllib.request.Request(base + path, data=data, method="POST")
    req.add_header("Content-Type", "application/json")
    if token:
        req.add_header("Authorization", f"Bearer {token}")
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            raw = resp.read()
            return resp.status, json.loads(raw.decode("utf-8")) if raw else {}
    except urllib.error.HTTPError as exc:
        payload = {}
        try:
            raw = exc.read()
            payload = json.loads(raw.decode("utf-8")) if raw else {}
        except Exception:
            pass
        finally:
            exc.close()
        return exc.code, payload


def _get(base: str, path: str, token: str | None = None, query: str = "") -> tuple[int, dict]:
    req = urllib.request.Request(base + path + query, method="GET")
    if token:
        req.add_header("Authorization", f"Bearer {token}")
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            raw = resp.read()
            return resp.status, json.loads(raw.decode("utf-8")) if raw else {}
    except urllib.error.HTTPError as exc:
        payload = {}
        try:
            raw = exc.read()
            payload = json.loads(raw.decode("utf-8")) if raw else {}
        except Exception:
            pass
        finally:
            exc.close()
        return exc.code, payload


class AgentKeyIdentityTestBase(unittest.TestCase):
    """Runs the real cloud HTTP service on a background thread (one per class)."""

    @classmethod
    def setUpClass(cls) -> None:
        cls.tmpdir = tempfile.mkdtemp(prefix="weft-keyidentity-test-")
        cls.db_path = str(Path(cls.tmpdir) / "test.db")
        from http.server import ThreadingHTTPServer

        cls._httpd = ThreadingHTTPServer(("127.0.0.1", 0), _CloudHTTPHandler)
        cls.port = cls._httpd.server_address[1]
        cls.base = f"http://127.0.0.1:{cls.port}"
        cls.service = WeftCloudService(SqliteWalBackend(cls.db_path))
        _CloudHTTPHandler.service = cls.service
        cls.server_thread = threading.Thread(target=cls._httpd.serve_forever, daemon=True)
        cls.server_thread.start()

    @classmethod
    def tearDownClass(cls) -> None:
        if cls._httpd is not None:
            try:
                cls._httpd.shutdown()
            finally:
                cls._httpd.server_close()
        try:
            cls.service.backend.close()
        except Exception:
            pass
        import shutil
        shutil.rmtree(cls.tmpdir, ignore_errors=True)

    # -- helpers ------------------------------------------------------

    def _signup(self, email: str, password: str = "password-123") -> dict:
        status, resp = _post(self.base, "/v1/auth/signup", {"email": email, "password": password})
        self.assertEqual(status, HTTPStatus.CREATED, f"signup failed: {resp}")
        return resp

    def _mint_key(self, session: str, label: str = "default") -> dict:
        status, resp = _post(self.base, "/v1/agent-keys", {"label": label}, token=session)
        self.assertEqual(status, HTTPStatus.CREATED, f"create key failed: {resp}")
        self.assertTrue(resp["agent_key"].startswith("agk_"))
        return resp

    def _create_room(self, token: str, name: str = "room", cap: int = 10) -> dict:
        status, resp = _post(self.base, "/v1/rooms/create", {"name": name, "cap": cap}, token=token)
        self.assertEqual(status, HTTPStatus.CREATED, f"create room failed: {resp}")
        return resp

    def _join_room(self, token: str, room_id: str, link_token: str) -> tuple[int, dict]:
        return _post(self.base, "/v1/rooms/join",
                     {"room_id": room_id, "link_token": link_token, "consent": True},
                     token=token)

    def _room_info(self, token: str, room_id: str) -> tuple[int, dict]:
        return _get(self.base, "/v1/rooms/info", token=token, query=f"?room_id={room_id}")

    def _send(self, token: str, room_id: str, payload: dict, target_spec: str | list = "*") -> tuple[int, dict]:
        return _post(self.base, "/v1/rooms/send",
                     {"room_id": room_id, "target_spec": target_spec, "payload": payload},
                     token=token)

    def _poll(self, token: str, room_id: str, after_seq: int = 0) -> tuple[int, dict]:
        return _post(self.base, "/v1/rooms/poll",
                     {"room_id": room_id, "after_seq": after_seq}, token=token)

    def _three_keys(self, email: str) -> tuple[dict, dict, dict, dict]:
        """Sign up an account and mint planner/coder/reviewer keys."""
        acct = self._signup(email)
        planner = self._mint_key(acct["session_token"], "planner")
        coder = self._mint_key(acct["session_token"], "coder")
        reviewer = self._mint_key(acct["session_token"], "reviewer")
        return acct, planner, coder, reviewer


class KeyIdentityMembershipTests(AgentKeyIdentityTestBase):
    """Three keys from ONE account are THREE distinct room members."""

    def test_three_keys_from_one_account_join_one_room_show_three_distinct_members(self) -> None:
        _acct, planner, coder, reviewer = self._three_keys("three-keys@example.com")

        # Room owned by the planner key; coder + reviewer redeem the link.
        room = self._create_room(planner["agent_key"], name="three-keys")
        status, joined = self._join_room(coder["agent_key"], room["room_id"], room["link_token"])
        self.assertEqual(status, HTTPStatus.OK, f"coder join failed: {joined}")
        status, joined = self._join_room(reviewer["agent_key"], room["room_id"], room["link_token"])
        self.assertEqual(status, HTTPStatus.OK, f"reviewer join failed: {joined}")

        status, info = self._room_info(planner["agent_key"], room["room_id"])
        self.assertEqual(status, HTTPStatus.OK, f"room_info failed: {info}")
        self.assertEqual(info["member_count"], 3,
                         "three keys from one account must be three members, not one")
        member_ids = {m["agent_id"] for m in info["members"]}
        self.assertEqual(len(member_ids), 3, "the three members must have distinct identities")
        self.assertEqual(member_ids, {planner["key_id"], coder["key_id"], reviewer["key_id"]})
        self.assertNotIn(_acct["account_id"], member_ids,
                         "a key identity must never collapse to the account id")

    def test_each_keys_messages_carry_a_distinct_origin_agent(self) -> None:
        _acct, planner, coder, reviewer = self._three_keys("origin-keys@example.com")
        room = self._create_room(planner["agent_key"], name="origin")
        for key in (coder, reviewer):
            status, _ = self._join_room(key["agent_key"], room["room_id"], room["link_token"])
            self.assertEqual(status, HTTPStatus.OK)

        for key, text in ((planner, "from planner"), (coder, "from coder"), (reviewer, "from reviewer")):
            status, sent = self._send(key["agent_key"], room["room_id"], {"text": text})
            self.assertEqual(status, HTTPStatus.OK, f"send failed: {sent}")

        status, poll = self._poll(planner["agent_key"], room["room_id"], after_seq=0)
        self.assertEqual(status, HTTPStatus.OK)
        origins = {e["origin_agent"] for e in poll["events"] if e["kind"] == "room.message"}
        self.assertEqual(len(origins), 3,
                         "each key's messages must carry a distinct origin_agent")
        self.assertEqual(origins, {planner["key_id"], coder["key_id"], reviewer["key_id"]})
        self.assertNotIn(_acct["account_id"], origins)

    def test_cap_counts_distinct_key_identities(self) -> None:
        acct = self._signup("cap-keys@example.com")
        session = acct["session_token"]
        key1 = self._mint_key(session, "k1")
        key2 = self._mint_key(session, "k2")
        key3 = self._mint_key(session, "k3")

        # Room cap 3: owner (session account) + key1 + key2 fill it.
        room = self._create_room(session, name="cap", cap=3)
        for key in (key1, key2):
            status, joined = self._join_room(key["agent_key"], room["room_id"], room["link_token"])
            self.assertEqual(status, HTTPStatus.OK, f"join failed: {joined}")

        # key3 is a FOURTH distinct identity -> refused as room_full.
        status, refused = self._join_room(key3["agent_key"], room["room_id"], room["link_token"])
        self.assertEqual(status, HTTPStatus.CONFLICT, f"key3 join must hit the cap: {refused}")
        self.assertEqual(refused["error"]["code"], "room_full")

        status, info = self._room_info(session, room["room_id"])
        self.assertEqual(status, HTTPStatus.OK)
        self.assertEqual(info["member_count"], 3,
                         "cap accounting must count the three distinct identities")


class KeyIdentityUnicastTests(AgentKeyIdentityTestBase):
    """Unicast addressing between keys — including same-account confinement."""

    def test_unicast_from_key_a_to_key_b_is_not_visible_to_key_c_same_account(self) -> None:
        _acct, planner, coder, reviewer = self._three_keys("unicast-keys@example.com")
        room = self._create_room(planner["agent_key"], name="unicast")
        for key in (coder, reviewer):
            status, _ = self._join_room(key["agent_key"], room["room_id"], room["link_token"])
            self.assertEqual(status, HTTPStatus.OK)

        # Planner unicasts to the CODER's key identity.
        status, sent = self._send(planner["agent_key"], room["room_id"],
                                  {"text": "for-coder-eyes-only"}, target_spec=coder["key_id"])
        self.assertEqual(status, HTTPStatus.OK, f"unicast send failed: {sent}")
        self.assertEqual([r["agent_id"] for r in sent["receipts"]], [coder["key_id"]],
                         "the unicast must be addressed to coder's key identity only")

        # Coder (the addressee) sees the body.
        status, poll = self._poll(coder["agent_key"], room["room_id"], after_seq=0)
        self.assertEqual(status, HTTPStatus.OK)
        coder_msg = [e for e in poll["events"] if e["kind"] == "room.message"][-1]
        self.assertEqual(coder_msg["payload"]["payload"]["text"], "for-coder-eyes-only")

        # Reviewer — SAME account, different key — sees only the redacted envelope.
        status, poll = self._poll(reviewer["agent_key"], room["room_id"], after_seq=0)
        self.assertEqual(status, HTTPStatus.OK)
        reviewer_msg = [e for e in poll["events"] if e["kind"] == "room.message"][-1]
        self.assertEqual(reviewer_msg["payload"], {"redacted": True, "reason": "not_the_addressee"})
        self.assertNotIn("for-coder-eyes-only", json.dumps(poll),
                         "a sibling key of the same account must not read another key's unicast")

    def test_key_cannot_spoof_another_keys_identity_via_any_argument(self) -> None:
        _acct, planner, coder, reviewer = self._three_keys("spoof-keys@example.com")
        room = self._create_room(planner["agent_key"], name="spoof")
        for key in (coder, reviewer):
            status, _ = self._join_room(key["agent_key"], room["room_id"], room["link_token"])
            self.assertEqual(status, HTTPStatus.OK)

        # Every identity argument naming coder's key id is refused — identity is
        # derived server-side from the authenticated key, never from the body.
        attempts = [
            {"room_id": room["room_id"], "agent_id": coder["key_id"]},
            {"room_id": room["room_id"], "sender_agent_id": coder["key_id"],
             "target_spec": "*", "payload": {"text": "forged"}},
            {"room_id": room["room_id"], "caller_agent_id": coder["key_id"]},
            {"room_id": room["room_id"], "agent_id": coder["key_id"], "seq": 1},
        ]
        for body in attempts:
            with self.subTest(body=body):
                status, resp = _post(self.base, "/v1/rooms/poll" if "seq" in body else "/v1/rooms/send",
                                     body, token=planner["agent_key"])
                if "caller_agent_id" in body:
                    status, resp = _post(self.base, "/v1/rooms/close", body, token=planner["agent_key"])
                self.assertEqual(status, HTTPStatus.BAD_REQUEST)
                self.assertEqual(resp["error"]["code"], "invalid_argument")
                self.assertIn("not accepted", resp["error"]["message"])

        # room_join smuggling an agent_id is refused the same way.
        status, resp = _post(self.base, "/v1/rooms/join",
                             {"room_id": room["room_id"], "link_token": room["link_token"],
                              "consent": True, "agent_id": coder["key_id"]},
                             token=reviewer["agent_key"])
        self.assertEqual(status, HTTPStatus.BAD_REQUEST)
        self.assertIn("not accepted", resp["error"]["message"])

        # And the derivation is visible: each key's own identity is its own
        # key id — planner can never resolve as coder.
        status, me_planner = _get(self.base, "/v1/me", token=planner["agent_key"])
        status, me_coder = _get(self.base, "/v1/me", token=coder["agent_key"])
        self.assertEqual(status, HTTPStatus.OK)
        self.assertEqual(me_planner["agent_id"], planner["key_id"])
        self.assertEqual(me_coder["agent_id"], coder["key_id"])
        self.assertNotEqual(me_planner["agent_id"], me_coder["agent_id"],
                            "two keys from one account must resolve as distinct identities")


class KeyIdentityRevocationTests(AgentKeyIdentityTestBase):
    """A revoked key's identity stops working on the very next request."""

    def test_revoked_keys_identity_stops_on_the_next_request(self) -> None:
        acct = self._signup("revoke-keyidentity@example.com")
        session = acct["session_token"]
        planner = self._mint_key(session, "planner")
        coder = self._mint_key(session, "coder")

        room = self._create_room(planner["agent_key"], name="revoke")
        status, joined = self._join_room(coder["agent_key"], room["room_id"], room["link_token"])
        self.assertEqual(status, HTTPStatus.OK)

        status, revoked = _post(self.base, "/v1/agent-keys/revoke",
                                {"key_id": planner["key_id"]}, token=session)
        self.assertEqual(status, HTTPStatus.OK)
        self.assertIs(revoked["revoked"], True)

        # The very next request with the revoked key is refused.
        status, resp = self._poll(planner["agent_key"], room["room_id"])
        self.assertEqual(status, HTTPStatus.UNAUTHORIZED)
        self.assertEqual(resp["error"]["code"], "invalid_session")

        # A sibling key of the SAME account remains authenticated, but the
        # ownerless room is fail-closed rather than left consuming quota.
        status, me = _get(self.base, "/v1/me", token=coder["agent_key"])
        self.assertEqual(status, HTTPStatus.OK)
        self.assertEqual(me["agent_id"], coder["key_id"])
        status, closed = self._send(coder["agent_key"], room["room_id"], {"text": "still alive"})
        self.assertEqual(status, HTTPStatus.CONFLICT)
        self.assertEqual(closed["error"]["code"], "room_closed")

    def test_revoking_room_owner_key_closes_room_and_releases_quota(self) -> None:
        acct = self._signup("revoke-owner-room@example.com")
        session = acct["session_token"]
        planner = self._mint_key(session, "planner")
        coder = self._mint_key(session, "coder")
        reviewer = self._mint_key(session, "reviewer")

        room = self._create_room(planner["agent_key"], name="owner-revocation")
        status, joined = self._join_room(coder["agent_key"], room["room_id"], room["link_token"])
        self.assertEqual(status, HTTPStatus.OK, f"coder join failed: {joined}")

        status, revoked = _post(self.base, "/v1/agent-keys/revoke",
                                {"key_id": planner["key_id"]}, token=session)
        self.assertEqual(status, HTTPStatus.OK)
        self.assertIs(revoked["revoked"], True)

        status, info = self._room_info(coder["agent_key"], room["room_id"])
        self.assertEqual(status, HTTPStatus.OK, f"room_info failed: {info}")
        self.assertEqual(info["state"], "closed")

        status, join_error = self._join_room(
            reviewer["agent_key"], room["room_id"], room["link_token"],
        )
        self.assertEqual(status, HTTPStatus.CONFLICT)
        self.assertEqual(join_error["error"]["code"], "room_closed")

        # The closed owner room no longer consumes the tenant's room quota.
        for index in range(5):
            replacement = self._create_room(session, name=f"replacement-{index}")
            self.assertTrue(replacement["room_id"].startswith("room_"))


class KeyIdentityRegressionTests(AgentKeyIdentityTestBase):
    """Session-derived identity and pre-existing memberships are unchanged."""

    def test_pre_existing_session_membership_still_resolves_after_change(self) -> None:
        acct = self._signup("session-membership@example.com")
        session = acct["session_token"]

        # A session-based room + membership + event history, exactly as it
        # would exist before this change.
        room = self._create_room(session, name="session-room")
        status, sent = self._send(session, room["room_id"], {"text": "pre-existing history"})
        self.assertEqual(status, HTTPStatus.OK)

        # The same session still resolves the room, its roster, and its cursor.
        status, info = self._room_info(session, room["room_id"])
        self.assertEqual(status, HTTPStatus.OK)
        self.assertEqual([m["agent_id"] for m in info["members"]], [acct["account_id"]],
                         "session identity is the account, unchanged")
        status, poll = self._poll(session, room["room_id"], after_seq=0)
        self.assertEqual(status, HTTPStatus.OK)
        self.assertEqual(poll["events"][-1]["payload"]["payload"]["text"], "pre-existing history")

        # A re-login (fresh session token) still resolves the same membership:
        # identity is the account, so the seat survives the token rotation.
        status, signin = _post(self.base, "/v1/auth/signin",
                               {"email": "session-membership@example.com", "password": "password-123"})
        self.assertEqual(status, HTTPStatus.OK)
        self.assertEqual(signin["account_id"], acct["account_id"])
        status, info2 = self._room_info(signin["session_token"], room["room_id"])
        self.assertEqual(status, HTTPStatus.OK, "fresh session must still resolve the old membership")
        self.assertEqual(info2["member_count"], 1)

        # A session reports agent_id == account_id (identity unchanged).
        status, me = _get(self.base, "/v1/me", token=signin["session_token"])
        self.assertEqual(status, HTTPStatus.OK)
        self.assertEqual(me["agent_id"], acct["account_id"])


if __name__ == "__main__":
    unittest.main()
