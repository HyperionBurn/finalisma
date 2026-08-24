"""Tests for "know about an outage before the owner does".

Covers:

- scripts/healthcheck.py: a dependency-free /healthz + storage-backed /readyz
  + unauthenticated POST /mcp probe. Runs fully offline against a tiny
  in-process HTTP server (stdlib http.server) — no real network, no VM.
- scripts/mail_error_detail.py: structured SMTP failure detail instead of
  the real production bug, where `last_error` was the literal string
  "permanent" for all 104 failed rows with no SMTP code and no reason.
"""

from __future__ import annotations

import json
import smtplib
import subprocess
import sys
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
SCRIPTS_DIR = REPO_ROOT / "scripts"
sys.path.insert(0, str(SCRIPTS_DIR))

from healthcheck import append_log, run  # noqa: E402
from mail_error_detail import classify_mail_error  # noqa: E402
from tests._server_readiness import await_serving as _await_serving  # noqa: E402


class _FakeApp(BaseHTTPRequestHandler):
    """Minimal stand-in for the real cloud service: one status per test."""

    healthz_status = 200
    readyz_status = 200
    readyz_service = "weft-cloud"
    mcp_status = 401

    def do_GET(self):
        if self.path == "/healthz":
            self.send_response(self.healthz_status)
            self.end_headers()
            self.wfile.write(b"{}")
        elif self.path == "/readyz":
            self.send_response(self.readyz_status)
            self.end_headers()
            self.wfile.write(json.dumps({
                "status": "ready" if self.readyz_status == 200 else "unavailable",
                "service": self.readyz_service,
            }).encode())
        else:
            self.send_response(404)
            self.end_headers()

    def do_POST(self):
        if self.path == "/mcp":
            self.send_response(self.mcp_status)
            self.end_headers()
            self.wfile.write(b"{}")
        else:
            self.send_response(404)
            self.end_headers()

    def log_message(self, *args):  # silence the test-run output
        pass


class _ServerCase(unittest.TestCase):
    healthz_status = 200
    readyz_status = 200
    readyz_service = "weft-cloud"
    mcp_status = 401

    def setUp(self):
        handler = type("Handler", (_FakeApp,), {
            "healthz_status": self.healthz_status,
            "readyz_status": self.readyz_status,
            "readyz_service": self.readyz_service,
            "mcp_status": self.mcp_status,
        })
        self.server = HTTPServer(("127.0.0.1", 0), handler)
        self.port = self.server.server_address[1]
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        _await_serving(self.port)
        self.addCleanup(self.server.server_close)
        self.addCleanup(self.thread.join, 2)
        self.addCleanup(self.server.shutdown)

    @property
    def base_url(self) -> str:
        return f"http://127.0.0.1:{self.port}"


class HealthcheckHappyPathTests(_ServerCase):
    healthz_status = 200
    mcp_status = 401

    def test_both_probes_pass(self):
        ok, record = run(self.base_url)
        self.assertTrue(ok)
        self.assertTrue(record["healthz"]["ok"])
        self.assertTrue(record["readyz"]["ok"])
        self.assertTrue(record["mcp_unauth"]["ok"])
        self.assertEqual(record["healthz"]["status"], 200)
        self.assertEqual(record["readyz"]["status"], 200)
        self.assertEqual(record["mcp_unauth"]["status"], 401)


class HealthcheckReadyzDownTests(_ServerCase):
    healthz_status = 200
    readyz_status = 503
    mcp_status = 401

    def test_healthy_healthz_but_failed_readyz_is_not_green(self):
        ok, record = run(self.base_url)
        self.assertFalse(ok)
        self.assertTrue(record["healthz"]["ok"])
        self.assertFalse(record["readyz"]["ok"])
        self.assertEqual(record["readyz"]["status"], 503)
        self.assertTrue(record["mcp_unauth"]["ok"])


class HealthcheckReadyzIdentityTests(_ServerCase):
    readyz_service = "weft-web"

    def test_wrong_process_identity_is_not_green(self):
        ok, record = run(self.base_url)
        self.assertFalse(ok)
        self.assertFalse(record["readyz"]["ok"])
        self.assertEqual(record["readyz"]["detail"], "expected status=ready and service=weft-cloud")


