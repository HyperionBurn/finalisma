"""Cloud service integration tests — the product claim, end-to-end.

Drives the REAL ``FinalismaCloudService`` over REAL HTTP (via a
``ThreadingHTTPServer`` on a background thread) — never against mocks or
in-process fakes. Covers the full account → room → multi-agent flow plus the
negative cases that prove enforcement.

Authoritative spec: docs/ROOMS_DESIGN.md §9 (negative cases),
docs/PRODUCT_ROADMAP.md §2 (one link, many agents).
"""

from __future__ import annotations

import json
import sys
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.request
from http import HTTPStatus
from pathlib import Path

# Ensure the src package is importable.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from weft_cloud.service import FinalismaCloudService, _CloudHTTPHandler
from weft_cloud.storage import SqliteWalBackend


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
        payload = {}
        try:
            payload = json.loads(exc.read().decode("utf-8"))
        except Exception:
            pass
        return exc.code, payload


def _get(base: str, path: str, token: str | None = None) -> tuple[int, dict]:
    req = urllib.request.Request(base + path, method="GET")
    if token:
        req.add_header("Authorization", f"Bearer {token}")
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            return resp.status, json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        payload = {}
        try:
            payload = json.loads(exc.read().decode("utf-8"))
        except Exception:
            pass
        return exc.code, payload


class CloudServiceTestBase(unittest.TestCase):
    """Base class that spins up a real HTTP service on a background thread."""

    def setUp(self) -> None:
        self.tmpdir = tempfile.mkdtemp(prefix="finalisma-test-")
        self.db_path = str(Path(self.tmpdir) / "test.db")
        self.service = FinalismaCloudService(SqliteWalBackend(self.db_path))
        self.port = 18800 + (hash(self.tmpdir) % 1000)
        self.server = threading.Thread(
            target=self._serve, daemon=True,
        )
        self.base = f"http://127.0.0.1:{self.port}"
        self._server_started = threading.Event()
        self.server.start()
        self._server_started.wait(timeout=5)

    def _serve(self) -> None:
        from http.server import ThreadingHTTPServer
        _CloudHTTPHandler.service = self.service
        httpd = ThreadingHTTPServer(("127.0.0.1", self.port), _CloudHTTPHandler)
        self._httpd = httpd
        self._server_started.set()
        httpd.serve_forever()

    def tearDown(self) -> None:
        if hasattr(self, "_httpd"):
            self._httpd.shutdown()
        # Release WAL handles so the tempdir can be removed on Windows.
        try:
            self.service.backend.close()
        except Exception:
            pass
        import shutil
        try:
            shutil.rmtree(self.tmpdir, ignore_errors=True)
        except Exception:
            pass

    def _signup(self, email: str, password: str) -> dict:
        status, body = _post(self.base, "/v1/auth/signup", {
            "email": email, "password": password,
        })
        self.assertEqual(status, 201, f"signup failed: {body}")
        return body

    def _signin(self, email: str, password: str) -> dict:
        status, body = _post(self.base, "/v1/auth/signin", {
            "email": email, "password": password,
        })
        self.assertEqual(status, 200, f"signin failed: {body}")
        return body

    def _create_room(self, token: str, cap: int = 6, **kwargs) -> dict:
        status, body = _post(self.base, "/v1/rooms/create", {
            "cap": cap, **kwargs,
        }, token)
        self.assertEqual(status, 201, f"room create failed: {body}")
        return body

    def _join_room(self, token: str, room_id: str, link_token: str,
                   agent_id: str, consent: bool = True) -> dict:
        status, body = _post(self.base, "/v1/rooms/join", {
            "room_id": room_id,
            "link_token": link_token,
            "agent_id": agent_id,
            "consent": consent,
        }, token)
        self.assertEqual(status, 200, f"join failed: {body}")
        return body


