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
        finally:
            exc.close()  # release the unread response body / socket
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
        finally:
            exc.close()  # release the unread response body / socket
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
        try:
            return exc.code, exc.read(), dict(exc.headers)
        finally:
            exc.close()  # release the unread response body / socket


class CloudServiceTestBase(unittest.TestCase):
    """Base class that spins up a real HTTP service on a background thread."""

    def setUp(self) -> None:
        self.tmpdir = tempfile.mkdtemp(prefix="weft-test-")
        self.db_path = str(Path(self.tmpdir) / "test.db")
        from http.server import ThreadingHTTPServer
        # Bind an ephemeral port (0) and read back the actual port so no two
        # tests ever contend for a fixed address. Binding happens synchronously
        # in setUp, so a bind failure raises here as a real error instead of
        # silently killing a background thread and timing out every request.
        self._httpd = ThreadingHTTPServer(("127.0.0.1", 0), _CloudHTTPHandler)
        self.port = self._httpd.server_address[1]
        self.base = f"http://127.0.0.1:{self.port}"
        # The service's public origin is the real test server so shareable
        # links point back at it and the /j/<token> endpoint can be exercised
        # end to end against the same process.
        self.service = WeftCloudService(SqliteWalBackend(self.db_path),
                                        origin=self.base)
        _CloudHTTPHandler.service = self.service
        self.server = threading.Thread(
            target=self._httpd.serve_forever, daemon=True,
        )
        self.server.start()

    def tearDown(self) -> None:
        # shutdown() stops serve_forever but does NOT close the listening
        # socket; server_close() is what releases the port. Both must run even
        # when a test failed, or a leaked listener can hijack a later test's
        # connections (they handshake then hang -> TimeoutError).
        if hasattr(self, "_httpd"):
            try:
                self._httpd.shutdown()
            finally:
                self._httpd.server_close()
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
                   consent: bool = True) -> dict:
        status, body = _post(self.base, "/v1/rooms/join", {
            "room_id": room_id,
            "link_token": link_token,
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

    def test_signup_does_not_auto_verify_email(self) -> None:
        # Signup must not self-consume its own email verification: ownership of
        # the address is never proven by signing up. The session is issued
        # immediately (the signup -> create-room flow works) but it does NOT
        # imply a verified address — the response says so and the account row
        # stays unverified with its verification token intact.
        result = self._signup("verifyme@example.com", "SecurePass!1")
        self.assertIs(result["email_verified"], False)
        with self.service.backend.transaction() as tx:
            row = tx.execute(
                "SELECT email_verified, verification_token_hash "
                "FROM cloud_identity_accounts WHERE account_id = ?",
                (result["account_id"],),
            ).fetchone()
        self.assertEqual(row["email_verified"], 0)
        # The token is NOT consumed by signup (verify_email NULLs it on success).
        self.assertIsNotNone(row["verification_token_hash"])
        # The immediate session still works for the signup -> create-room flow.
        status, body = _get(self.base, "/v1/me", result["session_token"])
        self.assertEqual(status, 200)
        self.assertEqual(body["account_id"], result["account_id"])

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
        room = self._create_room(signup["session_token"], cap=4)
        # Owner can poll immediately — identity is the authenticated account.
        status, body = _post(self.base, "/v1/rooms/poll", {
            "room_id": room["room_id"],
        }, signup["session_token"])
        self.assertEqual(status, 200)
        # room.created + room.joined = 2 events.
        self.assertEqual(len(body["events"]), 2)

    def test_lifecycle_payloads_are_visible_to_members(self) -> None:
        """Lifecycle metadata is public to room members; only messages redact."""
        owner = self._signup("lifecycle-owner@example.com", "CorrectHorse!1")
        room = self._create_room(owner["session_token"], cap=3)
        peer = self._signup("lifecycle-peer@example.com", "CorrectHorse!1")
        self._join_room(peer["session_token"], room["room_id"], room["link_token"])

        for token in (owner["session_token"], peer["session_token"]):
            status, poll = _post(self.base, "/v1/rooms/poll", {
                "room_id": room["room_id"],
                "after_seq": 0,
            }, token)
            self.assertEqual(status, 200)
            lifecycle = [
                event for event in poll["events"]
                if event["kind"] in {"room.created", "room.joined"}
            ]
            self.assertEqual(len(lifecycle), 3)
            self.assertTrue(all(event["payload"] for event in lifecycle))
            self.assertNotIn(
                {"redacted": True, "reason": "not_the_addressee"},
                [event["payload"] for event in lifecycle],
            )

            status, log = _post(self.base, "/v1/rooms/event_log", {
                "room_id": room["room_id"],
            }, token)
            self.assertEqual(status, 200)
            lifecycle_log = [
                event for event in log["events"]
                if event["kind"] in {"room.created", "room.joined"}
            ]
            self.assertEqual(
                [event["payload"] for event in lifecycle_log],
                [event["payload"] for event in lifecycle],
            )

    def test_four_agents_join_same_link(self) -> None:
        """The core product claim: ONE link, N agents (each an authenticated account)."""
        signup = self._signup("multi-owner@example.com", "CorrectHorse!1")
        room = self._create_room(signup["session_token"], cap=6)
        link_token = room["link_token"]
        room_id = room["room_id"]
        tokens = []
        for i in range(4):
            agent_signup = self._signup(f"agent-{i}@example.com", f"AgentPass-{i}!1")
            tokens.append(agent_signup["session_token"])
            result = self._join_room(agent_signup["session_token"], room_id, link_token)
            self.assertEqual(result["status"], "active")
        # Room should now be active (owner + 4 agents > 1).
        status, body = _post(self.base, "/v1/rooms/poll", {
            "room_id": room_id,
        }, tokens[0])
        self.assertEqual(status, 200)
        # Events: room.created, owner.joined, agent-0.joined, ..., agent-3.joined.
        self.assertEqual(len(body["events"]), 6)

    def test_join_without_consent_refused(self) -> None:
        signup = self._signup("noconsent@example.com", "CorrectHorse!1")
        room = self._create_room(signup["session_token"], cap=4)
        agent_signup = self._signup("refused@example.com", "AgentPass!1")
        status, body = _post(self.base, "/v1/rooms/join", {
            "room_id": room["room_id"],
            "link_token": room["link_token"],
            "consent": "yes",  # string, not boolean
        }, agent_signup["session_token"])
        self.assertEqual(status, 400)
        self.assertEqual(body["error"]["code"], "consent_required")

    def test_join_with_wrong_link_refused(self) -> None:
        signup = self._signup("wronglink@example.com", "CorrectHorse!1")
        room = self._create_room(signup["session_token"], cap=4)
        agent_signup = self._signup("wronglink-agent@example.com", "AgentPass!1")
        status, body = _post(self.base, "/v1/rooms/join", {
            "room_id": room["room_id"],
            "link_token": "frl_wrongtokenvalue",
            "consent": True,
        }, agent_signup["session_token"])
        self.assertEqual(status, 403)
        self.assertEqual(body["error"]["code"], "invalid_link")

    def test_join_room_full_refused(self) -> None:
        signup = self._signup("full@example.com", "CorrectHorse!1")
        room = self._create_room(signup["session_token"], cap=2)  # owner + 1
        link_token = room["link_token"]
        # First agent joins OK.
        a1 = self._signup("full-a1@example.com", "AgentPass!1")
        self._join_room(a1["session_token"], room["room_id"], link_token)
        # Second agent — room is full.
        a2 = self._signup("full-a2@example.com", "AgentPass!1")
        status, body = _post(self.base, "/v1/rooms/join", {
            "room_id": room["room_id"],
            "link_token": link_token,
            "consent": True,
        }, a2["session_token"])
        self.assertEqual(status, 409)
        self.assertEqual(body["error"]["code"], "room_full")

    def test_rejoin_idempotent(self) -> None:
        signup = self._signup("rejoin@example.com", "CorrectHorse!1")
        room = self._create_room(signup["session_token"], cap=4)
        agent_signup = self._signup("rejoin-agent@example.com", "AgentPass!1")
        link_token = room["link_token"]
        room_id = room["room_id"]
        # Join twice.
        r1 = self._join_room(agent_signup["session_token"], room_id, link_token)
        r2 = self._join_room(agent_signup["session_token"], room_id, link_token)
        self.assertEqual(r1["status"], "active")
        self.assertEqual(r2["status"], "active")

    def test_close_room_refuses_new_joins(self) -> None:
        signup = self._signup("close-owner@example.com", "CorrectHorse!1")
        room = self._create_room(signup["session_token"], cap=4)
        # Close the room.
        status, body = _post(self.base, "/v1/rooms/close", {
            "room_id": room["room_id"],
        }, signup["session_token"])
        self.assertEqual(status, 200)
        self.assertEqual(body["state"], "closed")
        # New join refused.
        agent_signup = self._signup("late-agent@example.com", "AgentPass!1")
        status, body = _post(self.base, "/v1/rooms/join", {
            "room_id": room["room_id"],
            "link_token": room["link_token"],
            "consent": True,
        }, agent_signup["session_token"])
        self.assertIn(status, (409, 410))  # room_closed or link_revoked

    def test_revoke_link_refuses_new_joins(self) -> None:
        signup = self._signup("revoke-owner@example.com", "CorrectHorse!1")
        room = self._create_room(signup["session_token"], cap=4)
        # Revoke the link.
        status, body = _post(self.base, "/v1/rooms/revoke_link", {
            "room_id": room["room_id"],
            "link_id": room["link_id"],
        }, signup["session_token"])
        self.assertEqual(status, 200)
        self.assertTrue(body["revoked"])
        # New join refused.
        agent_signup = self._signup("revoked-agent@example.com", "AgentPass!1")
        status, body = _post(self.base, "/v1/rooms/join", {
            "room_id": room["room_id"],
            "link_token": room["link_token"],
            "consent": True,
        }, agent_signup["session_token"])
        self.assertEqual(status, 410)
        self.assertEqual(body["error"]["code"], "link_revoked")

    def test_owner_can_remove_member_and_reclaim_room_seat(self) -> None:
        owner = self._signup("remove-owner@example.com", "CorrectHorse!1")
        member = self._signup("remove-member@example.com", "AgentPass!1")
        replacement = self._signup("remove-replacement@example.com", "AgentPass!1")
        room = self._create_room(owner["session_token"], cap=2)
        self._join_room(member["session_token"], room["room_id"], room["link_token"])

        status, removed = _post(self.base, "/v1/rooms/remove_member", {
            "room_id": room["room_id"], "member_id": member["account_id"],
        }, owner["session_token"])
        self.assertEqual(status, 200, removed)
        self.assertEqual(removed["status"], "left")

        status, refused = _get(
            self.base,
            f"/v1/rooms/info?room_id={room['room_id']}",
            member["session_token"],
        )
        self.assertEqual(status, 404)
        self.assertEqual(refused["error"]["code"], "room_not_found")

        self._join_room(replacement["session_token"], room["room_id"], room["link_token"])
        status, info = _get(
            self.base,
            f"/v1/rooms/info?room_id={room['room_id']}",
            owner["session_token"],
        )
        self.assertEqual(status, 200, info)
        self.assertEqual(info["member_count"], 2)

    def test_revoke_link_rejects_unknown_cross_room_and_repeated_ids(self) -> None:
        owner = self._signup("revoke-errors-owner@example.com", "CorrectHorse!1")
        other_owner = self._signup("revoke-errors-other@example.com", "CorrectHorse!1")
        room = self._create_room(owner["session_token"], cap=4)
        other_room = self._create_room(other_owner["session_token"], cap=4)

        # An owner must not receive a false success for a fabricated id or a
        # real link belonging to another room. Both cases use one generic
        # response so the control endpoint does not become a link oracle.
        status, unknown = _post(self.base, "/v1/rooms/revoke_link", {
            "room_id": room["room_id"],
            "link_id": "link_unknown_for_revoke_test",
        }, owner["session_token"])
        self.assertEqual(status, 404)
        self.assertEqual(unknown["error"]["code"], "link_not_found")

        status, cross_room = _post(self.base, "/v1/rooms/revoke_link", {
            "room_id": room["room_id"],
            "link_id": other_room["link_id"],
        }, owner["session_token"])
        self.assertEqual(status, 404)
        self.assertEqual(cross_room, unknown)

        # The cross-room attempt did not touch the actual link.
        joiner = self._signup("revoke-errors-joiner@example.com", "AgentPass!1")
        status, body = _post(self.base, "/v1/rooms/join", {
            "room_id": room["room_id"],
            "link_token": room["link_token"],
            "consent": True,
        }, joiner["session_token"])
        self.assertEqual(status, 200, body)

        status, body = _post(self.base, "/v1/rooms/revoke_link", {
            "room_id": room["room_id"],
            "link_id": room["link_id"],
        }, owner["session_token"])
        self.assertEqual(status, 200, body)
        self.assertTrue(body["revoked"])

        # Repeating the control action is not a second success: it is the
        # same generic invalid-link response as the unknown/cross-room cases.
        status, repeated = _post(self.base, "/v1/rooms/revoke_link", {
            "room_id": room["room_id"],
            "link_id": room["link_id"],
        }, owner["session_token"])
        self.assertEqual(status, 404)
        self.assertEqual(repeated, unknown)


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
        self.room = self._create_room(self.owner["session_token"], cap=6)
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
        self.assertEqual(body["join"]["auth_scheme"], "bearer-session-or-agent-key")
        self.assertIn("agent_card", body)
        self.assertEqual(body["agent_card"], f"{self.base}/.well-known/agent-card.json")

    def test_join_descriptor_is_pure_read_no_mutation(self) -> None:
        # Snapshot membership count + event-log length.
        _, info_before = _get(self.base, f"/v1/rooms/info?room_id={self.room_id}",
                              self.owner["session_token"])
        before_members = info_before["member_count"]
        _, poll_before = _post(self.base, "/v1/rooms/poll", {
            "room_id": self.room_id, "after_seq": 0,
        }, self.owner["session_token"])
        before_events = len(poll_before["events"])
        # Fetch the descriptor — this must NOT change anything.
        status, body = self._json_descriptor(self.link_token)
        self.assertEqual(status, 200)
        _, info_after = _get(self.base, f"/v1/rooms/info?room_id={self.room_id}",
                             self.owner["session_token"])
        after_members = info_after["member_count"]
        _, poll_after = _post(self.base, "/v1/rooms/poll", {
            "room_id": self.room_id, "after_seq": 0,
        }, self.owner["session_token"])
        after_events = len(poll_after["events"])
        self.assertEqual(after_members, before_members,
                         "GET /j mutated the membership")
        self.assertEqual(after_events, before_events,
                         "GET /j mutated the event log")
        # And the token STILL works for a real join afterwards — not consumed.
        newcomer = self._signup("discover-newcomer@example.com", "AgentPass!1")
        joined = self._join_room(newcomer["session_token"], self.room_id, self.link_token)
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

    def test_join_descriptor_html_page_teaches_agent_key_flow(self) -> None:
        # The /j/ page is where a human hands a credential to an agent. It must
        # teach the long-lived revocable agent-key flow and warn that the fss_
        # session dies after 24 hours with no renewal, or every connector built
        # from this page silently starts returning 401s a day later (regression
        # for the silent-expiry bug).
        status, raw, _ = _get_url(f"{self.base}/j/{self.link_token}", accept="text/html")
        self.assertEqual(status, 200)
        page = raw.decode("utf-8", errors="replace")
        self.assertIn("POST /v1/agent-keys", page)
        self.assertIn("agk_", page)
        self.assertIn("exactly once", page)
        self.assertIn("24 hours", page)
        self.assertIn("session-only", page)
        self.assertIn("cannot mint, list, or revoke", page)

    def test_join_descriptor_html_page_keeps_tier_disclaimers(self) -> None:
        # The honest disclaimers are the part a future edit could quietly drop.
        # Tier 1 (MCP stdio) reaches a hosted room through the remote bridge —
        # the only stdio path that works — while Tiers 3 and 4 run on a
        # self-hosted coordinator and cannot redeem a hosted cloud room link;
        # both point at the Streamable HTTP call. Consent is an attestation,
        # not proof of a human-approved screen.
        status, raw, _ = _get_url(f"{self.base}/j/{self.link_token}", accept="text/html")
        self.assertEqual(status, 200)
        page = raw.decode("utf-8", errors="replace")
        self.assertIn("This is not a hosted stdio server", page)
        self.assertIn("remote mode", page)
        self.assertIn("no <code>bridge webhook</code> CLI", page)
        self.assertIn("WeftClient.connect()", page)
        self.assertIn("not proof that a human saw and approved", page)
        self.assertIn("never from a request body argument", page)
        self.assertIn("cross-tenant", page)
        self.assertIn("link IS the authorization", page)
        # Tier 1 must teach the stdio bridge (command + args + env, token in
        # env never in argv, PYTHONUTF8=1) — it is the path real hosts use.
        self.assertIn("mcpServers", page)
        self.assertIn("PYTHONUTF8", page)
        self.assertIn("--token-env", page)
        self.assertIn("never in <code>args</code>", page)
        self.assertGreaterEqual(
            page.count("To join this hosted room use the Streamable HTTP call above"),
            2,
        )

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

    def test_join_descriptor_html_page_ships_security_headers(self) -> None:
        # The /j/ human page is a browser-facing HTML response on the live
        # service, so it must carry the full security header block like the
        # web app's pages (HSTS, CSP, frame protection, Referrer-Policy,
        # Permissions-Policy, nosniff).
        status, raw, headers = _get_url(f"{self.base}/j/{self.link_token}", accept="text/html")
        self.assertEqual(status, 200)
        self.assertEqual(headers.get("X-Content-Type-Options"), "nosniff")
        self.assertEqual(headers.get("Referrer-Policy"), "no-referrer")
        self.assertEqual(headers.get("X-Frame-Options"), "DENY")
        self.assertTrue(
            headers.get("Strict-Transport-Security", "").startswith("max-age="),
            "missing HSTS on /j/ HTML page",
        )
        csp = headers.get("Content-Security-Policy", "")
        self.assertIn("default-src 'none'", csp)
        self.assertIn("frame-ancestors 'none'", csp)
        self.assertIn("Permissions-Policy", headers)

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
        self.assertEqual(body["profile"]["name"], "weft.a2a")
        for banned in ("conformance", "a2a-compatible", "conforms to a2a",
                       "a2a protocol conformance", "implements the a2a",
                       "a2a protocol 1.0"):
            self.assertNotIn(banned, text,
                             f"agent card overclaims A2A: {banned!r}")

    def test_connect_tool_creates_room_and_second_agent_joins_via_url(self) -> None:
        # The high-level tool collapses room_create + returns the URL; a second
        # agent joins using ONLY that URL.
        status, body = _post(self.base, "/v1/rooms/connect", {
            "cap": 5,
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
        joined = self._join_room(newcomer["session_token"], descriptor["room_id"], token)
        self.assertEqual(joined["status"], "active")


class TestOrderedDelivery(CloudServiceTestBase):
    """Broadcast reaches all agents in the same order."""

    def setUp(self) -> None:
        super().setUp()
        self.owner = self._signup("delivery-owner@example.com", "CorrectHorse!1")
        self.room = self._create_room(self.owner["session_token"], cap=6)
        self.link_token = self.room["link_token"]
        self.room_id = self.room["room_id"]
        self.agent_tokens = []
        self.agent_ids = []
        for i in range(4):
            s = self._signup(f"delivery-agent-{i}@example.com", f"AgentPass-{i}!1")
            self.agent_tokens.append(s["session_token"])
            self.agent_ids.append(s["account_id"])
            self._join_room(s["session_token"], self.room_id, self.link_token)

    def test_broadcast_reaches_all_in_order(self) -> None:
        # Agent 0 sends a broadcast.
        status, body = _post(self.base, "/v1/rooms/send", {
            "room_id": self.room_id,
            "target_spec": "*",
            "payload": {"text": "broadcast-1"},
        }, self.agent_tokens[0])
        self.assertEqual(status, 200)
        seq1 = body["seq"]
        # Agent 1 sends another.
        status, body = _post(self.base, "/v1/rooms/send", {
            "room_id": self.room_id,
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
            "target_spec": self.agent_ids[3],
            "payload": {"text": "private"},
            "exclude_sender": True,
        }, self.agent_tokens[2])
        self.assertEqual(status, 200)
        targets = [r["agent_id"] for r in body["receipts"]]
        self.assertEqual(targets, [self.agent_ids[3]])

    def test_member_created_group_cannot_divert_exact_unicast(self) -> None:
        victim = self.agent_ids[0]
        diversion = self.agent_ids[1]

        status, group = _post(self.base, "/v1/rooms/groups", {
            "room_id": self.room_id,
            "group_name": victim,
            "action": "add",
            "members": [diversion],
        }, self.agent_tokens[1])
        self.assertEqual(status, 200, group)

        status, result = _post(self.base, "/v1/rooms/send", {
            "room_id": self.room_id,
            "target_spec": victim,
            "payload": {"text": "for victim only"},
        }, self.owner["session_token"])
        self.assertEqual(status, 200, result)
        self.assertEqual([receipt["agent_id"] for receipt in result["receipts"]], [victim])

    def test_unicast_sender_can_read_back_but_other_member_is_redacted(self) -> None:
        secret = "sender-audit-only"
        status, sent = _post(self.base, "/v1/rooms/send", {
            "room_id": self.room_id,
            "target_spec": self.agent_ids[3],
            "payload": {"text": secret},
        }, self.agent_tokens[2])
        self.assertEqual(status, 200)
        self.assertEqual(sent["receipts"][0]["read_status"], "queued")

        status, sender_poll = _post(self.base, "/v1/rooms/poll", {
            "room_id": self.room_id, "after_seq": 0,
        }, self.agent_tokens[2])
        self.assertEqual(status, 200)
        sender_events = [e for e in sender_poll["events"] if e["seq"] == sent["seq"]]
        self.assertEqual(sender_events[0]["payload"]["payload"]["text"], secret)

        status, outsider_poll = _post(self.base, "/v1/rooms/poll", {
            "room_id": self.room_id, "after_seq": 0,
        }, self.agent_tokens[0])
        self.assertEqual(status, 200)
        outsider_events = [e for e in outsider_poll["events"] if e["seq"] == sent["seq"]]
        self.assertEqual(
            outsider_events[0]["payload"],
            {"redacted": True, "reason": "not_the_addressee"},
        )
        self.assertNotIn(secret, json.dumps(outsider_poll))

    def test_non_member_send_refused(self) -> None:
        stranger = self._signup("delivery-stranger@example.com", "StrangerPass!1")
        status, body = _post(self.base, "/v1/rooms/send", {
            "room_id": self.room_id,
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
        }, stranger["session_token"])
        self.assertIn(status, (403, 404))

    def test_non_member_event_log_refused(self) -> None:
        stranger = self._signup("log-stranger@example.com", "StrangerPass!1")
        status, body = _post(self.base, "/v1/rooms/event_log", {
            "room_id": self.room_id,
        }, stranger["session_token"])
        self.assertIn(status, (403, 404))

    def test_event_log_excludes_refused_actions(self) -> None:
        # A stranger tries to send (refused).
        stranger = self._signup("clean-stranger@example.com", "StrangerPass!1")
        _post(self.base, "/v1/rooms/send", {
            "room_id": self.room_id,
            "target_spec": "*",
            "payload": {"text": "refused"},
        }, stranger["session_token"])
        # The event log should NOT contain the stranger's action.
        status, body = _post(self.base, "/v1/rooms/event_log", {
            "room_id": self.room_id,
        }, self.agent_tokens[0])
        self.assertEqual(status, 200)
        stranger_events = [e for e in body["events"] if e["origin_agent"] == stranger["account_id"]]
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
        room = self._create_room(a["session_token"], cap=4)
        b = self._signup("tenant-b2@example.com", "CorrectHorse!1")
        # Tenant B tries to poll without being a member — refused.
        status, body = _post(self.base, "/v1/rooms/poll", {
            "room_id": room["room_id"],
        }, b["session_token"])
        self.assertIn(status, (404, 403))

    def test_cross_tenant_join_with_wrong_link_refused(self) -> None:
        """Tenant B cannot join with a fabricated/garbled link."""
        a = self._signup("tenant-a3@example.com", "CorrectHorse!1")
        room = self._create_room(a["session_token"], cap=4)
        b = self._signup("tenant-b3@example.com", "CorrectHorse!1")
        status, body = _post(self.base, "/v1/rooms/join", {
            "room_id": room["room_id"],
            "link_token": "frl_fabricated_token_value_here",
            "consent": True,
        }, b["session_token"])
        self.assertEqual(status, 403)
        self.assertEqual(body["error"]["code"], "invalid_link")

    def test_valid_link_allows_cross_tenant_join(self) -> None:
        """A valid link IS the cross-tenant capability — holder can join."""
        a = self._signup("tenant-a4@example.com", "CorrectHorse!1")
        room = self._create_room(a["session_token"], cap=4)
        b = self._signup("tenant-b4@example.com", "CorrectHorse!1")
        # Tenant B uses the VALID link — join succeeds.
        status, body = _post(self.base, "/v1/rooms/join", {
            "room_id": room["room_id"],
            "link_token": room["link_token"],
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
        self.room = self._create_room(self.owner["session_token"], cap=8)
        self.room_id = self.room["room_id"]
        self.link_token = self.room["link_token"]
        self.tokens: dict[str, str] = {}
        self.ids: dict[str, str] = {}
        for name in ("agent-0", "agent-1", "agent-2", "agent-3"):
            s = self._signup(f"mk-{name}@example.com", "AgentPass!1")
            self.tokens[name] = s["session_token"]
            self.ids[name] = s["account_id"]
            self._join_room(s["session_token"], self.room_id, self.link_token)

    def _send(self, agent: str, *, kind: str | None, text: str, target: str = "*") -> dict:
        body = {
            "room_id": self.room_id,
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
        body: dict = {"room_id": self.room_id}
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
            "room_id": self.room_id, "seq": f_seqs[-1],
        }, self.tokens["agent-2"])
        self.assertEqual(status, 200)
        status, _ = _post(self.base, "/v1/rooms/ack", {
            "room_id": self.room_id, "seq": u_seqs[-1],
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
            "target_spec": self.ids["agent-2"],
            "message_kind": "result",
            "payload": {"text": "TOP-SECRET-UNICAST"},
        }, self.tokens["agent-1"])
        self.assertEqual(status, 200)
        self.assertEqual([r["agent_id"] for r in body["receipts"]], [self.ids["agent-2"]])

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


class TestRoomWaitRoute(CloudServiceTestBase):
    """POST /v1/rooms/wait — REST parity with the MCP ``room_wait`` tool.

    Every test drives the REAL HTTP route (never the internal function), and
    wait durations are asserted against wall clock so the suite proves "woke
    on the event" rather than "returned when the clock ran out".
    """

    def _wait_long(self, path: str, body: dict, token: str | None = None,
                   timeout: float = 40.0) -> tuple[int, dict]:
        """POST that tolerates a blocking long-poll (larger socket timeout)."""
        data = json.dumps(body).encode("utf-8")
        req = urllib.request.Request(self.base + path, data=data, method="POST")
        req.add_header("Content-Type", "application/json")
        if token:
            req.add_header("Authorization", f"Bearer {token}")
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                return resp.status, json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            payload = {}
            try:
                payload = json.loads(exc.read().decode("utf-8"))
            except Exception:
                pass
            finally:
                exc.close()
            return exc.code, payload

    def _wait_async(self, token: str, body: dict) -> tuple[dict, threading.Thread]:
        holder: dict = {}

        def _run() -> None:
            try:
                holder["resp"] = self._wait_long("/v1/rooms/wait", body, token)
            except Exception as exc:  # pragma: no cover - defensive
                holder["error"] = exc

        thread = threading.Thread(target=_run, daemon=True)
        thread.start()
        return holder, thread

    def _post_wait_raw(self, room_id: str, token: str | None,
                       timeout: float = 40.0) -> tuple[int, bytes]:
        """Raw POST /v1/rooms/wait returning the exact response bytes."""
        data = json.dumps({"room_id": room_id, "timeout_seconds": 1}).encode("utf-8")
        req = urllib.request.Request(self.base + "/v1/rooms/wait", data=data, method="POST")
        req.add_header("Content-Type", "application/json")
        if token:
            req.add_header("Authorization", f"Bearer {token}")
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                return resp.status, resp.read()
        except urllib.error.HTTPError as exc:
            try:
                return exc.code, exc.read()
            finally:
                exc.close()

    def _room_with_two_members(self, prefix: str) -> tuple[dict, dict, dict]:
        owner = self._signup(f"{prefix}-owner@example.com", "CorrectHorse!1")
        room = self._create_room(owner["session_token"], cap=6)
        peer = self._signup(f"{prefix}-peer@example.com", "AgentPass!1")
        self._join_room(peer["session_token"], room["room_id"],
                        room["link_token"])
        return owner, peer, room

    def _head(self, token: str, room_id: str) -> int:
        status, poll = _post(self.base, "/v1/rooms/poll", {
            "room_id": room_id, "after_seq": 0,
        }, token)
        self.assertEqual(status, 200, f"head poll failed: {poll}")
        return poll["cursor_head"]

    def test_blocks_then_wakes_promptly_on_new_event(self) -> None:
        owner, peer, room = self._room_with_two_members("wake")
        head = self._head(owner["session_token"], room["room_id"])

        holder, thread = self._wait_async(
            owner["session_token"],
            {"room_id": room["room_id"], "after_seq": head, "timeout_seconds": 10},
        )
        time.sleep(0.6)  # let the waiter block inside its wait loop
        start = time.monotonic()
        status, sent = _post(self.base, "/v1/rooms/send", {
            "room_id": room["room_id"],
            "target_spec": "*",
            "payload": {"kind": "message", "text": "wake up"},
        }, peer["session_token"])
        self.assertEqual(status, 200, f"send failed: {sent}")
        thread.join(timeout=30)
        self.assertFalse(thread.is_alive(), "wait never returned after the event")
        self.assertNotIn("error", holder, f"wait raised: {holder.get('error')}")
        wait_status, result = holder["resp"]
        self.assertEqual(wait_status, 200)
        elapsed = time.monotonic() - start
        self.assertLess(elapsed, 4.0,
                        f"woke on the clock, not the event ({elapsed:.2f}s vs 10s timeout)")
        self.assertFalse(result["timed_out"])
        texts = [e["payload"]["payload"]["text"] for e in result["events"]
                 if e["kind"] == "room.message"]
        self.assertIn("wake up", texts)

    def test_returns_empty_not_error_at_timeout(self) -> None:
        owner, _peer, room = self._room_with_two_members("timed")
        head = self._head(owner["session_token"], room["room_id"])

        start = time.monotonic()
        status, result = self._wait_long(
            "/v1/rooms/wait",
            {"room_id": room["room_id"], "after_seq": head, "timeout_seconds": 2},
            owner["session_token"],
        )
        elapsed = time.monotonic() - start
        self.assertEqual(status, 200, "a timed-out wait must be a 200, not an error")
        self.assertGreaterEqual(elapsed, 1.5,
                                f"returned early ({elapsed:.2f}s for a 2s timeout)")
        self.assertTrue(result["timed_out"])
        self.assertEqual(result["events"], [], "timeout must return an EMPTY event list")

    def test_requires_auth(self) -> None:
        status, body = _post(self.base, "/v1/rooms/wait", {
            "room_id": "room_any", "timeout_seconds": 1,
        })
        self.assertEqual(status, 401)
        self.assertEqual(body["error"]["code"], "unauthorized")

    def test_non_member_no_oracle_real_vs_fabricated_identical(self) -> None:
        owner, _peer, room = self._room_with_two_members("nooracle")
        outsider = self._signup("nooracle-outsider@example.com", "OutsiderPass!1")

        real_status, real_raw = self._post_wait_raw(room["room_id"],
                                                    outsider["session_token"])
        fake_status, fake_raw = self._post_wait_raw(
            "room_fabricated_0000000000000", outsider["session_token"])
        self.assertEqual(real_status, fake_status,
                         "non-member must get the SAME status for a real and a fabricated room")
        self.assertEqual(real_raw, fake_raw,
                         "non-member must get a BYTE-IDENTICAL body for a real and a fabricated room")
        self.assertEqual(real_status, 404)

    def test_smuggled_identity_arguments_rejected(self) -> None:
        owner, _peer, room = self._room_with_two_members("smuggle")
        for identity_arg in ("agent_id", "sender_agent_id", "caller_agent_id", "tenant_id"):
            with self.subTest(arg=identity_arg):
                status, body = _post(self.base, "/v1/rooms/wait", {
                    "room_id": room["room_id"],
                    identity_arg: "someone-else",
                    "timeout_seconds": 1,
                }, owner["session_token"])
                self.assertEqual(status, 400, f"identity arg {identity_arg} not rejected")
                self.assertEqual(body["error"]["code"], "invalid_argument")
                self.assertIn("not accepted", body["error"]["message"])

    def test_writer_can_write_while_waiter_blocked(self) -> None:
        owner, peer, room = self._room_with_two_members("writest")
        head = self._head(owner["session_token"], room["room_id"])

        holder, thread = self._wait_async(
            owner["session_token"],
            {"room_id": room["room_id"], "after_seq": head, "timeout_seconds": 10},
        )
        time.sleep(0.6)  # let the waiter block inside its wait loop
        send_start = time.monotonic()
        status, sent = _post(self.base, "/v1/rooms/send", {
            "room_id": room["room_id"],
            "target_spec": "*",
            "payload": {"kind": "message", "text": "writer not blocked"},
        }, peer["session_token"])
        send_elapsed = time.monotonic() - send_start
        self.assertEqual(status, 200, f"writer blocked by waiter: {sent}")
        self.assertLess(send_elapsed, 2.0,
                        f"writer took {send_elapsed:.2f}s while a waiter was blocked")
        thread.join(timeout=30)
        self.assertNotIn("error", holder, f"wait raised: {holder.get('error')}")
        wait_status, result = holder["resp"]
        self.assertEqual(wait_status, 200)
        self.assertFalse(result["timed_out"])
        texts = [e["payload"]["payload"]["text"] for e in result["events"]
                 if e["kind"] == "room.message"]
        self.assertIn("writer not blocked", texts)


class TestSignoutNoDeadlock(CloudServiceTestBase):
    """POST /v1/auth/signout must revoke the session in ONE transaction.

    The auditor flagged a nested-writer hazard: ``handle_signout`` opened a
    ``BEGIN IMMEDIATE`` transaction and then called ``sessions.revoke`` inside
    it, which opened a SECOND writer transaction. SQLite has a single writer,
    so the inner ``BEGIN IMMEDIATE`` blocks until the outer commits — but the
    outer cannot commit until the inner returns. Signout wedged for the full
    lock timeout (~15s) and then surfaced as a 500.

    This test drives the real endpoint and asserts the response is prompt and
    that the session really is revoked afterwards.
    """

    def _signout(self, token: str, timeout: float = 25.0) -> tuple[int, dict]:
        data = json.dumps({}).encode("utf-8")
        req = urllib.request.Request(self.base + "/v1/auth/signout", data=data, method="POST")
        req.add_header("Content-Type", "application/json")
        req.add_header("Authorization", f"Bearer {token}")
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                return resp.status, json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            payload = {}
            try:
                payload = json.loads(exc.read().decode("utf-8"))
            except Exception:
                pass
            finally:
                exc.close()
            return exc.code, payload

    def test_signout_revokes_session_promptly(self) -> None:
        signup = self._signup("signout@example.com", "CorrectHorse!1")
        token = signup["session_token"]
        start = time.monotonic()
        status, body = self._signout(token)
        elapsed = time.monotonic() - start
        self.assertEqual(status, 200, f"signout failed: {body}")
        # The SQLite lock timeout is 15s; a wedged (nested-transaction) signout
        # takes ~15s and returns 500. A healthy one is milliseconds.
        self.assertLess(elapsed, 5.0,
                        f"signout took {elapsed:.2f}s — likely a nested-writer deadlock")
        self.assertTrue(body["signed_out"])

    def test_signout_token_no_longer_authenticates(self) -> None:
        signup = self._signup("signout2@example.com", "CorrectHorse!1")
        token = signup["session_token"]
        status, _ = self._signout(token)
        self.assertEqual(status, 200)
        status, body = _get(self.base, "/v1/me", token)
        self.assertEqual(status, 401, f"revoked session still authenticates: {body}")


class TestRoomSendAtomicityAndIdempotency(CloudServiceTestBase):
    """room_send must persist the event AND its delivery receipts atomically.

    The auditor flagged two defects in the old implementation:
      1. The event committed first, then each recipient's receipt was written
         in a SEPARATE transaction — a crash between the two left a visible
         event with partial or zero receipts.
      2. A retried send had no idempotency key, so it DUPLICATED the event.

    Fix: event + receipts commit under ONE transaction, and a caller-supplied
    idempotency key makes a retry a no-op that returns the original result.
    """

    def _room_with_members(self, prefix: str, n_agents: int = 2) -> tuple[dict, list[dict]]:
        owner = self._signup(f"{prefix}-owner@example.com", "CorrectHorse!1")
        room = self._create_room(owner["session_token"], cap=n_agents + 2)
        link_token = room["link_token"]
        room_id = room["room_id"]
        agents = [owner]
        for i in range(n_agents):
            s = self._signup(f"{prefix}-agent-{i}@example.com", f"AgentPass-{i}!1")
            self._join_room(s["session_token"], room_id, link_token)
            agents.append(s)
        return room, agents

    def _event_rows(self, tenant_id: str, room_id: str) -> list[dict]:
        with self.service.backend.transaction() as tx:
            rows = tx.execute(
                "SELECT seq, kind, idempotency_key FROM cloud_room_event_log "
                "WHERE tenant_id = ? AND room_id = ? AND kind = 'room.message' ORDER BY seq",
                (tenant_id, room_id),
            ).fetchall()
        return [dict(r) for r in rows]

    def _outbox_rows(self, tenant_id: str) -> list[dict]:
        with self.service.backend.transaction() as tx:
            rows = tx.execute(
                "SELECT entry_id, recipient, status FROM cloud_outbox "
                "WHERE tenant_id = ? ORDER BY recipient",
                (tenant_id,),
            ).fetchall()
        return [dict(r) for r in rows]

    def test_send_with_receipt_failure_leaves_no_event_no_receipts(self) -> None:
        """A crash between the event write and the receipts leaves NOTHING.

        Monkeypatch the receipt-writer to raise mid-send. The old code had
        already committed the event in its own transaction, so the event
        survived the crash. The fixed code commits event + receipts together,
        so the failure rolls the whole send back.
        """
        import unittest.mock as mock
        room, agents = self._room_with_members("atomic")
        tenant_id = agents[0]["tenant_id"]
        owner_token = agents[0]["session_token"]
        with mock.patch.object(self.service.backend, "enqueue_outbox_in_tx",
                               side_effect=RuntimeError("simulated crash")):
            status, body = _post(self.base, "/v1/rooms/send", {
                "room_id": room["room_id"],
                "target_spec": "*",
                "payload": {"text": "must not persist"},
            }, owner_token)
            # The send failed server-side — never a 200.
            self.assertEqual(status, 500, f"send should fail, got {status}: {body}")
        # ALL-OR-NOTHING: no event row, no receipt row may survive the crash.
        events = self._event_rows(tenant_id, room["room_id"])
        self.assertEqual(events, [], "event persisted without its receipts — not atomic")
        outbox = self._outbox_rows(tenant_id)
        self.assertEqual(outbox, [], "receipt rows persisted for a rolled-back send")

    def test_send_with_idempotency_key_does_not_duplicate(self) -> None:
        """Two sends with the same idempotency key produce exactly one event."""
        room, agents = self._room_with_members("idem")
        tenant_id = agents[0]["tenant_id"]
        owner_token = agents[0]["session_token"]
        first = _post(self.base, "/v1/rooms/send", {
            "room_id": room["room_id"],
            "target_spec": "*",
            "payload": {"text": "once"},
            "idempotency_key": "send-1",
        }, owner_token)
        self.assertEqual(first[0], 200, first[1])
        seq1 = first[1]["seq"]
        second = _post(self.base, "/v1/rooms/send", {
            "room_id": room["room_id"],
            "target_spec": "*",
            "payload": {"text": "once"},
            "idempotency_key": "send-1",
        }, owner_token)
        self.assertEqual(second[0], 200, second[1])
        # Same seq returned for the retry — the event was NOT re-created.
        self.assertEqual(second[1]["seq"], seq1)
        events = self._event_rows(tenant_id, room["room_id"])
        self.assertEqual(len(events), 1, f"duplicate event created: {events}")
        # Receipts are not doubled either.
        outbox = self._outbox_rows(tenant_id)
        expected_receipts = 2  # owner + 2 agents, sender excluded
        self.assertEqual(len(outbox), expected_receipts,
                         f"receipts doubled by retry: {len(outbox)}")

    def test_changed_payload_replay_returns_original_result_without_rate_burn(self) -> None:
        """A lost-response retry must ignore changed input and consume no new budget."""
        room, agents = self._room_with_members("idem-changed")
        tenant_id = agents[0]["tenant_id"]
        owner_token = agents[0]["session_token"]
        first = _post(self.base, "/v1/rooms/send", {
            "room_id": room["room_id"],
            "target_spec": "*",
            "payload": {"text": "original"},
            "idempotency_key": "send-changed-1",
        }, owner_token)
        self.assertEqual(first[0], 200, first[1])
        before = self._outbox_rows(tenant_id)
        second = _post(self.base, "/v1/rooms/send", {
            "room_id": room["room_id"],
            "target_spec": "*",
            "payload": {"text": "tampered-retry"},
            "idempotency_key": "send-changed-1",
        }, owner_token)
        self.assertEqual(second[0], 409, second[1])
        self.assertEqual(second[1]["error"]["code"], "idempotency_conflict")
        self.assertEqual(self._event_rows(tenant_id, room["room_id"]), [
            {"seq": first[1]["seq"], "kind": "room.message", "idempotency_key": "send-changed-1"}
        ])
        self.assertEqual(self._outbox_rows(tenant_id), before)

        with self.service.backend.transaction() as tx:
            rate_row = tx.execute(
                "SELECT count FROM cloud_rate_windows WHERE tenant_id = ? "
                "AND room_id = ? AND limit_key = 'messages'",
                (tenant_id, room["room_id"]),
            ).fetchone()
        self.assertEqual(rate_row["count"], 1)

    def test_malformed_short_link_is_a_controlled_client_error(self) -> None:
        signup = self._signup("short-link-owner@example.com", "CorrectHorse!1")
        room = self._create_room(signup["session_token"], cap=4)
        joiner = self._signup("short-link-joiner@example.com", "AgentPass!1")
        status, body = _post(self.base, "/v1/rooms/join", {
            "room_id": room["room_id"],
            "link_token": "rm_short",
            "consent": True,
        }, joiner["session_token"])
        self.assertEqual(status, 403)
        self.assertEqual(body["error"]["code"], "invalid_link")

    def test_distinct_idempotency_keys_create_distinct_events(self) -> None:
        room, agents = self._room_with_members("idem2")
        tenant_id = agents[0]["tenant_id"]
        owner_token = agents[0]["session_token"]
        for key in ("a", "b"):
            status, body = _post(self.base, "/v1/rooms/send", {
                "room_id": room["room_id"],
                "target_spec": "*",
                "payload": {"text": key},
                "idempotency_key": key,
            }, owner_token)
            self.assertEqual(status, 200, body)
        events = self._event_rows(tenant_id, room["room_id"])
        self.assertEqual(len(events), 2)


class TestRoomSendIdempotencyKeyValidation(CloudServiceTestBase):
    """The idempotency_key field is validated at the request boundary.

    A malformed idempotency_key must be the caller's error (400
    ``invalid_argument``) — NEVER a 500. Reproduced on production: a list or
    dict key crashed the handler with an unhashable/unsupported bind type
    (``TypeError`` / sqlite ``ProgrammingError``) that surfaced as 500, an
    integer or bool was silently accepted (type confusion), and an unbounded
    string was stored (storage vector). The key must be a non-empty STRING of
    bounded length, and a null/absent key keeps the current "no idempotency"
    behaviour.
    """

    MAX_LEN = 256

    def _reject(self, key: Any) -> tuple[int, dict]:
        counter = getattr(self, "_key_counter", 0)
        self._key_counter = counter + 1
        acct = self._signup(f"idemval-{counter}@example.com", "CorrectHorse!1")
        room = self._create_room(acct["session_token"], cap=4)
        status, body = _post(self.base, "/v1/rooms/send", {
            "room_id": room["room_id"],
            "target_spec": "*",
            "payload": {"text": "x"},
            "idempotency_key": key,
        }, acct["session_token"])
        return status, body

    def _assert_rejected_400(self, key: Any) -> None:
        status, body = self._reject(key)
        self.assertEqual(status, 400, f"expected 400 for key {key!r}, got {status}: {body}")
        self.assertEqual(body["error"]["code"], "invalid_argument",
                         f"expected invalid_argument for key {key!r}: {body}")
        self.assertNotEqual(body["error"]["code"], "internal_error",
                            f"key {key!r} must never be a 500: {body}")

    def test_non_string_types_are_rejected_never_500(self) -> None:
        # Each of list/dict/int/bool/float previously either 500'd (list, dict)
        # or was silently accepted with type confusion (int, bool).
        for key in (["a", "b"], {"k": "v"}, 12345, True, 1.5):
            self._assert_rejected_400(key)

    def test_empty_and_whitespace_only_strings_are_rejected(self) -> None:
        for key in ("", "   ", "\t\n"):
            self._assert_rejected_400(key)

    def test_over_length_key_is_rejected_but_boundary_is_accepted(self) -> None:
        self._assert_rejected_400("k" * (self.MAX_LEN + 1))
        # A key exactly at the limit is still valid.
        status, body = self._reject("k" * self.MAX_LEN)
        self.assertEqual(status, 200, f"max-length key must be accepted: {body}")

    def test_absent_or_null_key_keeps_no_idempotency_behaviour(self) -> None:
        room = self._create_room(self._signup("idemval-absent@example.com", "CorrectHorse!1")["session_token"], cap=4)
        token = self._signin("idemval-absent@example.com", "CorrectHorse!1")["session_token"]
        seqs = []
        for body in ({"room_id": room["room_id"], "target_spec": "*", "payload": {"text": "a"}},
                     {"room_id": room["room_id"], "target_spec": "*", "payload": {"text": "b"},
                      "idempotency_key": None}):
            status, resp = _post(self.base, "/v1/rooms/send", body, token)
            self.assertEqual(status, 200, resp)
            seqs.append(resp["seq"])
        # No dedup: each send appended its own event.
        self.assertEqual(len(set(seqs)), 2, f"absent/null keys must not dedupe: {seqs}")

    def test_valid_key_reused_returns_same_seq_and_no_duplicate_event(self) -> None:
        acct = self._signup("idemval-reuse@example.com", "CorrectHorse!1")
        room = self._create_room(acct["session_token"], cap=4)
        token = acct["session_token"]
        tenant_id = acct["tenant_id"]
        status, first = _post(self.base, "/v1/rooms/send", {
            "room_id": room["room_id"], "target_spec": "*", "payload": {"text": "once"},
            "idempotency_key": "abc-123",
        }, token)
        self.assertEqual(status, 200, first)
        status, second = _post(self.base, "/v1/rooms/send", {
            "room_id": room["room_id"], "target_spec": "*", "payload": {"text": "once"},
            "idempotency_key": "abc-123",
        }, token)
        self.assertEqual(status, 200, second)
        self.assertEqual(second["seq"], first["seq"],
                         "same key must return the SAME seq (retry-dedup must not regress)")
        with self.service.backend.transaction() as tx:
            rows = tx.execute(
                "SELECT COUNT(*) AS n FROM cloud_room_event_log "
                "WHERE tenant_id = ? AND room_id = ? AND kind = 'room.message'",
                (tenant_id, room["room_id"]),
            ).fetchone()
        self.assertEqual(rows["n"], 1, "same key twice must not create a duplicate event")

    def test_uuid_and_idem_prefix_keys_still_pass(self) -> None:
        import uuid
        for key in (str(uuid.uuid4()), f"idem_{uuid.uuid4().hex}"):
            status, body = self._reject(key)
            self.assertEqual(status, 200, f"valid key {key[:20]}... rejected: {body}")

    def test_error_never_echoes_the_raw_key_value(self) -> None:
        secret_key = "k" * 300  # over-length
        status, body = self._reject(secret_key)
        self.assertEqual(status, 400, body)
        rendered = json.dumps(body)
        self.assertNotIn(secret_key, rendered,
                         "the raw idempotency_key must not be echoed into the error body")


if __name__ == "__main__":
    unittest.main()
