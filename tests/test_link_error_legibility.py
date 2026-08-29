"""MPAI-112: a link failure must tell the reader what to DO, without becoming
an oracle.

Two distinct properties, one per class:

* LinkErrorUniformityTests - on ``room_join``, a MALFORMED token and a
  WELL-FORMED-BUT-UNKNOWN token must be indistinguishable in code, message,
  HTTP status and error-envelope shape, and an unknown token must give the
  same answer whether or not the ``room_id`` names a real room. This is the
  guard that stops a future "helpful" split into ``no_such_link`` vs
  ``wrong_room`` (or ``room_not_found``), which would let anyone probe which
  room ids exist by submitting a guessed id with a garbage token. Both
  branches are pre-auth and touch no member tables, so the structural
  property is what carries the guarantee - there is deliberately NO
  wall-clock timing assertion here (a flaky timing test gets skipped, and
  then the guard is gone).

* TransportErrorDiagnosisTests - a client pointed at a dead / wrong endpoint
  must say so precisely ("could not reach ... check the base URL"), keep the
  code ``transport_error``, and never be mapped to ``invalid_link``. Config
  failure is not user-supplied and is safe to describe exactly; conflating it
  with a bad token is what cost the floor seven agents' time.

Per the Option B ruling on MPAI-112: ``link_revoked`` / ``link_expired`` are
NOT collapsed - they are reached only by a caller who already holds the real
link secret and a valid actor token, so they are diagnostic value for a
legitimate holder, not a leak. This file must not assert otherwise.
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
from weft_sdk.client import WeftClient, WeftError

# A well-formed link token the store has simply never seen: right shape, right
# length band for _token_hash (16..512 chars), just absent.
UNKNOWN_TOKEN = "rm_" + "A" * 44
# Too short for _token_hash - rejected before any lookup.
MALFORMED_TOKEN = "short"


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


class LinkErrorUniformityTests(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.mkdtemp(prefix="weft-linkerr-")
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

        self.owner = self._signup("owner@acme.test")
        self.stranger = self._signup("stranger@other-corp.test")
        self.room = self._create_room()

    def tearDown(self):
        try:
            self.httpd.shutdown()
        finally:
            self.httpd.server_close()
        self.service.backend.close()
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def _signup(self, email):
        status, response = _request(
            self.base, "POST", "/v1/auth/signup",
            body={"email": email, "password": "CorrectHorse!1"},
        )
        self.assertEqual(status, HTTPStatus.CREATED, response)
        return response

    def _create_room(self, name="Room"):
        status, response = _request(
            self.base, "POST", "/v1/rooms/create",
            token=self.owner["session_token"], body={"cap": 6, "name": name},
        )
        self.assertEqual(status, HTTPStatus.CREATED, response)
        return response

    def _join_attempt(self, *, room_id, link_token):
        return _request(
            self.base, "POST", "/v1/rooms/join",
            token=self.stranger["session_token"],
            body={"room_id": room_id, "link_token": link_token, "consent": True},
        )

    # ---- uniformity: malformed vs well-formed-unknown ---------------------
    def test_malformed_and_unknown_link_are_indistinguishable(self):
        """A malformed token and an unknown-but-well-formed token must produce
        byte-identical refusals. If they ever diverge, someone has built a way
        to tell 'this token never existed' from 'this token had the wrong
        shape' - the first step toward a per-token oracle. Structural equality
        only (code, message, status, envelope keys); no timing assertion.
        """
        s1, b1 = self._join_attempt(room_id=self.room["room_id"], link_token=MALFORMED_TOKEN)
        s2, b2 = self._join_attempt(room_id=self.room["room_id"], link_token=UNKNOWN_TOKEN)

        self.assertEqual(s1, s2, (b1, b2))
        self.assertEqual(b1["error"]["code"], b2["error"]["code"])
        self.assertEqual(b1["error"]["code"], "invalid_link")
        self.assertEqual(b1["error"]["message"], b2["error"]["message"])
        self.assertEqual(sorted(b1["error"].keys()), sorted(b2["error"].keys()))

    # ---- actionability (Part 1) -----------------------------------------
    def test_invalid_link_message_says_what_to_do(self):
        """The one uniform message must still be useful: it has to point at the
        two things a stuck joiner can actually check - that the link is
        complete, and that they are talking to the Weft service that issued
        it (the wrong-endpoint case that burned seven agents). RED until the
        Part 1 copy lands.
        """
        _, body = self._join_attempt(room_id=self.room["room_id"], link_token=UNKNOWN_TOKEN)
        message = body["error"]["message"].lower()
        self.assertIn("invalid or expired", message)
        self.assertIn("whole link", message)
        self.assertIn("same weft service", message)

    # ---- anti-oracle: unknown token, real vs fabricated room ------------
    def test_unknown_link_does_not_reveal_whether_room_exists(self):
        """An unknown token against a real room_id and against a fabricated
        room_id must give the same refusal. Diverging here (e.g. room_not_found
        for the fake, invalid_link for the real) turns room_join into a
        room-id enumeration oracle.
        """
        fabricated = "room_" + "0" * 32
        s_real, b_real = self._join_attempt(room_id=self.room["room_id"], link_token=UNKNOWN_TOKEN)
        s_fake, b_fake = self._join_attempt(room_id=fabricated, link_token=UNKNOWN_TOKEN)

        self.assertEqual(s_real, s_fake, (b_real, b_fake))
        self.assertEqual(b_real["error"]["code"], b_fake["error"]["code"])
        self.assertEqual(b_real["error"]["message"], b_fake["error"]["message"])


class TransportErrorDiagnosisTests(unittest.TestCase):
    """A wrong / dead endpoint is broken CONFIGURATION, not a bad token. It
    must be named precisely and must never wear the ``invalid_link`` label.
    """

    def test_dead_endpoint_names_itself_and_is_not_invalid_link(self):
        # Port 1 is reserved and never listening -> connection refused.
        client = WeftClient("http://127.0.0.1:1/mcp", "agent-a", "demo")
        try:
            with self.assertRaises(WeftError) as ctx:
                client.connect()
        finally:
            client.close()

        exc = ctx.exception
        self.assertEqual(exc.code, "transport_error")
        self.assertNotEqual(exc.code, "invalid_link")
        message = str(exc).lower()
        self.assertNotIn("invalid_link", message)
        self.assertIn("127.0.0.1:1", message)   # RED until Part 2: name the endpoint
        self.assertIn("base url", message)       # RED until Part 2: say what to check


if __name__ == "__main__":
    unittest.main()