class TestAccountAndOrgFlow(CloudServiceTestBase):
    """Sign up, sign in, org membership."""

    def test_signup_returns_session_and_tenant(self) -> None:
        result = self._signup("alice@example.com", "SecurePass!1")
        self.assertIn("session_token", result)
        self.assertIn("tenant_id", result)
        self.assertEqual(result["role"], "owner")
        # Session token is an fss_ token.
        self.assertTrue(result["session_token"].startswith("fss_"))

    def test_signup_requires_valid_email(self) -> None:
        status, body = _post(self.base, "/v1/auth/signup", {
            "email": "not-an-email", "password": "SecurePass!1",
        })
        self.assertEqual(status, 400)

    def test_signup_requires_password_length(self) -> None:
        status, body = _post(self.base, "/v1/auth/signup", {
            "email": "bob@example.com", "password": "short",
        })
        self.assertEqual(status, 400)

    def test_signin_with_correct_credentials(self) -> None:
        self._signup("carol@example.com", "CorrectHorse!1")
        result = self._signin("carol@example.com", "CorrectHorse!1")
        self.assertIn("session_token", result)

    def test_signin_with_wrong_password_refused(self) -> None:
        self._signup("dave@example.com", "CorrectHorse!1")
        status, body = _post(self.base, "/v1/auth/signin", {
            "email": "dave@example.com", "password": "WrongPassword!1",
        })
        self.assertEqual(status, 401)

    def test_me_requires_auth(self) -> None:
        status, body = _get(self.base, "/v1/me")
        self.assertEqual(status, 401)

    def test_me_returns_account(self) -> None:
        signup = self._signup("eve@example.com", "CorrectHorse!1")
        status, body = _get(self.base, "/v1/me", signup["session_token"])
        self.assertEqual(status, 200)
        self.assertEqual(body["account_id"], signup["account_id"])
        self.assertEqual(body["tenant_id"], signup["tenant_id"])

    def test_owner_sees_self_in_org_members(self) -> None:
        signup = self._signup("frank@example.com", "CorrectHorse!1")
        status, body = _get(self.base, "/v1/org/members", signup["session_token"])
        self.assertEqual(status, 200)
        self.assertEqual(len(body["members"]), 1)
        self.assertEqual(body["members"][0]["role"], "owner")


