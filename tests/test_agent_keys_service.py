"""Agent API keys — hosted HTTP service surface + security contract (TDD — RED).

Drives the REAL ``WeftCloudService`` over REAL HTTP. Agent keys are the
config-file credential for agent-to-agent products: long-lived, revocable,
scoped to tenant + account. They must resolve to the SAME ``SessionContext``
a session produces and flow through the EXACT same authorization, tenancy,
rate-limit, and quota checks — one authentication funnel, two credential
types. A second credential type that skips a check the session path performs
is the bug this file exists to catch.

The security surface under test (each is a test):

  - create/list/revoke keys are SESSION-ONLY: a leaked agent key cannot mint
    more keys or revoke the owner's credentials (lockout);
  - an agent key drives the full room surface end to end, and a revoked key
    is refused on the very next request (the product's core promise);
  - an agent key CANNOT read or write another tenant's data;
  - an agent key CANNOT perform an action its account is not permitted to do
    (member key cannot invite; owner key can);
  - rate limiting AND quotas apply to agent-key requests exactly as to
    session requests (one shared budget);
  - an invalid/unknown agent key refuses BYTE-IDENTICALLY to a well-formed
    but wrong one (no enumeration oracle).

Authoritative spec: docs/AGENT_KEYS.md.
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


def _raw(base: str, method: str, path: str, body: dict | None = None,
         token: str | None = None, query: str = "") -> tuple[int, bytes, dict]:
    data = json.dumps(body).encode("utf-8") if body is not None else b""
    req = urllib.request.Request(base + path + query, data=data, method=method)
    req.add_header("Content-Type", "application/json")
    if token:
        req.add_header("Authorization", f"Bearer {token}")
    try:
        with urllib.request.urlopen(req, timeout=20) as resp:
            return resp.status, resp.read(), dict(resp.getheaders())
    except urllib.error.HTTPError as exc:
        payload = exc.read()
        headers = dict(exc.getheaders())
        exc.close()
        return exc.code, payload, headers


def _post(base: str, path: str, body: dict, token: str | None = None) -> tuple[int, dict]:
    status, raw, _ = _raw(base, "POST", path, body=body, token=token)
    return status, json.loads(raw.decode("utf-8")) if raw else {}


def _get(base: str, path: str, token: str | None = None, query: str = "") -> tuple[int, dict]:
    status, raw, _ = _raw(base, "GET", path, token=token, query=query)
    return status, json.loads(raw.decode("utf-8")) if raw else {}


class AgentKeyServiceTestBase(unittest.TestCase):
    """Runs the real cloud HTTP service on a background thread (one per class)."""

    @classmethod
    def setUpClass(cls) -> None:
        cls.tmpdir = tempfile.mkdtemp(prefix="weft-agentkeys-test-")
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

    def _signup(self, email: str, password: str = "password-123") -> dict:
        status, resp = _post(self.base, "/v1/auth/signup", {"email": email, "password": password})
        self.assertEqual(status, HTTPStatus.CREATED, f"signup failed: {resp}")
        return resp

    def _create_key(self, session: str, label: str = "ci") -> dict:
        status, resp = _post(self.base, "/v1/agent-keys", {"label": label}, token=session)
        self.assertEqual(status, HTTPStatus.CREATED, f"create key failed: {resp}")
        self.assertTrue(resp["agent_key"].startswith("agk_"))
        return resp

    def _create_room(self, token: str, name: str = "room", cap: int = 10) -> dict:
        status, resp = _post(self.base, "/v1/rooms/create", {"name": name, "cap": cap}, token=token)
        self.assertEqual(status, HTTPStatus.CREATED, f"create room failed: {resp}")
        return resp

    def _demote_to_member(self, account_id: str) -> None:
        with self.service.backend.transaction() as tx:
            tx.execute(
                "UPDATE cloud_identity_members SET role = 'member' WHERE account_id = ?",
                (account_id,),
            )
            tx.commit()


class AgentKeyEndToEndTests(AgentKeyServiceTestBase):
    """The product promise, proven by request: create key -> use it -> revoke -> refused."""

    def test_agent_key_cannot_manage_agent_keys(self) -> None:
        acct = self._signup("management-boundary@example.com")
        session = acct["session_token"]
        key = self._create_key(session, label="management-boundary")
        raw_key = key["agent_key"]

        status, _ = _post(self.base, "/v1/agent-keys", {"label": "nested"}, token=raw_key)
        self.assertEqual(status, HTTPStatus.UNAUTHORIZED)
        status, _ = _get(self.base, "/v1/agent-keys", token=raw_key)
        self.assertEqual(status, HTTPStatus.UNAUTHORIZED)
        status, _ = _post(
            self.base,
            "/v1/agent-keys/revoke",
            {"key_id": key["key_id"]},
            token=raw_key,
        )
        self.assertEqual(status, HTTPStatus.UNAUTHORIZED)

    def test_create_use_revoke_refuse_round_trip(self) -> None:
        acct = self._signup("e2e@example.com")
        session = acct["session_token"]
        key = self._create_key(session, label="prod-agent")
        raw_key = key["agent_key"]
        key_id = key["key_id"]

        # The key drives the real room surface end to end.
        room = self._create_room(raw_key, name="e2e")
        room_id = room["room_id"]
        status, joined = _post(
            self.base,
            "/v1/rooms/join",
            {"room_id": room_id, "link_token": room["link_token"], "consent": True},
            token=raw_key,
        )
        self.assertEqual(status, HTTPStatus.OK, f"key room_join failed: {joined}")
        status, send = _post(self.base, "/v1/rooms/send",
                             {"room_id": room_id, "target_spec": "*",
                              "payload": {"text": "hello"}}, token=raw_key)
        self.assertEqual(status, HTTPStatus.OK, f"key room_send failed: {send}")
        self.assertGreater(send["seq"], 0)

        # Revoke through the session, then the next request with the key is refused.
        status, rev = _post(self.base, "/v1/agent-keys/revoke",
                            {"key_id": key_id}, token=session)
        self.assertEqual(status, HTTPStatus.OK, f"revoke failed: {rev}")
        self.assertIs(rev["revoked"], True)

        status, resp = _post(self.base, "/v1/rooms/send",
                             {"room_id": room_id, "target_spec": "*", "payload": {}},
                             token=raw_key)
        self.assertEqual(status, HTTPStatus.UNAUTHORIZED)
        self.assertEqual(resp["error"]["code"], "invalid_session")

    def test_agent_key_created_room_is_account_owned_and_session_administered(self) -> None:
        acct = self._signup("account-owned-room@example.com")
        session = acct["session_token"]
        key = self._create_key(session, label="room-creator")
        raw_key = key["agent_key"]

        room = self._create_room(raw_key, name="account-owned")
        self.assertEqual(room["owner_agent_id"], acct["account_id"])

        status, listing = _get(self.base, "/v1/rooms", token=session)
        self.assertEqual(status, HTTPStatus.OK, f"session room list failed: {listing}")
        self.assertIn(room["room_id"], {entry["room_id"] for entry in listing["rooms"]})

        # The key remains a distinct agent identity. It must explicitly redeem
        # the link before it can send, even though its account owns the room.
        status, denied = _post(
            self.base,
            "/v1/rooms/send",
            {"room_id": room["room_id"], "target_spec": "*", "payload": {}},
            token=raw_key,
        )
        self.assertEqual(status, HTTPStatus.NOT_FOUND)
        self.assertEqual(denied["error"]["code"], "room_not_found")

        status, closed = _post(
            self.base, "/v1/rooms/close", {"room_id": room["room_id"]}, token=session,
        )
        self.assertEqual(status, HTTPStatus.OK, f"session room close failed: {closed}")
        self.assertEqual(closed["state"], "closed")

    def test_agent_key_can_administer_own_room_after_join(self) -> None:
        acct = self._signup("agent-key-owner-lifecycle@example.com")
        session = acct["session_token"]
        owner_key = self._create_key(session, label="room-owner")
        member_key = self._create_key(session, label="room-member")

        room = self._create_room(owner_key["agent_key"], name="key-owner-lifecycle", cap=15)
        status, joined = _post(
            self.base,
            "/v1/rooms/join",
            {"room_id": room["room_id"], "link_token": room["link_token"], "consent": True},
            token=owner_key["agent_key"],
        )
        self.assertEqual(status, HTTPStatus.OK, f"owner key join failed: {joined}")
        status, joined = _post(
            self.base,
            "/v1/rooms/join",
            {"room_id": room["room_id"], "link_token": room["link_token"], "consent": True},
            token=member_key["agent_key"],
        )
        self.assertEqual(status, HTTPStatus.OK, f"member key join failed: {joined}")

        status, listing = _get(self.base, "/v1/rooms", token=owner_key["agent_key"])
        self.assertEqual(status, HTTPStatus.OK, f"key room list failed: {listing}")
        self.assertIn(room["room_id"], {entry["room_id"] for entry in listing["rooms"]})

        status, info = _get(
            self.base,
            "/v1/rooms/info",
            token=owner_key["agent_key"],
            query=f"?room_id={room['room_id']}",
        )
        self.assertEqual(status, HTTPStatus.OK, f"key room info failed: {info}")
        self.assertEqual(info["link_id"], room["link_id"])

        status, removed = _post(
            self.base,
            "/v1/rooms/remove_member",
            {"room_id": room["room_id"], "member_id": member_key["key_id"]},
            token=owner_key["agent_key"],
        )
        self.assertEqual(status, HTTPStatus.OK, f"key remove_member failed: {removed}")
        self.assertEqual(removed["agent_id"], member_key["key_id"])

        status, revoked = _post(
            self.base,
            "/v1/rooms/revoke_link",
            {"room_id": room["room_id"], "link_id": room["link_id"]},
            token=owner_key["agent_key"],
        )
        self.assertEqual(status, HTTPStatus.OK, f"key revoke_link failed: {revoked}")
        self.assertIs(revoked["revoked"], True)

        status, closed = _post(
            self.base, "/v1/rooms/close", {"room_id": room["room_id"]},
            token=owner_key["agent_key"],
        )
        self.assertEqual(status, HTTPStatus.OK, f"key room close failed: {closed}")
        self.assertEqual(closed["state"], "closed")
        status, repeated = _post(
            self.base, "/v1/rooms/close", {"room_id": room["room_id"]},
            token=owner_key["agent_key"],
        )
        self.assertEqual(status, HTTPStatus.OK, f"repeated key room close failed: {repeated}")
        self.assertEqual(repeated["state"], "closed")

        replacement = self._create_room(session, name="key-owner-replacement", cap=15)
        self.assertTrue(replacement["room_id"].startswith("room_"))

    def test_cross_account_key_keeps_room_access_without_owner_controls(self) -> None:
        owner = self._signup("agent-key-cross-account-owner@example.com")
        room = self._create_room(owner["session_token"], name="cross-account", cap=15)
        member = self._signup("agent-key-cross-account-member@example.com")
        member_key = self._create_key(member["session_token"], label="cross-account-member")

        status, joined = _post(
            self.base,
            "/v1/rooms/join",
            {"room_id": room["room_id"], "link_token": room["link_token"], "consent": True},
            token=member_key["agent_key"],
        )
        self.assertEqual(status, HTTPStatus.OK, f"cross-account key join failed: {joined}")

        status, info = _get(
            self.base,
            "/v1/rooms/info",
            token=member_key["agent_key"],
            query=f"?room_id={room['room_id']}",
        )
        self.assertEqual(status, HTTPStatus.OK, f"cross-account key info failed: {info}")
        self.assertEqual(info["owner_agent_id"], owner["account_id"])
        self.assertNotIn("link_id", info)

        status, refused = _post(
            self.base, "/v1/rooms/close", {"room_id": room["room_id"]},
            token=member_key["agent_key"],
        )
        self.assertEqual(status, HTTPStatus.FORBIDDEN)
        self.assertEqual(refused["error"]["code"], "owner_required")

        status, closed = _post(
            self.base, "/v1/rooms/close", {"room_id": room["room_id"]},
            token=owner["session_token"],
        )
        self.assertEqual(status, HTTPStatus.OK, f"owner room close failed: {closed}")

    def test_agent_key_connect_room_is_visible_to_account_session(self) -> None:
        acct = self._signup("account-owned-connect@example.com")
        session = acct["session_token"]
        raw_key = self._create_key(session, label="connect-creator")["agent_key"]

        status, connected = _post(
            self.base,
            "/v1/rooms/connect",
            {"name": "account-owned-connect", "cap": 8},
            token=raw_key,
        )
        self.assertEqual(status, HTTPStatus.CREATED, f"key room connect failed: {connected}")

        status, listing = _get(self.base, "/v1/rooms", token=session)
        self.assertEqual(status, HTTPStatus.OK, f"session room list failed: {listing}")
        self.assertIn(connected["room_id"], {entry["room_id"] for entry in listing["rooms"]})

        status, closed = _post(
            self.base, "/v1/rooms/close", {"room_id": connected["room_id"]}, token=session,
        )
        self.assertEqual(status, HTTPStatus.OK, f"session room close failed: {closed}")
        self.assertEqual(closed["state"], "closed")

    def test_agent_key_create_and_connect_default_to_fifteen_members(self) -> None:
        acct = self._signup("agent-key-default-cap@example.com")
        session = acct["session_token"]
        raw_key = self._create_key(session, label="default-cap")["agent_key"]

        status, created = _post(
            self.base, "/v1/rooms/create", {"name": "default-create"}, token=raw_key,
        )
        self.assertEqual(status, HTTPStatus.CREATED, f"default key create failed: {created}")
        self.assertEqual(created["cap"], 15)

        status, connected = _post(
            self.base, "/v1/rooms/connect", {"name": "default-connect"}, token=raw_key,
        )
        self.assertEqual(status, HTTPStatus.CREATED, f"default key connect failed: {connected}")
        self.assertEqual(connected["cap"], 15)

        for room_id in (created["room_id"], connected["room_id"]):
            status, closed = _post(
                self.base, "/v1/rooms/close", {"room_id": room_id}, token=session,
            )
            self.assertEqual(status, HTTPStatus.OK, f"cleanup close failed: {closed}")

    def test_revoked_key_refused_on_the_very_next_request(self) -> None:
        acct = self._signup("revoke-next@example.com")
        key = self._create_key(acct["session_token"])
        raw_key = key["agent_key"]
        self._create_room(raw_key)  # valid before revoke

        status, _ = _post(self.base, "/v1/agent-keys/revoke",
                          {"key_id": key["key_id"]}, token=acct["session_token"])
        self.assertEqual(status, HTTPStatus.OK)

        status, resp = _get(self.base, "/v1/me", token=raw_key)
        self.assertEqual(status, HTTPStatus.UNAUTHORIZED)
        self.assertEqual(resp["error"]["code"], "invalid_session")


class AgentKeyIsolationTests(AgentKeyServiceTestBase):
    """Cross-tenant and role confinement through the key funnel."""

    def test_key_cannot_read_or_write_another_tenants_data(self) -> None:
        a = self._signup("tenant-a@example.com")
        key_a = self._create_key(a["session_token"])["agent_key"]
        room_a = self._create_room(key_a, name="A's room")

        b = self._signup("tenant-b@example.com")
        key_b = self._create_key(b["session_token"])["agent_key"]

        # B's key reaching A's room_id by id alone gets the identical refusal as
        # a fabricated room_id — no cross-tenant read AND no existence oracle.
        status_b, resp_b = _get(self.base, "/v1/rooms/info",
                                token=key_b, query=f"?room_id={room_a['room_id']}")
        status_fab, resp_fab = _get(self.base, "/v1/rooms/info",
                                    token=key_b, query="?room_id=room_nonexistent_0000")
        self.assertEqual(status_b, HTTPStatus.NOT_FOUND)
        self.assertEqual(resp_b, resp_fab)

        # B's key cannot WRITE into A's room either.
        status, resp = _post(self.base, "/v1/rooms/send",
                             {"room_id": room_a["room_id"], "target_spec": "*",
                              "payload": {"text": "trespass"}}, token=key_b)
        self.assertEqual(status, HTTPStatus.NOT_FOUND)
        self.assertEqual(resp["error"]["code"], "room_not_found")

        # And A's own key still works (the refusal was confinement, not breakage).
        status, joined = _post(
            self.base,
            "/v1/rooms/join",
            {"room_id": room_a["room_id"], "link_token": room_a["link_token"], "consent": True},
            token=key_a,
        )
        self.assertEqual(status, HTTPStatus.OK, f"A key join failed: {joined}")
        status, _ = _post(self.base, "/v1/rooms/send",
                          {"room_id": room_a["room_id"], "target_spec": "*",
                           "payload": {"text": "still mine"}}, token=key_a)
        self.assertEqual(status, HTTPStatus.OK)

    def test_key_never_exceeds_the_creating_accounts_role(self) -> None:
        # Owner key CAN invite (role honoured, same as a session).
        owner = self._signup("owner-key@example.com")
        owner_key = self._create_key(owner["session_token"], label="owner")["agent_key"]
        status, resp = _post(self.base, "/v1/org/invite",
                             {"email": "newbie@example.com", "role": "member"},
                             token=owner_key)
        self.assertEqual(status, HTTPStatus.CREATED, f"owner key invite failed: {resp}")

        # A demoted account's key re-derives the role on the NEXT request and
        # can no longer perform the admin-gated action — same RoleError, same
        # funnel as a session would hit.
        acct = self._signup("demoted-key@example.com")
        key = self._create_key(acct["session_token"], label="was-owner")
        raw_key = key["agent_key"]
        self._demote_to_member(acct["account_id"])

        status, resp = _post(self.base, "/v1/org/invite",
                             {"email": "victim@example.com", "role": "member"},
                             token=raw_key)
        self.assertEqual(status, HTTPStatus.FORBIDDEN)
        self.assertEqual(resp["error"]["code"], "forbidden")

        # The demoted key is still valid for what a member MAY do (proving the
        # refusal is role-gated, not a revoked/unknown key).
        status, resp = _get(self.base, "/v1/me", token=raw_key)
        self.assertEqual(status, HTTPStatus.OK)
        self.assertEqual(resp["role"], "member")


class AgentKeyQuotaAndRateLimitTests(AgentKeyServiceTestBase):
    """Quotas and rate limits apply to agent-key requests exactly as sessions."""

    def test_quota_enforced_for_agent_key_request(self) -> None:
        acct = self._signup("quota-key@example.com")
        key = self._create_key(acct["session_token"])["agent_key"]
        # Free plan caps room members at 15; cap=50 must be refused with the
        # SAME quota error a session would get.
        status, resp = _post(self.base, "/v1/rooms/create", {"name": "q", "cap": 50}, token=key)
        self.assertEqual(status, HTTPStatus.CONFLICT)
        self.assertEqual(resp["error"]["code"], "quota_exceeded")

        # Control: a session gets the identical refusal.
        status_s, resp_s = _post(self.base, "/v1/rooms/create",
                                 {"name": "q", "cap": 50}, token=acct["session_token"])
        self.assertEqual(status_s, HTTPStatus.CONFLICT)
        self.assertEqual(resp_s, resp)

    def test_member_cap_boundaries_match_session_path(self) -> None:
        acct = self._signup("cap-boundary-key@example.com")
        session = acct["session_token"]
        key = self._create_key(session, label="cap-boundary")["agent_key"]

        status, session_refused = _post(
            self.base, "/v1/rooms/create", {"name": "session-over", "cap": 16}, token=session,
        )
        self.assertEqual(status, HTTPStatus.CONFLICT)

        status, refused = _post(
            self.base, "/v1/rooms/create", {"name": "over", "cap": 16}, token=key,
        )
        self.assertEqual(status, HTTPStatus.CONFLICT)
        self.assertEqual(refused["error"], session_refused["error"])
        self.assertEqual(refused["error"]["code"], "quota_exceeded")
        self.assertEqual(refused["error"]["limit"], {
            "name": "max_members_per_room", "value": 15, "plan": "free",
        })

        status, accepted = _post(
            self.base, "/v1/rooms/create", {"name": "at-limit", "cap": 15}, token=key,
        )
        self.assertEqual(status, HTTPStatus.CREATED, f"cap 15 should pass: {accepted}")
        self.assertEqual(accepted["cap"], 15)
        status, closed = _post(
            self.base, "/v1/rooms/close", {"room_id": accepted["room_id"]}, token=session,
        )
        self.assertEqual(status, HTTPStatus.OK, f"cleanup close failed: {closed}")

    def test_room_count_quota_is_shared_between_key_and_session(self) -> None:
        acct = self._signup("room-count-key@example.com")
        session = acct["session_token"]
        key = self._create_key(session, label="room-count")["agent_key"]
        rooms = []
        for index in range(5):
            status, room = _post(
                self.base, "/v1/rooms/create", {"name": f"room-{index}", "cap": 2}, token=key,
            )
            self.assertEqual(status, HTTPStatus.CREATED, f"room {index} failed: {room}")
            rooms.append(room)

        status, refused = _post(
            self.base, "/v1/rooms/create", {"name": "room-over", "cap": 2}, token=session,
        )
        self.assertEqual(status, HTTPStatus.CONFLICT)
        self.assertEqual(refused["error"]["code"], "quota_exceeded")
        self.assertEqual(refused["error"]["limit"]["name"], "max_rooms")
        self.assertEqual(refused["error"]["limit"]["value"], 5)
        self.assertEqual(refused["error"]["limit"]["plan"], "free")

        status, closed = _post(
            self.base, "/v1/rooms/close", {"room_id": rooms[0]["room_id"]}, token=session,
        )
        self.assertEqual(status, HTTPStatus.OK, f"session cleanup close failed: {closed}")
        status, replacement = _post(
            self.base, "/v1/rooms/create", {"name": "room-replacement", "cap": 2}, token=key,
        )
        self.assertEqual(status, HTTPStatus.CREATED, f"replacement failed: {replacement}")
        for room in [*rooms[1:], replacement]:
            status, closed = _post(
                self.base, "/v1/rooms/close", {"room_id": room["room_id"]}, token=session,
            )
            self.assertEqual(status, HTTPStatus.OK, f"room cleanup close failed: {closed}")

    def test_message_rate_limit_shared_between_session_and_key(self) -> None:
        acct = self._signup("ratelimit-key@example.com")
        session = acct["session_token"]
        key = self._create_key(session)["agent_key"]
        room = self._create_room(session, cap=10)
        room_id = room["room_id"]

        # The key is a DISTINCT identity (one key = one agent), so it must
        # redeem the link to become a member of the session-owned room before
        # it can send — exactly as any second agent would.
        status, joined = _post(self.base, "/v1/rooms/join",
                               {"room_id": room_id, "link_token": room["link_token"],
                                "consent": True}, token=key)
        self.assertEqual(status, HTTPStatus.OK, f"key join failed: {joined}")
        self.assertEqual(joined["status"], "active")

        # The free plan budget is 60 messages/minute per room. Draw 40 with the
        # session and 20 with the key — the funnel must treat them as ONE
        # budget, so the 61st is refused whichever credential presents it.
        for i in range(40):
            status, resp = _post(self.base, "/v1/rooms/send",
                                 {"room_id": room_id, "target_spec": "*",
                                  "payload": {"n": i}}, token=session)
            self.assertEqual(status, HTTPStatus.OK, f"session send {i}: {resp}")
        for i in range(20):
            status, resp = _post(self.base, "/v1/rooms/send",
                                 {"room_id": room_id, "target_spec": "*",
                                  "payload": {"n": 100 + i}}, token=key)
            self.assertEqual(status, HTTPStatus.OK, f"key send {i}: {resp}")

        status, resp = _post(self.base, "/v1/rooms/send",
                             {"room_id": room_id, "target_spec": "*", "payload": {}},
                             token=key)
        self.assertEqual(status, HTTPStatus.TOO_MANY_REQUESTS)
        self.assertEqual(resp["error"]["code"], "rate_limited")
        self.assertIn("retry_after", resp["error"])


class AgentKeyNoOracleTests(AgentKeyServiceTestBase):
    """Invalid/unknown keys refuse byte-identically to well-formed-but-wrong ones."""

    def _body(self, status: int, raw: bytes) -> bytes:
        return raw

    def test_invalid_unknown_and_revoked_keys_refuse_byte_identically(self) -> None:
        acct = self._signup("oracle-key@example.com")
        key = self._create_key(acct["session_token"])["agent_key"]
        key_id = self._create_key(acct["session_token"], label="doomed")["key_id"]

        wellformed_wrong = key[:-1] + ("X" if key[-1] != "X" else "Y")
        unknown = "agk_totally-made-up-key-token"
        revoked = key_id  # the other key's id is not its secret; use a real one

        # Revoke one key so a revoked-key probe is in the set.
        _post(self.base, "/v1/agent-keys/revoke",
              {"key_id": key_id}, token=acct["session_token"])

        bodies: list[bytes] = []
        for tok in (wellformed_wrong, unknown):
            status, raw, _ = _raw(self.base, "GET", "/v1/me", token=tok)
            self.assertEqual(status, HTTPStatus.UNAUTHORIZED)
            bodies.append(raw)
        self.assertEqual(bodies[0], bodies[1],
                         "unknown and well-formed-but-wrong keys must refuse byte-identically")

        # The revoked key also refuses with the same bytes (no revocation oracle).
        # Its secret must be fetched via the list after creation; here we create
        # a fresh one and revoke it to compare.
        doomed = self._create_key(acct["session_token"], label="doomed-2")
        _post(self.base, "/v1/agent-keys/revoke",
              {"key_id": doomed["key_id"]}, token=acct["session_token"])
        status, raw_revoked, _ = _raw(self.base, "GET", "/v1/me", token=doomed["agent_key"])
        self.assertEqual(status, HTTPStatus.UNAUTHORIZED)
        self.assertEqual(raw_revoked, bodies[0],
                         "a revoked key must refuse byte-identically to an unknown one")

        # A malformed fss_ session produces the SAME bytes too (no oracle for
        # which credential type is valid).
        status, raw_fss, _ = _raw(self.base, "GET", "/v1/me", token="fss_tampered-session")
        self.assertEqual(status, HTTPStatus.UNAUTHORIZED)
        self.assertEqual(raw_fss, bodies[0])

    def test_mcp_refuses_unknown_and_wrong_keys_byte_identically(self) -> None:
        acct = self._signup("mcp-oracle@example.com")
        key = self._create_key(acct["session_token"])["agent_key"]
        wellformed_wrong = key[:-1] + ("X" if key[-1] != "X" else "Y")
        unknown = "agk_no-such-key-at-all"

        def _mcp_raw(tok: str) -> bytes:
            body = json.dumps({"jsonrpc": "2.0", "id": 1, "method": "initialize",
                               "params": {}}).encode("utf-8")
            req = urllib.request.Request(self.base + "/mcp", data=body, method="POST")
            req.add_header("Content-Type", "application/json")
            req.add_header("Authorization", f"Bearer {tok}")
            try:
                with urllib.request.urlopen(req, timeout=20) as resp:
                    return resp.read()
            except urllib.error.HTTPError as exc:
                raw = exc.read()
                exc.close()
                return raw

        self.assertEqual(_mcp_raw(wellformed_wrong), _mcp_raw(unknown),
                         "MCP must refuse unknown and wrong agent keys byte-identically")


class AgentKeyListTests(AgentKeyServiceTestBase):
    """Listing exposes id/label/created/last-used — never the secret."""

    def test_list_shows_metadata_never_secret_or_hash(self) -> None:
        acct = self._signup("list-key@example.com")
        session = acct["session_token"]
        key = self._create_key(session, label="web-app")
        raw_key = key["agent_key"]

        # Use it once so last_used_at is populated.
        _get(self.base, "/v1/me", token=raw_key)

        status, resp = _get(self.base, "/v1/agent-keys", token=session)
        self.assertEqual(status, HTTPStatus.OK)
        keys = resp["keys"]
        self.assertEqual(len(keys), 1)
        entry = keys[0]
        self.assertEqual(entry["key_id"], key["key_id"])
        self.assertEqual(entry["label"], "web-app")
        self.assertIsNotNone(entry["created_at"])
        self.assertIsNotNone(entry["last_used_at"])
        self.assertIsNone(entry["revoked_at"])
        serialized = json.dumps(keys)
        self.assertNotIn("agk_", serialized, "list must never contain the raw credential")
        self.assertNotIn("token_hash", serialized, "list must never expose the stored hash")

    def test_create_returns_raw_key_exactly_once_field(self) -> None:
        acct = self._signup("show-once@example.com")
        key = self._create_key(acct["session_token"], label="once")
        self.assertTrue(key["agent_key"].startswith("agk_"))
        # The creation response is the ONLY place the raw key appears: listing it
        # again yields metadata only, and the raw key is not retrievable.
        status, resp = _get(self.base, "/v1/agent-keys", token=acct["session_token"])
        self.assertEqual(status, HTTPStatus.OK)
        self.assertNotIn(key["agent_key"], json.dumps(resp))


if __name__ == "__main__":
    unittest.main()
