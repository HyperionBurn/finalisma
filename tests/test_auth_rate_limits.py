"""Auth-endpoint rate limiting — real HTTP, one mechanism (rate_limit.py).

Drives POST /v1/auth/signup and POST /v1/auth/signin over a REAL HTTP server
and asserts the limits are enforced through the SAME ``RateLimiter`` that room
messages already use (weft_cloud/rate_limit.py): trigger-after-N, throttled
known vs throttled unknown responses are byte-identical (no existence
oracle), the window resets, normal usage is unaffected, and the known-vs-
unknown timing ratio stays below 3.0.

The web-app reset-request surface is covered in test_webapp_auth.py
(TestResetRequestRateLimits) against the same mechanism.
"""

from __future__ import annotations

import json
import shutil
import statistics
import sys
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from weft_cloud.service import WeftCloudService, _CloudHTTPHandler
from weft_cloud.storage import SqliteWalBackend
from weft_cloud.identity import accounts as identity_accounts

# Test-tuned limits. The email tier for signin is lowered so "triggers after
# N attempts" and "throttled-known == throttled-unknown" are exercised without
# tripping the (generous) per-IP tier; the signin/reset windows are shortened
# so "resets after its window" does not require a 15-minute sleep.
TEST_AUTH_LIMITS = {
    "signup": {"ip": 20, "email": 5, "window_seconds": 900},
    "signin": {"ip": 40, "email": 5, "window_seconds": 2},
    "reset_request": {"ip": 10, "email": 3, "window_seconds": 2},
}

KNOWN_EMAIL = "known@example.com"
UNKNOWN_EMAIL = "ghost@example.org"
PASSWORD = "CorrectHorse!1"
WRONG = "wrong-password-1"


class _ServiceHarness:
    """Real HTTP server + service, torn down even when a test fails."""

    def __init__(self, auth_rate_limits=None):
        self._tmp = tempfile.mkdtemp(prefix="authrl-")
        self._httpd = ThreadingHTTPServer(("127.0.0.1", 0), _CloudHTTPHandler)
        port = self._httpd.server_address[1]
        self.base = f"http://127.0.0.1:{port}"
        self.service = WeftCloudService(
            SqliteWalBackend(str(Path(self._tmp) / "rl.db")),
            origin=self.base,
            auth_rate_limits=auth_rate_limits,
        )
        _CloudHTTPHandler.service = self.service
        self._thread = threading.Thread(target=self._httpd.serve_forever, daemon=True)
        self._thread.start()

    def post_full(self, path: str, body: dict) -> tuple[int, dict, dict]:
        data = json.dumps(body).encode("utf-8")
        req = urllib.request.Request(self.base + path, data=data, method="POST")
        req.add_header("Content-Type", "application/json")
        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                raw = resp.read()
                parsed = json.loads(raw.decode("utf-8")) if raw else {}
                return resp.status, parsed, dict(resp.headers)
        except urllib.error.HTTPError as exc:
            try:
                raw = exc.read()
                parsed = json.loads(raw.decode("utf-8")) if raw else {}
                return exc.code, parsed, dict(exc.headers)
            finally:
                exc.close()

    def post(self, path: str, body: dict) -> tuple[int, dict]:
        status, parsed, _ = self.post_full(path, body)
        return status, parsed

    def post_raw(self, path: str, body: dict) -> tuple[int, bytes, dict]:
        """POST returning the RAW response bytes — the strongest form of a
        "full body" equality assertion (byte-identical, not just dict-equal)."""
        data = json.dumps(body).encode("utf-8")
        req = urllib.request.Request(self.base + path, data=data, method="POST")
        req.add_header("Content-Type", "application/json")
        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                return resp.status, resp.read(), dict(resp.headers)
        except urllib.error.HTTPError as exc:
            try:
                return exc.code, exc.read(), dict(exc.headers)
            finally:
                exc.close()

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


class AuthRateLimitTestBase(unittest.TestCase):
    def setUp(self):
        self.harness = _ServiceHarness(auth_rate_limits=TEST_AUTH_LIMITS)

    def tearDown(self):
        self.harness.close()