class HealthcheckEdgeBoundaryTests(_ServerCase):
    """The periodic probe must exercise nginx, not only the backend socket."""

    healthz_status = 200
    mcp_status = 401

    def setUp(self):
        super().setUp()
        handler = type("EdgeHandler", (_FakeApp,), {
            "healthz_status": 200,
            "mcp_status": 303,
        })
        self.edge_server = HTTPServer(("127.0.0.1", 0), handler)
        self.edge_port = self.edge_server.server_address[1]
        self.edge_thread = threading.Thread(
            target=self.edge_server.serve_forever, daemon=True,
        )
        self.edge_thread.start()
        _await_serving(self.edge_port)
        self.addCleanup(self.edge_server.server_close)
        self.addCleanup(self.edge_thread.join, 2)
        self.addCleanup(self.edge_server.shutdown)

    @property
    def edge_url(self) -> str:
        return f"http://127.0.0.1:{self.edge_port}"

    def test_healthy_backend_and_broken_edge_fail_overall_probe(self):
        ok, record = run(self.base_url, edge_url=self.edge_url)
        self.assertFalse(ok)
        self.assertTrue(record["healthz"]["ok"])
        self.assertFalse(record["mcp_unauth"]["ok"])
        self.assertEqual(record["mcp_unauth"]["status"], 303)
        self.assertEqual(record["edge_url"], self.edge_url)


class HealthcheckMcpFallthroughTests(_ServerCase):
    healthz_status = 200
    mcp_status = 303  # the exact real-world bug: falls through to the web app's login

    def test_303_on_unauth_mcp_is_a_failure_not_a_pass(self):
        ok, record = run(self.base_url)
        self.assertFalse(ok)
        self.assertFalse(record["mcp_unauth"]["ok"])
        self.assertIn("login redirect", record["mcp_unauth"]["detail"])


class HealthcheckMcpWronglyOpenTests(_ServerCase):
    healthz_status = 200
    mcp_status = 200  # would mean the unauthenticated request was ACCEPTED

    def test_200_on_unauth_mcp_is_a_failure(self):
        ok, record = run(self.base_url)
        self.assertFalse(ok)
        self.assertFalse(record["mcp_unauth"]["ok"])


class HealthcheckHealthzDownTests(_ServerCase):
    healthz_status = 500
    mcp_status = 401

    def test_500_on_healthz_is_a_failure(self):
        ok, record = run(self.base_url)
        self.assertFalse(ok)
        self.assertFalse(record["healthz"]["ok"])
        self.assertEqual(record["healthz"]["status"], 500)


class HealthcheckUnreachableTests(unittest.TestCase):
    def test_unreachable_service_fails_cleanly_not_a_crash(self):
        # Port 1 is a privileged, essentially-never-listening port — nothing
        # answers, which is exactly what a downed service looks like.
        ok, record = run("http://127.0.0.1:1", timeout=1.0)
        self.assertFalse(ok)
        self.assertFalse(record["healthz"]["ok"])
        self.assertIsNone(record["healthz"]["status"])
        self.assertIn("unreachable", record["healthz"]["detail"])

    def test_log_is_appended_as_one_json_line_per_run(self):
        with tempfile.TemporaryDirectory() as tmp:
            log_path = Path(tmp) / "healthcheck.jsonl"
            _ok1, record1 = run("http://127.0.0.1:1", timeout=1.0)
            _ok2, record2 = run("http://127.0.0.1:1", timeout=1.0)
            append_log(log_path, record1)
            append_log(log_path, record2)
            lines = log_path.read_text(encoding="utf-8").splitlines()
            self.assertEqual(len(lines), 2)
            for line in lines:
                parsed = json.loads(line)
                self.assertIn("ts", parsed)

    def test_never_logs_response_bodies_or_credentials(self):
        with tempfile.TemporaryDirectory() as tmp:
            log_path = Path(tmp) / "healthcheck.jsonl"
            _ok, record = run("http://127.0.0.1:1", timeout=1.0)
            append_log(log_path, record)
            raw = log_path.read_text(encoding="utf-8")
            for forbidden in ("password", "token", "authorization", "body"):
                self.assertNotIn(forbidden, raw.lower())

    def test_cli_rejects_http_edge_when_https_is_required(self):
        result = subprocess.run(
            [
                sys.executable,
                str(SCRIPTS_DIR / "healthcheck.py"),
                "--base-url", "http://127.0.0.1:18788",
                "--edge-url", "http://127.0.0.1",
                "--require-https-edge",
            ],
            capture_output=True,
            text=True,
        )
        self.assertEqual(result.returncode, 2)
        self.assertIn("absolute HTTPS URL", result.stderr)


