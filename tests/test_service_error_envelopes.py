"""API error-envelope and rate-limit-header characterization (MPAI-106).

These are CHARACTERIZATION / regression tests, not TDD-red-first tests: the
hosted API surface in ``weft_cloud/service.py`` already meets the MPAI-106
contract (structured ``{"error": {"code", "message"}}`` envelopes, no traceback
leak on 500, ``Retry-After`` on every 429, clean 400s for malformed/empty/non-
object JSON, consistent 401s for missing/invalid auth). This file pins that
contract through the REAL HTTP surface (``ThreadingHTTPServer`` +
``_CloudHTTPHandler``) so a future regression turns red instead of shipping
silently.

The web/app.py flat-error gaps (room events / static 404/403 returning
``{"error": "not_found"}`` instead of the nested shape) are reported as
findings; they are NOT asserted here because the fix lives in a file that
carried another agent's in-flight work at audit time, and a red test the gate
runs would block the ratchet.
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
from http.server import ThreadingHTTPServer
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from weft_cloud.service import WeftCloudService, _CloudHTTPHandler
from weft_cloud.storage import SqliteWalBackend

# Test-tuned auth rate limits — tight email tier so a 429 is reachable in a
# handful of requests without tripping the per-IP tier. Same shape as
# test_auth_rate_limits.TEST_AUTH_LIMITS.
TEST_LIMITS = {
    "signup": {"ip": 20, "email": 5, "window_seconds": 900},
    "signin": {"ip": 40, "email": 5, "window_seconds": 900},
    "reset_request": {"ip": 10, "email": 3, "window_seconds": 2},
}

EMAIL = "envelope@example.com"
PASSWORD = "CorrectHorse!1"
WRONG = "wrong-password-1"


class _Harness:
    """Real HTTP server + service, torn down even when a test fails."""

    def __init__(self, auth_rate_limits=None):
        self._tmp = tempfile.mkdtemp(prefix="errv-")
        self._httpd = ThreadingHTTPServer(("127.0.0.1", 0), _CloudHTTPHandler)
        port = self._httpd.server_address[1]
        self.base = f"http://127.0.0.1:{port}"
        self.service = WeftCloudService(
            SqliteWalBackend(str(Path(self._tmp) / "ev.db")),
            origin=self.base,
            auth_rate_limits=auth_rate_limits,
        )
        _CloudHTTPHandler.service = self.service
        self._thread = threading.Thread(target=self._httpd.serve_forever, daemon=True)
        self._thread.start()

    # --- request helpers -------------------------------------------------

    def get(self, path: str, headers: dict | None = None) -> tuple[int, dict, dict]:
        req = urllib.request.Request(self.base + path, method="GET")
        for k, v in (headers or {}).items():
            req.add_header(k, v)
        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                raw = resp.read()
                parsed = json.loads(raw.decode("utf-8")) if raw else {}
                return resp.status, parsed, _norm_headers(resp.headers)
        except urllib.error.HTTPError as exc:
            try:
                raw = exc.read()
                parsed = json.loads(raw.decode("utf-8")) if raw else {}
                return exc.code, parsed, _norm_headers(exc.headers)
            finally:
                exc.close()

    def post_json(self, path: str, body: dict) -> tuple[int, dict, dict]:
        data = json.dumps(body).encode("utf-8")
        req = urllib.request.Request(self.base + path, data=data, method="POST")
        req.add_header("Content-Type", "application/json")
        return self._send(req)

    def post_raw(self, path: str, raw: bytes,
                 content_type: str = "application/json") -> tuple[int, dict, dict]:
        req = urllib.request.Request(self.base + path, data=raw, method="POST")
        req.add_header("Content-Type", content_type)
        return self._send(req)

    def post_empty(self, path: str) -> tuple[int, dict, dict]:
        req = urllib.request.Request(self.base + path, method="POST")
        req.add_header("Content-Type", "application/json")
        return self._send(req)

    def _send(self, req: urllib.request.Request) -> tuple[int, dict, dict]:
        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                raw = resp.read()
                parsed = json.loads(raw.decode("utf-8")) if raw else {}
                return resp.status, parsed, _norm_headers(resp.headers)
        except urllib.error.HTTPError as exc:
            try:
                raw = exc.read()
                parsed = json.loads(raw.decode("utf-8")) if raw else {}
                return exc.code, parsed, _norm_headers(exc.headers)
            finally:
                exc.close()

    # --- lifecycle -------------------------------------------------------

    def signup(self) -> str:
        status, body, _ = self.post_json("/v1/auth/signup",
                                        {"email": EMAIL, "password": PASSWORD})
        assert status == 201, f"signup failed: {body}"
        token = body.get("session_token") or body.get("token")
        assert token, f"no session token in signup body: {body}"
        return token

    def close(self):
        try:
            self._httpd.shutdown()
        finally:
            self._httpd.server_close()
        try:
            self.service.backend.close()
        except Exception:
            pass
        shutil.rmtree(self._tmp, ignore_errors=True)


def _norm_headers(headers) -> dict:
    return {k.lower(): v for k, v in headers.items()}


def _assert_envelope(test: unittest.TestCase, body: dict) -> None:
    """Every error response is ``{"error": {"code": str, "message": str}}``."""
    test.assertIn("error", body, f"missing top-level 'error' key: {body}")
    err = body["error"]
    test.assertIsInstance(err, dict, f"'error' is not an object: {body}")
    test.assertIn("code", err, f"missing error.code: {body}")
    test.assertIn("message", err, f"missing error.message: {body}")
    test.assertIsInstance(err["code"], str, f"error.code not a string: {body}")
    test.assertIsInstance(err["message"], str, f"error.message not a string: {body}")


class TestServiceErrorEnvelopes(unittest.TestCase):
    def setUp(self):
        self.harness = _Harness(auth_rate_limits=TEST_LIMITS)

    def tearDown(self):
        self.harness.close()

    # 1. Malformed / empty / non-object JSON --------------------------------

    def test_malformed_json_returns_invalid_json_envelope(self):
        status, body, headers = self.harness.post_raw(
            "/v1/auth/signin", b'{not valid json')
        self.assertEqual(status, 400, f"expected 400, got {status}: {body}")
        _assert_envelope(self, body)
        self.assertEqual(body["error"]["code"], "invalid_json")
        self.assertIn("application/json", headers.get("content-type", ""),
                      "malformed-JSON 400 must be JSON")

    def test_empty_body_returns_invalid_body_envelope(self):
        status, body, headers = self.harness.post_empty("/v1/auth/signin")
        self.assertEqual(status, 400, f"expected 400, got {status}: {body}")
        _assert_envelope(self, body)
        self.assertEqual(body["error"]["code"], "invalid_body")

    def test_non_object_json_returns_invalid_body_envelope(self):
        status, body, headers = self.harness.post_raw(
            "/v1/auth/signin", b'[1, 2, 3]')
        self.assertEqual(status, 400, f"expected 400, got {status}: {body}")
        _assert_envelope(self, body)
        self.assertEqual(body["error"]["code"], "invalid_body")

    def test_truncated_body_rejected_cleanly(self):
        # Send honest Content-Length with INCOMPLETE JSON (truncated object).
        # The server reads the announced bytes, fails to decode, and must
        # return a 400 invalid_json envelope — not a 500, not a hang.
        raw = b'{"email":'
        status, body, _ = self.harness.post_raw("/v1/auth/signin", raw)
        self.assertEqual(status, 400, f"truncated JSON must be 400: {status} {body}")
        _assert_envelope(self, body)
        self.assertEqual(body["error"]["code"], "invalid_json")

    # 2. Missing / invalid auth --------------------------------------------

    def test_missing_auth_returns_unauthorized_envelope(self):
        status, body, headers = self.harness.get("/v1/me")
        self.assertEqual(status, 401, f"expected 401, got {status}: {body}")
        _assert_envelope(self, body)
        # Must not leak whether the account exists or any internal id.
        self.assertNotIn("traceback", json.dumps(body).lower())
        self.assertIn("application/json", headers.get("content-type", ""))

    def test_garbage_bearer_token_returns_unauthorized_envelope(self):
        status, body, headers = self.harness.get(
            "/v1/me", {"Authorization": "Bearer not-a-real-token"})
        self.assertEqual(status, 401, f"expected 401, got {status}: {body}")
        _assert_envelope(self, body)
        # No internal state in the message — generic "Invalid session" / code.
        self.assertNotIn("traceback", json.dumps(body).lower())

    def test_malformed_authorization_header_returns_unauthorized(self):
        # "Bearer" with no token, and a non-Bearer scheme, must both 401.
        for header in ["Bearer ", "Basic abc:def", ""]:
            status, body, _ = self.harness.get(
                "/v1/me", {"Authorization": header} if header else None)
            self.assertEqual(status, 401, f"expected 401 for {header!r}: {body}")
            _assert_envelope(self, body)

    # 3. Rate-limit 429 carries Retry-After --------------------------------

    def test_rate_limited_429_has_retry_after_integer(self):
        self.harness.signup()
        for _ in range(5):
            self.harness.post_json("/v1/auth/signin",
                                   {"email": EMAIL, "password": WRONG})
        status, body, headers = self.harness.post_json(
            "/v1/auth/signin", {"email": EMAIL, "password": WRONG})
        self.assertEqual(status, 429, f"expected 429, got {status}: {body}")
        _assert_envelope(self, body)
        self.assertEqual(body["error"]["code"], "rate_limited")
        retry_after = headers.get("retry-after")
        self.assertIsNotNone(retry_after,
                            "429 response missing Retry-After header")
        # Must be an integer-parseable string >= 1 (client SDKs parse it).
        parsed = int(retry_after)
        self.assertGreaterEqual(parsed, 1,
                                f"Retry-After must be >= 1 second, got {parsed}")

    # 4. 500 never leaks a traceback --------------------------------------

    def test_internal_error_never_leaks_traceback(self):
        # Force the bare-Exception branch of _handle by making a handler
        # raise a RuntimeError whose message contains a canary string. The
        # client must receive a generic internal_error envelope with NO trace
        # of the canary or any traceback.
        canary = "SECRET_INTERNAL_DETAIL_zzq9k"
        with patch.object(self.harness.service, "handle_me",
                          side_effect=RuntimeError(canary)):
            status, body, headers = self.harness.get("/v1/me")
        self.assertEqual(status, 500, f"expected 500, got {status}: {body}")
        _assert_envelope(self, body)
        self.assertEqual(body["error"]["code"], "internal_error")
        self.assertEqual(body["error"]["message"], "Internal server error")
        raw = json.dumps(body)
        self.assertNotIn(canary, raw,
                         "500 response leaked the exception message")
        self.assertNotIn("Traceback", raw, "500 response leaked a traceback")
        self.assertIn("application/json", headers.get("content-type", ""))

    # 5. Structured shape across many error kinds --------------------------

    def test_all_error_responses_use_nested_envelope(self):
        # Collect one of each error kind and assert the nested shape.
        cases = [
            self.harness.post_raw("/v1/auth/signin", b'!!!'),      # invalid_json
            self.harness.post_empty("/v1/auth/signin"),             # invalid_body
            self.harness.get("/v1/me"),                            # unauthorized
            self.harness.get("/v1/does-not-exist"),                # not_found
        ]
        for status, body, _ in cases:
            self.assertLess(status, 500, f"unexpected 5xx: {status} {body}")
            _assert_envelope(self, body)

    # 6. Success endpoints still return JSON -------------------------------

    def test_healthz_returns_json(self):
        status, body, headers = self.harness.get("/healthz")
        self.assertEqual(status, 200)
        self.assertIn("application/json", headers.get("content-type", ""))
        self.assertIn("status", body)

    def test_agent_card_returns_json(self):
        status, body, headers = self.harness.get("/.well-known/agent-card.json")
        self.assertEqual(status, 200)
        self.assertIn("application/json", headers.get("content-type", ""))
        self.assertEqual(body.get("service"), "weft")

    def test_unknown_post_route_returns_not_found_envelope(self):
        status, body, headers = self.harness.post_json("/v1/no-such-route", {})
        self.assertEqual(status, 404, f"expected 404, got {status}: {body}")
        _assert_envelope(self, body)
        self.assertEqual(body["error"]["code"], "not_found")


if __name__ == "__main__":
    unittest.main(verbosity=2)