class TestSigninRateLimit(AuthRateLimitTestBase):
    """POST /v1/auth/signin — brute-force protection via the shared limiter."""

    def _signup_known(self) -> None:
        status, body = self.harness.post("/v1/auth/signup",
                                         {"email": KNOWN_EMAIL, "password": PASSWORD})
        self.assertEqual(status, 201, f"signup failed: {body}")

    def test_limit_triggers_after_n_attempts(self):
        self._signup_known()
        for i in range(5):
            status, body = self.harness.post(
                "/v1/auth/signin", {"email": KNOWN_EMAIL, "password": WRONG})
            self.assertEqual(status, 401, f"attempt {i+1} must be allowed: {body}")
        status, body = self.harness.post(
            "/v1/auth/signin", {"email": KNOWN_EMAIL, "password": WRONG})
        self.assertEqual(status, 429, f"6th attempt must be refused: {body}")
        self.assertEqual(body["error"]["code"], "rate_limited")

    def test_throttled_known_equals_throttled_unknown(self):
        """A throttled unknown email returns the BYTE-IDENTICAL response to a
        throttled known email — the 429 cannot be used to enumerate accounts."""
        self._signup_known()
        # Exhaust the per-email window for the known email.
        for _ in range(5):
            self.harness.post("/v1/auth/signin",
                              {"email": KNOWN_EMAIL, "password": WRONG})
        known_status, known_body, known_headers = self.harness.post_full(
            "/v1/auth/signin", {"email": KNOWN_EMAIL, "password": WRONG})
        # Exhaust the per-email window for an unknown email (counted the same way).
        for _ in range(5):
            self.harness.post("/v1/auth/signin",
                              {"email": UNKNOWN_EMAIL, "password": "irrelevant"})
        unknown_status, unknown_body, unknown_headers = self.harness.post_full(
            "/v1/auth/signin", {"email": UNKNOWN_EMAIL, "password": "irrelevant"})

        self.assertEqual(known_status, 429)
        self.assertEqual(unknown_status, 429)
        self.assertEqual(known_body, unknown_body,
                         "throttled-known and throttled-unknown bodies differ — oracle")
        self.assertEqual(
            known_headers.get("Retry-After"), unknown_headers.get("Retry-After"),
            "Retry-After differs between throttled-known and throttled-unknown",
        )
        self.assertIsNotNone(known_headers.get("Retry-After"))

    def test_limit_resets_after_window(self):
        self._signup_known()
        for _ in range(5):
            self.harness.post("/v1/auth/signin",
                              {"email": KNOWN_EMAIL, "password": WRONG})
        status, _ = self.harness.post(
            "/v1/auth/signin", {"email": KNOWN_EMAIL, "password": WRONG})
        self.assertEqual(status, 429, "6th attempt must be refused")
        time.sleep(2.4)
        status, _ = self.harness.post(
            "/v1/auth/signin", {"email": KNOWN_EMAIL, "password": WRONG})
        self.assertEqual(status, 401,
                         "after the window the limit must reset and allow attempts")

    def test_normal_usage_unaffected(self):
        """A person who mistypes once and retries still signs in fine."""
        self._signup_known()
        status, _ = self.harness.post(
            "/v1/auth/signin", {"email": KNOWN_EMAIL, "password": "Oops-mistyped!"})
        self.assertEqual(status, 401, "one mistype is an ordinary refusal")
        status, body = self.harness.post(
            "/v1/auth/signin", {"email": KNOWN_EMAIL, "password": PASSWORD})
        self.assertEqual(status, 200, f"correct retry must succeed: {body}")
        self.assertIn("session_token", body)

    def test_known_and_unknown_signin_identical_full_body_every_attempt(self):
        """The assertion the message-leak oracle slipped past: FULL bodies
        (status AND raw bytes) must be equal for a known and an unknown email
        at the SAME point in the attempt sequence.

        The signin leak survived for weeks because every check compared STATUS
        CODES, and the statuses were identical (401 vs 401, 429 vs 429). The
        leak lived in the message text. This test compares the two responses
        TO EACH OTHER — status AND body bytes — at every attempt-pair, for an
        equal number of prior attempts for each, across the whole sequence so
        the comparison holds on BOTH tiers: before the rate limiter engages
        (401) and after it has engaged (429).

        The email tier is set to 5 so the limiter provably engages partway
        through the 25 pairs; the IP tier is set high so the email tier is the
        one that trips (deterministic). A high per-IP cap keeps the per-email
        keys independent of the shared-IP counter.
        """
        harness = _ServiceHarness(auth_rate_limits={
            "signup": {"ip": 20, "email": 5, "window_seconds": 900},
            # This test proves parity across both tiers, not window expiry. A
            # long window keeps the 25-pair oracle check deterministic when the
            # full suite is under load; expiry is covered separately above.
            "signin": {"ip": 200, "email": 5, "window_seconds": 900},
            "reset_request": {"ip": 10, "email": 3, "window_seconds": 2},
        })
        try:
            status, body = harness.post(
                "/v1/auth/signup", {"email": KNOWN_EMAIL, "password": PASSWORD})
            self.assertEqual(status, 201, f"signup failed: {body}")

            pairs = 25
            seen_statuses: set[int] = set()
            for i in range(pairs):
                known_status, known_raw, _ = harness.post_raw(
                    "/v1/auth/signin", {"email": KNOWN_EMAIL, "password": WRONG})
                unknown_status, unknown_raw, _ = harness.post_raw(
                    "/v1/auth/signin", {"email": UNKNOWN_EMAIL, "password": "irrelevant"})
                self.assertEqual(
                    known_status, unknown_status,
                    f"attempt pair {i + 1}: statuses differ "
                    f"({known_status} vs {unknown_status}) — oracle",
                )
                self.assertEqual(
                    known_raw, unknown_raw,
                    f"attempt pair {i + 1}: bodies differ — oracle "
                    f"(known={known_raw!r} unknown={unknown_raw!r})",
                )
                seen_statuses.add(known_status)
            # Prove BOTH tiers were exercised, not just one.
            self.assertIn(401, seen_statuses,
                          "sequence never exercised the pre-limit (401) tier")
            self.assertIn(429, seen_statuses,
                          "rate limiter never engaged during the 25 pairs")
        finally:
            harness.close()


