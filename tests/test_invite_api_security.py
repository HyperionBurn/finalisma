"""Security contract for the authenticated ``POST /v1/org/invite`` API."""

from __future__ import annotations
from tests._server_readiness import await_serving as _await_serving

import contextlib
import io
import json
import logging
import re
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

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from weft_cloud.service import WeftCloudService, _CloudHTTPHandler
from weft_cloud.storage import SqliteWalBackend


def _request(
    base: str,
    method: str,
    path: str,
    body: dict | None = None,
    token: str | None = None,
) -> tuple[int, bytes]:
    data = json.dumps(body).encode("utf-8") if body is not None else None
    request = urllib.request.Request(base + path, data=data, method=method)
    if data is not None:
        request.add_header("Content-Type", "application/json")
    if token:
        request.add_header("Authorization", f"Bearer {token}")
    try:
        with urllib.request.urlopen(request, timeout=10) as response:
            return response.status, response.read()
    except urllib.error.HTTPError as exc:
        try:
            return exc.code, exc.read()
        finally:
            exc.close()


def _json_request(
    base: str,
    method: str,
    path: str,
    body: dict | None = None,
    token: str | None = None,
) -> tuple[int, dict, bytes]:
    status, raw = _request(base, method, path, body=body, token=token)
    return status, json.loads(raw.decode("utf-8")), raw


class InviteApiSecurityTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmpdir = tempfile.mkdtemp(prefix="weft-invite-api-")
        self.db_path = str(Path(self.tmpdir) / "test.db")
        self.service = WeftCloudService(SqliteWalBackend(self.db_path))

        class TestHTTPHandler(_CloudHTTPHandler):
            service = self.service

        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), TestHTTPHandler)
        self.base = f"http://127.0.0.1:{self.httpd.server_address[1]}"
        self.server_thread = threading.Thread(
            target=self.httpd.serve_forever,
            daemon=True,
        )
        self.server_thread.start()
        _await_serving(self.httpd)

    def tearDown(self) -> None:
        self.httpd.shutdown()
        self.httpd.server_close()
        self.server_thread.join(timeout=5)
        self.service.backend.close()
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def _signup(self, email: str) -> dict:
        status, body, _ = _json_request(
            self.base,
            "POST",
            "/v1/auth/signup",
            {"email": email, "password": "SecurePass!1"},
        )
        self.assertEqual(status, HTTPStatus.CREATED, body)
        return body

    def test_invite_response_is_useful_but_not_a_bearer_secret(self) -> None:
        admin = self._signup("admin@example.com")
        invitee_email = "invitee@example.com"

        log_stream = io.StringIO()
        log_handler = logging.StreamHandler(log_stream)
        root_logger = logging.getLogger()
        root_logger.addHandler(log_handler)
        try:
            with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
                status, response, raw_response = _json_request(
                    self.base,
                    "POST",
                    "/v1/org/invite",
                    {"email": invitee_email, "role": "member"},
                    token=admin["session_token"],
                )
        finally:
            root_logger.removeHandler(log_handler)
            log_handler.close()

        self.assertEqual(status, HTTPStatus.CREATED, response)
        self.assertEqual(
            response,
            {
                "invite_id": response["invite_id"],
                "email": invitee_email,
                "role": "member",
                "delivery_status": "queued",
            },
        )
        self.assertNotIn("invite_token", response)
        self.assertNotIn("invite_url", response)

        with self.service.backend.transaction() as tx:
            outbox = tx.execute(
                "SELECT to_email, body FROM cloud_identity_outbox "
                "WHERE tenant_id = ? AND to_email = ? ORDER BY created_at DESC LIMIT 1",
                (admin["tenant_id"], invitee_email),
            ).fetchone()
        self.assertIsNotNone(outbox)
        raw_token = re.search(r"(fiv_[A-Za-z0-9_-]+)", outbox["body"]).group(1)

        response_text = raw_response.decode("utf-8")
        self.assertNotIn(raw_token, response_text)
        self.assertNotIn(raw_token, log_stream.getvalue())

        # Every value returned to the admin is metadata, not a credential that
        # can authenticate as the invited user or any other identity.
        for candidate in response.values():
            if not isinstance(candidate, str):
                continue
            bearer_status, bearer_response, _ = _json_request(
                self.base,
                "GET",
                "/v1/me",
                token=candidate,
            )
            self.assertEqual(bearer_status, HTTPStatus.UNAUTHORIZED, bearer_response)

        # The raw token remains available only through the configured delivery
        # flow, preserving the invite acceptance contract without returning it
        # from the authenticated admin API.
        self.assertIn(raw_token, outbox["body"])


if __name__ == "__main__":
    unittest.main()