class TestRoomLifecycle(CloudServiceTestBase):
    """Create room, join via link, poll, leave, close."""

    def test_create_room_returns_shareable_link(self) -> None:
        signup = self._signup("owner@example.com", "CorrectHorse!1")
        room = self._create_room(signup["session_token"], cap=6, name="test-room")
        self.assertIn("room_id", room)
        self.assertIn("link_token", room)
        self.assertIn("shareable_link", room)
        self.assertEqual(room["cap"], 6)
        self.assertEqual(room["state"], "forming")
        self.assertTrue(room["link_token"].startswith("rm_"))

    def test_owner_auto_joined(self) -> None:
        signup = self._signup("owner2@example.com", "CorrectHorse!1")
        room = self._create_room(signup["session_token"], cap=4,
                                 owner_agent_id="owner-agent")
        # Owner can poll immediately.
        status, body = _post(self.base, "/v1/rooms/poll", {
            "room_id": room["room_id"],
            "agent_id": "owner-agent",
        }, signup["session_token"])
        self.assertEqual(status, 200)
        # room.created + room.joined = 2 events.
        self.assertEqual(len(body["events"]), 2)

    def test_four_agents_join_same_link(self) -> None:
        """The core product claim: ONE link, N agents."""
        signup = self._signup("multi-owner@example.com", "CorrectHorse!1")
        room = self._create_room(signup["session_token"], cap=6,
                                 owner_agent_id="multi-owner-agent")
        link_token = room["link_token"]
        room_id = room["room_id"]
        tokens = []
        for i in range(4):
            agent_signup = self._signup(f"agent-{i}@example.com", f"AgentPass-{i}!1")
            tokens.append(agent_signup["session_token"])
            result = self._join_room(
                agent_signup["session_token"], room_id, link_token, f"agent-{i}",
            )
            self.assertEqual(result["status"], "active")
        # Room should now be active (owner + 4 agents > 1).
        status, body = _post(self.base, "/v1/rooms/poll", {
            "room_id": room_id, "agent_id": "agent-0",
        }, tokens[0])
        self.assertEqual(status, 200)
        # Events: room.created, owner.joined, agent-0.joined, ..., agent-3.joined.
        self.assertEqual(len(body["events"]), 6)

    def test_join_without_consent_refused(self) -> None:
        signup = self._signup("noconsent@example.com", "CorrectHorse!1")
        room = self._create_room(signup["session_token"], cap=4,
                                 owner_agent_id="noconsent-owner")
        agent_signup = self._signup("refused@example.com", "AgentPass!1")
        status, body = _post(self.base, "/v1/rooms/join", {
            "room_id": room["room_id"],
            "link_token": room["link_token"],
            "agent_id": "refused-agent",
            "consent": "yes",  # string, not boolean
        }, agent_signup["session_token"])
        self.assertEqual(status, 400)
        self.assertEqual(body["error"]["code"], "consent_required")

    def test_join_with_wrong_link_refused(self) -> None:
        signup = self._signup("wronglink@example.com", "CorrectHorse!1")
        room = self._create_room(signup["session_token"], cap=4,
                                 owner_agent_id="wronglink-owner")
        agent_signup = self._signup("wronglink-agent@example.com", "AgentPass!1")
        status, body = _post(self.base, "/v1/rooms/join", {
            "room_id": room["room_id"],
            "link_token": "frl_wrongtokenvalue",
            "agent_id": "wrong-agent",
            "consent": True,
        }, agent_signup["session_token"])
        self.assertEqual(status, 403)
        self.assertEqual(body["error"]["code"], "invalid_link")

    def test_join_room_full_refused(self) -> None:
        signup = self._signup("full@example.com", "CorrectHorse!1")
        room = self._create_room(signup["session_token"], cap=2,
                                 owner_agent_id="full-owner")  # owner + 1
        link_token = room["link_token"]
        # First agent joins OK.
        a1 = self._signup("full-a1@example.com", "AgentPass!1")
        self._join_room(a1["session_token"], room["room_id"], link_token, "full-a1")
        # Second agent — room is full.
        a2 = self._signup("full-a2@example.com", "AgentPass!1")
        status, body = _post(self.base, "/v1/rooms/join", {
            "room_id": room["room_id"],
            "link_token": link_token,
            "agent_id": "full-a2",
            "consent": True,
        }, a2["session_token"])
        self.assertEqual(status, 409)
        self.assertEqual(body["error"]["code"], "room_full")

    def test_rejoin_idempotent(self) -> None:
        signup = self._signup("rejoin@example.com", "CorrectHorse!1")
        room = self._create_room(signup["session_token"], cap=4,
                                 owner_agent_id="rejoin-owner")
        agent_signup = self._signup("rejoin-agent@example.com", "AgentPass!1")
        link_token = room["link_token"]
        room_id = room["room_id"]
        # Join twice.
        r1 = self._join_room(agent_signup["session_token"], room_id, link_token, "rejoin-agent")
        r2 = self._join_room(agent_signup["session_token"], room_id, link_token, "rejoin-agent")
        self.assertEqual(r1["status"], "active")
        self.assertEqual(r2["status"], "active")

    def test_close_room_refuses_new_joins(self) -> None:
        signup = self._signup("close-owner@example.com", "CorrectHorse!1")
        room = self._create_room(signup["session_token"], cap=4,
                                 owner_agent_id="close-owner-agent")
        # Close the room.
        status, body = _post(self.base, "/v1/rooms/close", {
            "room_id": room["room_id"],
            "caller_agent_id": "close-owner-agent",
        }, signup["session_token"])
        self.assertEqual(status, 200)
        self.assertEqual(body["state"], "closed")
        # New join refused.
        agent_signup = self._signup("late-agent@example.com", "AgentPass!1")
        status, body = _post(self.base, "/v1/rooms/join", {
            "room_id": room["room_id"],
            "link_token": room["link_token"],
            "agent_id": "late-agent",
            "consent": True,
        }, agent_signup["session_token"])
        self.assertIn(status, (409, 410))  # room_closed or link_revoked

    def test_revoke_link_refuses_new_joins(self) -> None:
        signup = self._signup("revoke-owner@example.com", "CorrectHorse!1")
        room = self._create_room(signup["session_token"], cap=4,
                                 owner_agent_id="revoke-owner-agent")
        # Revoke the link.
        status, body = _post(self.base, "/v1/rooms/revoke_link", {
            "room_id": room["room_id"],
            "link_id": room["link_id"],
            "owner_agent_id": "revoke-owner-agent",
        }, signup["session_token"])
        self.assertEqual(status, 200)
        self.assertTrue(body["revoked"])
        # New join refused.
        agent_signup = self._signup("revoked-agent@example.com", "AgentPass!1")
        status, body = _post(self.base, "/v1/rooms/join", {
            "room_id": room["room_id"],
            "link_token": room["link_token"],
            "agent_id": "revoked-agent",
            "consent": True,
        }, agent_signup["session_token"])
        self.assertEqual(status, 410)
        self.assertEqual(body["error"]["code"], "link_revoked")