class TestSignupRateLimit(AuthRateLimitTestBase):
    """POST /v1/auth/signup — storage-exhaustion + repeated-address protection."""

    def test_same_email_repeated_signup_limited(self):
        for _ in range(5):
            status, _ = self.harness.post(
                "/v1/auth/signup", {"email": "spam@example.com", "password": PASSWORD})
            self.assertIn(status, (201, 400))
        status, body = self.harness.post(
            "/v1/auth/signup", {"email": "spam@example.com", "password": PASSWORD})
        self.assertEqual(status, 429, f"6th signup for the same email: {body}")
        self.assertEqual(body["error"]["code"], "rate_limited")

    def test_mass_signup_from_one_ip_limited(self):
        for i in range(20):
            status, body = self.harness.post(
                "/v1/auth/signup",
                {"email": f"bulk{i}@example.com", "password": PASSWORD})
            self.assertEqual(status, 201, f"signup {i} failed: {body}")
        status, body = self.harness.post(
            "/v1/auth/signup", {"email": "bulk20@example.com", "password": PASSWORD})
        self.assertEqual(status, 429,
                         f"21st unique signup from one IP must be refused: {body}")

    def test_normal_signup_unaffected(self):
        status, body = self.harness.post(
            "/v1/auth/signup", {"email": "normal@example.com", "password": PASSWORD})
        self.assertEqual(status, 201, f"a normal signup must succeed: {body}")


class TestAuthTimingRatio(unittest.TestCase):
    """The signin limiter must not reintroduce a known-vs-unknown timing gap.

    Uses DEFAULT limits so the per-email tier (20) and per-IP tier (40) leave
    headroom for a statistically useful sample — the invariant is the same
    scrypt work is done for known and unknown emails, so the ratio stays ~1.0x.
    """

    def _post(self, base, path, body):
        data = json.dumps(body).encode("utf-8")
        req = urllib.request.Request(base + path, data=data, method="POST")
        req.add_header("Content-Type", "application/json")
        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                return resp.status, json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            try:
                raw = exc.read()
                return exc.code, json.loads(raw.decode("utf-8")) if raw else {}
            finally:
                exc.close()

    def test_known_vs_unknown_ratio_below_3x(self):
        harness = _ServiceHarness(auth_rate_limits=None)
        try:
            status, _ = harness.post("/v1/auth/signup",
                                     {"email": KNOWN_EMAIL, "password": PASSWORD})
            self.assertEqual(status, 201)

            def _signin_ms(email, password):
                t0 = time.perf_counter()
                status, _ = harness.post("/v1/auth/signin",
                                         {"email": email, "password": password})
                self.assertEqual(status, 401)
                return (time.perf_counter() - t0) * 1000.0

            for _ in range(3):
                _signin_ms(KNOWN_EMAIL, WRONG)
                _signin_ms(UNKNOWN_EMAIL, "irrelevant")

            unknown_times = []
            wrong_pw_times = []
            for _ in range(12):
                wrong_pw_times.append(_signin_ms(KNOWN_EMAIL, WRONG))
                unknown_times.append(_signin_ms(UNKNOWN_EMAIL, "irrelevant"))

            med_unknown = statistics.median(unknown_times)
            med_wrong = statistics.median(wrong_pw_times)
            ratio = med_wrong / med_unknown if med_unknown > 0 else float("inf")
            self.assertLess(
                ratio, 3.0,
                f"known-vs-unknown signin ratio {ratio:.2f}x "
                f"({med_unknown:.2f}ms vs {med_wrong:.2f}ms) exceeds 3.0 — "
                "the rate limiter reintroduced a timing oracle",
            )
        finally:
            harness.close()


