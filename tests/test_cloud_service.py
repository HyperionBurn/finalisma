"""Cloud service integration tests — the product claim, end-to-end.

Drives the REAL ``WeftCloudService`` over REAL HTTP (via a
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

from weft_cloud.service import WeftCloudService, _CloudHTTPHandler
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


def _get_url(url: str, accept: str | None = None) -> tuple[int, bytes, dict]:
    """GET a full URL returning (status, raw body bytes, headers).

    Used for the /j/<token> and agent-card endpoints where the response may be
    HTML or JSON and the Accept header selects the audience.
    """
    req = urllib.request.Request(url, method="GET")
    if accept:
        req.add_header("Accept", accept)
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            return resp.status, resp.read(), dict(resp.headers)
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read(), dict(exc.headers)


class CloudServiceTestBase(unittest.TestCase):
    """Base class that spins up a real HTTP service on a background thread."""

    def setUp(self) -> None:
        self.tmpdir = tempfile.mkdtemp(prefix="weft-test-")
        self.db_path = str(Path(self.tmpdir) / "test.db")
        self.port = 18800 + (hash(self.tmpdir) % 1000)
        self.base = f"http://127.0.0.1:{self.port}"
        # The service's public origin is the real test server so shareable
        # links point back at it and the /j/<token> endpoint can be exercised
        # end to end against the same process.
        self.service = WeftCloudService(SqliteWalBackend(self.db_path),
                                        origin=self.base)
        self.server = threading.Thread(
            target=self._serve, daemon=True,
        )
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


class TestJoinDescriptorAndAgentCard(CloudServiceTestBase):
    """Self-describing links: /j/<token> descriptor + well-known agent card.

    The core discovery primitive: a shareable link is an absolute URL an agent
    can fetch (Accept: application/json) to learn the room, the join endpoint,
    the auth scheme, and where the agent card lives — no human copy-paste of
    tribal knowledge required.
    """

    def setUp(self) -> None:
        super().setUp()
        self.owner = self._signup("discover-owner@example.com", "CorrectHorse!1")
        self.room = self._create_room(self.owner["session_token"], cap=6,
                                      owner_agent_id="discover-owner-agent")
        self.link_token = self.room["link_token"]
        self.room_id = self.room["room_id"]

    def _json_descriptor(self, token: str) -> tuple[int, dict]:
        status, raw, _ = _get_url(f"{self.base}/j/{token}", accept="application/json")
        payload = {}
        try:
            payload = json.loads(raw.decode("utf-8"))
        except Exception:
            pass
        return status, payload

    def test_create_room_shareable_link_is_absolute_url_with_token(self) -> None:
        # room_create returns shareable_link as an absolute URL containing the
        # token, AND still returns link_token unchanged (rm_ prefix).
        self.assertTrue(self.room["shareable_link"].startswith(("http://", "https://")),
                        f"shareable_link not absolute: {self.room['shareable_link']}")
        self.assertTrue(self.room["shareable_link"].startswith(self.base))
        self.assertTrue(self.room["shareable_link"].endswith(f"/j/{self.link_token}"))
        self.assertTrue(self.room["link_token"].startswith("rm_"))

    def test_join_descriptor_json_matches_room_id(self) -> None:
        status, body = self._json_descriptor(self.link_token)
        self.assertEqual(status, 200)
        self.assertEqual(body["room_id"], self.room_id)
        self.assertEqual(body["join"]["request"]["room_id"], self.room_id)
        self.assertTrue(body["join"]["endpoint"].startswith(self.base))
        self.assertEqual(body["join"]["method"], "POST")
        self.assertIn("agent_card", body)
        self.assertEqual(body["agent_card"], f"{self.base}/.well-known/agent-card.json")

    def test_join_descriptor_is_pure_read_no_mutation(self) -> None:
        # Snapshot membership count + event-log length.
        _, info_before = _get(self.base, f"/v1/rooms/info?room_id={self.room_id}&agent_id=discover-owner-agent",
                              self.owner["session_token"])
        before_members = info_before["member_count"]
        _, poll_before = _post(self.base, "/v1/rooms/poll", {
            "room_id": self.room_id, "agent_id": "discover-owner-agent", "after_seq": 0,
        }, self.owner["session_token"])
        before_events = len(poll_before["events"])
        # Fetch the descriptor — this must NOT change anything.
        status, body = self._json_descriptor(self.link_token)
        self.assertEqual(status, 200)
        _, info_after = _get(self.base, f"/v1/rooms/info?room_id={self.room_id}&agent_id=discover-owner-agent",
                             self.owner["session_token"])
        after_members = info_after["member_count"]
        _, poll_after = _post(self.base, "/v1/rooms/poll", {
            "room_id": self.room_id, "agent_id": "discover-owner-agent", "after_seq": 0,
        }, self.owner["session_token"])
        after_events = len(poll_after["events"])
        self.assertEqual(after_members, before_members,
                         "GET /j mutated the membership")
        self.assertEqual(after_events, before_events,
                         "GET /j mutated the event log")
        # And the token STILL works for a real join afterwards — not consumed.
        newcomer = self._signup("discover-newcomer@example.com", "AgentPass!1")
        joined = self._join_room(newcomer["session_token"], self.room_id,
                                 self.link_token, "discover-newcomer")
        self.assertEqual(joined["status"], "active")

    def test_join_descriptor_bad_token_is_no_oracle(self) -> None:
        # A malformed token and a well-formed-but-unknown token must return the
        # SAME response shape — no oracle distinguishing them.
        malformed_status, malformed_raw, _ = _get_url(f"{self.base}/j/garbage",
                                                     accept="application/json")
        unknown_status, unknown_raw, _ = _get_url(
            f"{self.base}/j/rm_{'a' * 43}", accept="application/json")
        self.assertEqual(malformed_status, unknown_status)
        self.assertEqual(malformed_raw, unknown_raw)
        self.assertEqual(malformed_status, 404)

    def test_join_descriptor_does_not_leak(self) -> None:
        # The descriptor reveals only what a joining agent strictly needs: no
        # org identity, member emails, or event log to an unauthenticated fetch.
        status, body = self._json_descriptor(self.link_token)
        self.assertEqual(status, 200)
        text = json.dumps(body).lower()
        for banned in ("email", "tenant", "member", "event", "password", "secret"):
            self.assertNotIn(banned, text,
                             f"join descriptor leaked sensitive data: {banned!r}")

    def test_join_descriptor_html_page_for_humans(self) -> None:
        # A human browser gets a readable page reusing the connect-page copy.
        status, raw, _ = _get_url(f"{self.base}/j/{self.link_token}", accept="text/html")
        self.assertEqual(status, 200)
        page = raw.decode("utf-8", errors="replace")
        self.assertIn("Connect an agent", page)
        self.assertIn("Tier 1", page)
        self.assertIn(self.link_token, page)
        self.assertIn(self.room_id, page)

    def test_join_descriptor_html_documents_a_working_join(self) -> None:
        # The /j/ human page reuses the connect-page copy, so it must also
        # document the auth header, consent, and the join endpoint — the
        # elements a copy-paste needs to succeed (regression for the bug).
        status, raw, _ = _get_url(f"{self.base}/j/{self.link_token}", accept="text/html")
        self.assertEqual(status, 200)
        page = raw.decode("utf-8", errors="replace")
        self.assertIn("POST /v1/rooms/join", page)
        self.assertIn("Authorization: Bearer", page)
        self.assertIn("consent: true", page)
        self.assertIn("/v1/auth/signup", page)
        self.assertIn("/v1/auth/signin", page)
        self.assertIn("cross-tenant", page)
        self.assertIn("not proof that a human saw and approved", page)

    def test_agent_card_is_public_and_contains_no_a2a_conformance_claim(self) -> None:
        # Unauthenticated, valid JSON, no secrets, no A2A conformance claim.
        status, raw, headers = _get_url(f"{self.base}/.well-known/agent-card.json")
        self.assertEqual(status, 200)
        self.assertIn("application/json", headers.get("Content-Type", ""))
        body = json.loads(raw.decode("utf-8"))
        text = json.dumps(body).lower()
        # No secret-bearing values, no emails.
        for banned in ("fst_", "rm_", "fss_", "secret", "password", "@"):
            self.assertNotIn(banned, text,
                             f"agent card leaked a secret-ish value: {banned!r}")
        # Names OUR own profile — and does NOT claim A2A conformance.
        self.assertEqual(body["profile"]["name"], "finalisma.a2a")
        for banned in ("conformance", "a2a-compatible", "conforms to a2a",
                       "a2a protocol conformance", "implements the a2a",
                       "a2a protocol 1.0"):
            self.assertNotIn(banned, text,
                             f"agent card overclaims A2A: {banned!r}")

    def test_connect_tool_creates_room_and_second_agent_joins_via_url(self) -> None:
        # The high-level tool collapses room_create + returns the URL; a second
        # agent joins using ONLY that URL.
        status, body = _post(self.base, "/v1/rooms/connect", {
            "cap": 5, "owner_agent_id": "connect-owner",
        }, self.owner["session_token"])
        self.assertEqual(status, 201)
        shareable = body["shareable_link"]
        self.assertTrue(shareable.startswith("http://"))
        token = shareable.rsplit("/", 1)[1]
        self.assertTrue(token.startswith("rm_"))
        self.assertEqual(token, body["link_token"])
        # Second agent fetches the descriptor from the URL, then joins.
        newcomer = self._signup("connect-newcomer@example.com", "AgentPass!1")
        fetch_status, raw, _ = _get_url(shareable, accept="application/json")
        self.assertEqual(fetch_status, 200)
        descriptor = json.loads(raw.decode("utf-8"))
        joined = self._join_room(newcomer["session_token"], descriptor["room_id"],
                                 token, "connect-newcomer")
        self.assertEqual(joined["status"], "active")


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


class TestRoomMessageKind(CloudServiceTestBase):
    """Sender-settable message_kind + message_kinds filter (cloud HTTP API)."""

    def setUp(self) -> None:
        super().setUp()
        self.owner = self._signup("mk-owner@example.com", "CorrectHorse!1")
        self.room = self._create_room(self.owner["session_token"], cap=8,
                                      owner_agent_id="mk-owner-agent")
        self.room_id = self.room["room_id"]
        self.link_token = self.room["link_token"]
        self.tokens: dict[str, str] = {}
        for name in ("agent-0", "agent-1", "agent-2", "agent-3"):
            s = self._signup(f"mk-{name}@example.com", "AgentPass!1")
            self.tokens[name] = s["session_token"]
            self._join_room(s["session_token"], self.room_id, self.link_token, name)

    def _send(self, agent: str, *, kind: str | None, text: str, target: str = "*") -> dict:
        body = {
            "room_id": self.room_id,
            "sender_agent_id": agent,
            "target_spec": target,
            "payload": {"text": text},
        }
        if kind is not None:
            body["message_kind"] = kind
        status, result = _post(self.base, "/v1/rooms/send", body, self.tokens[agent])
        self.assertEqual(status, 200, f"send failed: {result}")
        return result

    def _poll(self, agent: str, after_seq: int | None = 0,
              kinds: list[str] | None = None) -> dict:
        body: dict = {"room_id": self.room_id, "agent_id": agent}
        if after_seq is not None:
            body["after_seq"] = after_seq
        if kinds is not None:
            body["message_kinds"] = kinds
        status, result = _post(self.base, "/v1/rooms/poll", body, self.tokens[agent])
        self.assertEqual(status, 200, f"poll failed: {result}")
        return result

    @staticmethod
    def _messages(result: dict) -> list[dict]:
        return [e for e in result["events"] if e["kind"] == "room.message"]

    def test_send_without_message_kind_is_unchanged_and_visible_unfiltered(self) -> None:
        self._send("agent-0", kind=None, text="plain")
        result = self._poll("agent-2")
        plain = [m for m in self._messages(result)
                 if m["payload"]["payload"].get("text") == "plain"]
        self.assertEqual(len(plain), 1, "unfiltered poll must still see unlabelled sends")
        self.assertIsNone(plain[0]["message_kind"])

    def test_result_visible_when_filtering_on_result_absent_when_filtering_on_status(self) -> None:
        self._send("agent-0", kind="result", text="r1")
        on_result = self._poll("agent-2", kinds=["result"])
        r_events = self._messages(on_result)
        self.assertEqual(len(r_events), 1)
        self.assertEqual(r_events[0]["message_kind"], "result")
        self.assertEqual(r_events[0]["payload"]["payload"]["text"], "r1")
        # The SAME send is absent when filtering on a different kind.
        on_status = self._poll("agent-2", kinds=["status"])
        self.assertEqual(len(self._messages(on_status)), 0)

    def test_system_events_never_match_a_kind_filter(self) -> None:
        self._send("agent-0", kind="result", text="r1")
        result = self._poll("agent-2", kinds=["result"])
        self.assertGreater(len(result["events"]), 0)
        for e in result["events"]:
            if e["kind"] == "room.message":
                self.assertEqual(e["message_kind"], "result")
            else:
                self.assertIsNone(e["message_kind"],
                                  "system events have no message_kind and must not match a filter")

    def test_filtering_and_unfiltered_callers_agree_and_advance_without_gaps_or_repeats(self) -> None:
        self._send("agent-0", kind="status", text="s1")
        self._send("agent-1", kind="result", text="r1")
        self._send("agent-0", kind="status", text="s2")
        self._send("agent-1", kind="result", text="r2")

        filtered = self._poll("agent-2", after_seq=0, kinds=["result"])
        unfiltered = self._poll("agent-3", after_seq=0)

        f_seqs = [e["seq"] for e in self._messages(filtered)]
        u_msgs = self._messages(unfiltered)
        u_seqs = [e["seq"] for e in u_msgs]
        u_result_seqs = [e["seq"] for e in u_msgs if e["message_kind"] == "result"]

        # Ordering agreement: the filtering caller sees exactly the result
        # events, in the same relative order as the unfiltered caller.
        self.assertEqual(f_seqs, u_result_seqs)
        self.assertEqual(u_seqs, sorted(u_seqs), "unfiltered events must be in ascending seq")
        # cursor_head is the FULL stream head for both — never the filtered subset.
        self.assertEqual(filtered["cursor_head"], unfiltered["cursor_head"])
        self.assertEqual(filtered["cursor_head"], max(u_seqs))
        # next_seq is the resume cursor for paging the filtered view.
        self.assertEqual(filtered["next_seq"], f_seqs[-1] + 1)

        # Both callers consume what they saw and ack their cursor.
        status, _ = _post(self.base, "/v1/rooms/ack", {
            "room_id": self.room_id, "agent_id": "agent-2", "seq": f_seqs[-1],
        }, self.tokens["agent-2"])
        self.assertEqual(status, 200)
        status, _ = _post(self.base, "/v1/rooms/ack", {
            "room_id": self.room_id, "agent_id": "agent-3", "seq": u_seqs[-1],
        }, self.tokens["agent-3"])
        self.assertEqual(status, 200)

        # New interleaved messages after both cursors.
        self._send("agent-0", kind="status", text="s3")
        self._send("agent-1", kind="result", text="r3")

        # Filtering caller: only the new result, no repeat of r2, no gap.
        filtered_again = self._poll("agent-2", after_seq=None, kinds=["result"])
        f2_seqs = [e["seq"] for e in self._messages(filtered_again)]
        self.assertEqual(f2_seqs, [f_seqs[-1] + 2],
                         "filtered resume must return exactly the one new result (no gaps, no repeats)")
        self.assertGreater(f2_seqs[0], f_seqs[-1])

        # Unfiltered caller: both new messages, ascending, no gap, no repeat.
        unfiltered_again = self._poll("agent-3", after_seq=None)
        u2_seqs = [e["seq"] for e in self._messages(unfiltered_again)]
        self.assertEqual(u2_seqs, sorted(u2_seqs))
        self.assertEqual(len(u2_seqs), 2, "unfiltered resume must see both new messages")
        self.assertGreater(u2_seqs[0], u_seqs[-1])

    def test_invalid_message_kind_rejected_with_clear_error(self) -> None:
        for bad in ("UPPER", "x" * 33, "has spaces", "has.dot", ""):
            status, body = _post(self.base, "/v1/rooms/send", {
                "room_id": self.room_id,
                "sender_agent_id": "agent-0",
                "target_spec": "*",
                "message_kind": bad,
                "payload": {"text": "bad"},
            }, self.tokens["agent-0"])
            self.assertEqual(status, 400, f"message_kind={bad!r} must be rejected")
            self.assertEqual(body["error"]["code"], "invalid_argument")
            self.assertIn("message_kind", body["error"]["message"])

    def test_message_kinds_filter_rejects_non_list_and_bad_entries(self) -> None:
        for bad in ("result", 42, ["result", "BAD"], [""]):
            status, body = _post(self.base, "/v1/rooms/poll", {
                "room_id": self.room_id,
                "agent_id": "agent-2",
                "message_kinds": bad,
            }, self.tokens["agent-2"])
            self.assertEqual(status, 400, f"message_kinds={bad!r} must be rejected")
            self.assertEqual(body["error"]["code"], "invalid_argument")
            self.assertIn("message_kinds", body["error"]["message"])

    def test_filtering_never_leaks_a_unicast_body(self) -> None:
        """Filtering is not a way around confidentiality: a non-addressee
        filtering on the message_kind sees the redacted envelope, never the body."""
        status, body = _post(self.base, "/v1/rooms/send", {
            "room_id": self.room_id,
            "sender_agent_id": "agent-1",
            "target_spec": "agent-2",
            "message_kind": "result",
            "payload": {"text": "TOP-SECRET-UNICAST"},
        }, self.tokens["agent-1"])
        self.assertEqual(status, 200)
        self.assertEqual([r["agent_id"] for r in body["receipts"]], ["agent-2"])

        # The addressee sees the body when filtering on the same kind.
        addressee = self._poll("agent-2", kinds=["result"])
        found = [e for e in self._messages(addressee)
                 if e["payload"]["payload"].get("text") == "TOP-SECRET-UNICAST"]
        self.assertEqual(len(found), 1, "addressee must receive the body")

        # A non-addressee filtering on the same kind sees the EVENT (redacted
        # envelope, seq view stays consistent) but NEVER the body.
        outsider = self._poll("agent-3", kinds=["result"])
        leaked = [e for e in outsider["events"]
                  if e["kind"] == "room.message" and e["message_kind"] == "result"]
        self.assertEqual(len(leaked), 1,
                         "non-addressee sees the event row so its seq view stays consistent")
        self.assertEqual(leaked[0]["payload"],
                         {"redacted": True, "reason": "not_the_addressee"})
        self.assertNotIn("TOP-SECRET-UNICAST", json.dumps(outsider),
                         "filtering must never expose a unicast body to a non-addressee")


if __name__ == "__main__":
    unittest.main()