class TestOrderedDelivery(CloudServiceTestBase):
    """Broadcast reaches all agents in the same order."""

    def setUp(self) -> None:
        super().setUp()
        self.owner = self._signup("delivery-owner@example.com", "CorrectHorse!1")
        self.room = self._create_room(self.owner["session_token"], cap=6,
                                      owner_agent_id="delivery-owner-agent")
        self.link_token = self.room["link_token"]
        self.room_id = self.room["room_id"]
        self.agent_tokens = []
        for i in range(4):
            s = self._signup(f"delivery-agent-{i}@example.com", f"AgentPass-{i}!1")
            self.agent_tokens.append(s["session_token"])
            self._join_room(s["session_token"], self.room_id, self.link_token, f"delivery-agent-{i}")

    def test_broadcast_reaches_all_in_order(self) -> None:
        # Agent 0 sends a broadcast.
        status, body = _post(self.base, "/v1/rooms/send", {
            "room_id": self.room_id,
            "sender_agent_id": "delivery-agent-0",
            "target_spec": "*",
            "payload": {"text": "broadcast-1"},
        }, self.agent_tokens[0])
        self.assertEqual(status, 200)
        seq1 = body["seq"]
        # Agent 1 sends another.
        status, body = _post(self.base, "/v1/rooms/send", {
            "room_id": self.room_id,
            "sender_agent_id": "delivery-agent-1",
            "target_spec": "*",
            "payload": {"text": "broadcast-2"},
        }, self.agent_tokens[1])
        self.assertEqual(status, 200)
        seq2 = body["seq"]
        self.assertGreater(seq2, seq1)
        # Every agent sees both messages in the same order.
        for i in range(4):
            status, poll = _post(self.base, "/v1/rooms/poll", {
                "room_id": self.room_id,
                "agent_id": f"delivery-agent-{i}",
                "after_seq": 0,
            }, self.agent_tokens[i])
            self.assertEqual(status, 200)
            message_events = [e for e in poll["events"] if e["kind"] == "room.message"]
            self.assertGreaterEqual(len(message_events), 2)
            seqs = [e["seq"] for e in message_events]
            self.assertEqual(seqs, sorted(seqs), f"agent-{i} events not in order")

    def test_unicast_reaches_only_target(self) -> None:
        status, body = _post(self.base, "/v1/rooms/send", {
            "room_id": self.room_id,
            "sender_agent_id": "delivery-agent-2",
            "target_spec": "delivery-agent-3",
            "payload": {"text": "private"},
            "exclude_sender": True,
        }, self.agent_tokens[2])
        self.assertEqual(status, 200)
        targets = [r["agent_id"] for r in body["receipts"]]
        self.assertEqual(targets, ["delivery-agent-3"])

    def test_non_member_send_refused(self) -> None:
        stranger = self._signup("delivery-stranger@example.com", "StrangerPass!1")
        status, body = _post(self.base, "/v1/rooms/send", {
            "room_id": self.room_id,
            "sender_agent_id": "stranger",
            "target_spec": "*",
            "payload": {"text": "should be refused"},
        }, stranger["session_token"])
        # 404 (room_not_found) is acceptable — it doesn't reveal the room exists.
        # 403 (member_required) is returned when the caller is in the right tenant.
        self.assertIn(status, (403, 404))

    def test_non_member_poll_refused(self) -> None:
        stranger = self._signup("poll-stranger@example.com", "StrangerPass!1")
        status, body = _post(self.base, "/v1/rooms/poll", {
            "room_id": self.room_id,
            "agent_id": "stranger",
        }, stranger["session_token"])
        self.assertIn(status, (403, 404))

    def test_non_member_event_log_refused(self) -> None:
        stranger = self._signup("log-stranger@example.com", "StrangerPass!1")
        status, body = _post(self.base, "/v1/rooms/event_log", {
            "room_id": self.room_id,
            "agent_id": "stranger",
        }, stranger["session_token"])
        self.assertIn(status, (403, 404))

    def test_event_log_excludes_refused_actions(self) -> None:
        # A stranger tries to send (refused).
        stranger = self._signup("clean-stranger@example.com", "StrangerPass!1")
        _post(self.base, "/v1/rooms/send", {
            "room_id": self.room_id,
            "sender_agent_id": "stranger",
            "target_spec": "*",
            "payload": {"text": "refused"},
        }, stranger["session_token"])
        # The event log should NOT contain the stranger's action.
        status, body = _post(self.base, "/v1/rooms/event_log", {
            "room_id": self.room_id,
            "agent_id": "delivery-agent-0",
        }, self.agent_tokens[0])
        self.assertEqual(status, 200)
        stranger_events = [e for e in body["events"] if e["origin_agent"] == "stranger"]
        self.assertEqual(len(stranger_events), 0,
                         "refused action leaked into event log")