class TestConcurrentSignupEmailReservation(unittest.TestCase):
    """Global duplicate-email rejection is linearized on the real HTTP path."""

    def test_barrier_started_http_signups_return_one_success_and_one_email_exists(self):
        harness = _ServiceHarness(auth_rate_limits=None)
        email = f"race-{time.time_ns()}@example.com"
        start = threading.Barrier(3)
        results = [None, None]
        errors = []

        original_create_account = identity_accounts._create_account

        def pause_after_the_legacy_check(*args, **kwargs):
            # The old implementation checked the global email, committed that
            # read, created a tenant, and only then entered _create_account.
            # Holding both real HTTP requests here makes that old interleaving
            # deterministic without replacing the SQLite layer.
            start_after_check.wait(timeout=10)
            return original_create_account(*args, **kwargs)

        start_after_check = threading.Barrier(2)

        def send(index):
            try:
                start.wait(timeout=10)
                results[index] = harness.post(
                    "/v1/auth/signup", {"email": email, "password": PASSWORD}
                )
            except Exception as exc:  # surfaced below with the thread index
                errors.append((index, exc))

        try:
            # This synchronization wrapper is exercised only by the vulnerable
            # implementation; the transactional fix no longer calls it.
            with patch.object(identity_accounts, "_create_account", pause_after_the_legacy_check):
                threads = [threading.Thread(target=send, args=(i,)) for i in range(2)]
                for thread in threads:
                    thread.start()
                start.wait(timeout=10)
                for thread in threads:
                    thread.join(timeout=30)
            self.assertFalse(errors, errors)
            self.assertTrue(all(result is not None for result in results), results)
            statuses = sorted(result[0] for result in results)
            self.assertEqual(statuses, [201, 400], results)
            for status, body in results:
                if status == 400:
                    self.assertEqual(body["error"]["code"], "email_exists")
            with harness.service.backend.transaction() as tx:
                row = tx.execute(
                    "SELECT COUNT(*) AS n FROM cloud_identity_accounts WHERE email = ?",
                    (email,),
                ).fetchone()
            self.assertEqual(row["n"], 1)
        finally:
            harness.close()


class TestAuthJsonBoundaryTypes(AuthRateLimitTestBase):
    """Malformed JSON field types are client errors, never internal errors."""

    def test_auth_fields_with_wrong_types_return_invalid_argument_without_secrets(self):
        secret = "password-secret-that-must-not-leak"
        cases = (
            ("/v1/auth/signup", {"email": 123, "password": secret}),
            ("/v1/auth/signup", {"email": "typed@example.com", "password": {"secret": secret}}),
            ("/v1/auth/signin", {"email": ["typed@example.com"], "password": secret}),
            ("/v1/auth/signin", {"email": "typed@example.com", "password": False}),
            ("/v1/org/accept_invite", {"invite_token": 123, "email": "typed@example.com", "password": secret}),
            ("/v1/org/accept_invite", {"invite_token": "fiv_not-real", "email": {"value": "typed@example.com"}, "password": secret}),
            ("/v1/org/accept_invite", {"invite_token": "fiv_not-real", "email": "typed@example.com", "password": [secret]}),
        )
        for path, body in cases:
            with self.subTest(path=path, body=body):
                status, response = self.harness.post(path, body)
                self.assertEqual(status, 400, response)
                self.assertEqual(response["error"]["code"], "invalid_argument")
                self.assertNotIn(secret, json.dumps(response))


if __name__ == "__main__":
    unittest.main()