class MailErrorDetailTests(unittest.TestCase):
    """The real bug: `last_error` was the literal string "permanent" for
    all 104 failed rows, with no SMTP code and no reason — nobody could
    diagnose real delivery failures from that."""

    def test_signature_has_no_body_or_subject_parameter(self):
        # Structural guarantee, not just a convention: there is no parameter
        # through which a body/subject could be threaded into stored detail.
        import inspect
        self.assertEqual(list(inspect.signature(classify_mail_error).parameters), ["exc"])

    def test_recipient_refused_extracts_code_and_is_not_retryable(self):
        exc = smtplib.SMTPRecipientsRefused({"a@example.com": (550, b"Mailbox unavailable")})
        detail = classify_mail_error(exc)
        self.assertEqual(detail.smtp_code, 550)
        self.assertEqual(detail.category, "recipient_refused")
        self.assertFalse(detail.retryable)
        self.assertNotEqual(detail.reason, "permanent", "must not regress to the flattened bug string")

    def test_4xx_is_transient_and_retryable(self):
        exc = smtplib.SMTPResponseException(421, "Service not available")
        detail = classify_mail_error(exc)
        self.assertEqual(detail.smtp_code, 421)
        self.assertTrue(detail.retryable)

    def test_5xx_is_permanent_and_not_retryable(self):
        exc = smtplib.SMTPResponseException(550, "Mailbox unavailable")
        detail = classify_mail_error(exc)
        self.assertEqual(detail.smtp_code, 550)
        self.assertFalse(detail.retryable)

    def test_connection_failure_has_no_code_but_is_retryable(self):
        exc = ConnectionRefusedError("Connection refused")
        detail = classify_mail_error(exc)
        self.assertIsNone(detail.smtp_code)
        self.assertTrue(detail.retryable)
        self.assertEqual(detail.category, "connect_refused")

    def test_timeout_is_retryable_regardless_of_python_version_aliasing(self):
        # socket.timeout is an alias for TimeoutError on modern Python; both
        # spellings must classify the same way.
        import socket
        detail = classify_mail_error(socket.timeout("timed out"))
        self.assertTrue(detail.retryable)
        self.assertEqual(detail.category, "timeout")

    def test_different_causes_get_different_structured_detail(self):
        # This is the actual bug, reproduced and shown fixed: today every
        # one of 104 failed rows gets the identical string "permanent"
        # regardless of cause. Two different real causes here must produce
        # two different, non-generic reasons and categories.
        refused = classify_mail_error(smtplib.SMTPResponseException(550, "Mailbox unavailable"))
        throttled = classify_mail_error(smtplib.SMTPResponseException(421, "Too many connections"))
        self.assertNotEqual(refused.reason, throttled.reason)
        self.assertNotEqual(refused.retryable, throttled.retryable)

    def test_to_json_is_structured_not_a_bare_string(self):
        detail = classify_mail_error(smtplib.SMTPResponseException(550, "Mailbox unavailable"))
        parsed = json.loads(detail.to_json())
        self.assertEqual(set(parsed), {"category", "smtp_code", "reason", "retryable"})

    def test_reason_is_truncated_defensively(self):
        detail = classify_mail_error(Exception("x" * 5000))
        self.assertLessEqual(len(detail.reason), 200)

    def test_unrecognized_exception_still_produces_a_safe_record(self):
        detail = classify_mail_error(RuntimeError("something unexpected"))
        self.assertEqual(detail.category, "unknown")
        self.assertIsNone(detail.smtp_code)
        self.assertIsInstance(detail.retryable, bool)


if __name__ == "__main__":
    unittest.main()