class TestCrossTenantIsolation(CloudServiceTestBase):
    """Tenant B cannot read tenant A's data."""

    def test_cross_tenant_poll_without_link_refused(self) -> None:
        """Tenant B cannot poll tenant A's room without a valid link.

        The link is the cross-tenant capability — without it, the room is
        invisible to other tenants.
        """
        a = self._signup("tenant-a2@example.com", "CorrectHorse!1")
        room = self._create_room(a["session_token"], cap=4,
                                 owner_agent_id="tenant-a-owner")
        b = self._signup("tenant-b2@example.com", "CorrectHorse!1")
        # Tenant B tries to poll without being a member — refused.
        status, body = _post(self.base, "/v1/rooms/poll", {
            "room_id": room["room_id"],
            "agent_id": "tenant-b-agent",
        }, b["session_token"])
        self.assertIn(status, (404, 403))

    def test_cross_tenant_join_with_wrong_link_refused(self) -> None:
        """Tenant B cannot join with a fabricated/garbled link."""
        a = self._signup("tenant-a3@example.com", "CorrectHorse!1")
        room = self._create_room(a["session_token"], cap=4,
                                 owner_agent_id="tenant-a3-owner")
        b = self._signup("tenant-b3@example.com", "CorrectHorse!1")
        status, body = _post(self.base, "/v1/rooms/join", {
            "room_id": room["room_id"],
            "link_token": "frl_fabricated_token_value_here",
            "agent_id": "tenant-b-agent",
            "consent": True,
        }, b["session_token"])
        self.assertEqual(status, 403)
        self.assertEqual(body["error"]["code"], "invalid_link")

    def test_valid_link_allows_cross_tenant_join(self) -> None:
        """A valid link IS the cross-tenant capability — holder can join."""
        a = self._signup("tenant-a4@example.com", "CorrectHorse!1")
        room = self._create_room(a["session_token"], cap=4,
                                 owner_agent_id="tenant-a4-owner")
        b = self._signup("tenant-b4@example.com", "CorrectHorse!1")
        # Tenant B uses the VALID link — join succeeds.
        status, body = _post(self.base, "/v1/rooms/join", {
            "room_id": room["room_id"],
            "link_token": room["link_token"],
            "agent_id": "tenant-b-agent",
            "consent": True,
        }, b["session_token"])
        self.assertEqual(status, 200)
        self.assertEqual(body["status"], "active")


class TestHealthEndpoint(CloudServiceTestBase):
    def test_healthz_returns_ok(self) -> None:
        status, body = _get(self.base, "/healthz")
        self.assertEqual(status, 200)
        self.assertEqual(body["status"], "ok")


if __name__ == "__main__":
    unittest.main()
