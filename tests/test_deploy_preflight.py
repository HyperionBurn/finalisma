from __future__ import annotations

import io
import json
import os
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

import deploy_preflight


SHA = "a" * 40
MAIN_REF = "refs/heads/main"


def _valid_environment() -> dict[str, str]:
    return {
        "WEFT_VM": "deploy@vm.example",
        "WEFT_NGINX_CONF": "/etc/nginx/sites-enabled/weft",
        "WEFT_NGINX_SERVER_NAME": "weft.example",
        "PUBLIC_ORIGIN": "HTTPS://weft.example/",
        "WEFT_API_ORIGIN": "https://weft.example:443",
        "WEFT_SITE_URL": "https://marketing.weft.example",
        "WEFT_SSH_PRIVATE_KEY_CONTENT": "PRIVATE-KEY-CONTENT",
        "WEFT_SSH_KNOWN_HOSTS_CONTENT": "KNOWN-HOSTS-CONTENT",
        "WEFT_MCP_PROBE_TOKEN": "MCP-PROBE-TOKEN",
    }


class DeployPreflightTests(unittest.TestCase):
    def test_missing_configuration_fails_without_printing_values(self) -> None:
        report = deploy_preflight.evaluate(
            {"WEFT_VM": "secret-host-value"},
            actual_sha=SHA,
            actual_ref=MAIN_REF,
            expected_sha=SHA,
        )

        self.assertEqual(report["status"], "fail")
        self.assertIn("missing production variable: WEFT_NGINX_CONF", report["failures"])
        self.assertFalse(report["required_secrets"]["WEFT_MCP_PROBE_TOKEN"])
        rendered = json.dumps(report)
        self.assertNotIn("secret-host-value", rendered)

    def test_valid_configuration_passes_and_is_redacted(self) -> None:
        environment = _valid_environment()
        report = deploy_preflight.evaluate(
            environment,
            actual_sha=SHA,
            actual_ref=MAIN_REF,
            expected_sha=SHA,
        )

        self.assertEqual(report["status"], "pass")
        self.assertTrue(report["release_sha_matches"])
        self.assertTrue(all(report["required_variables"].values()))
        self.assertTrue(all(report["required_secrets"].values()))
        self.assertTrue(all(report["https_origins"].values()))
        self.assertTrue(report["public_api_origin_match"])
        rendered = json.dumps(report)
        for value in environment.values():
            self.assertNotIn(value, rendered)

    def test_public_api_origin_mismatch_fails_closed_without_value_leakage(self) -> None:
        environment = _valid_environment()
        environment["PUBLIC_ORIGIN"] = "https://weft.example"
        environment["WEFT_API_ORIGIN"] = "https://api.weft.example"
        report = deploy_preflight.evaluate(
            environment,
            actual_sha=SHA,
            actual_ref=MAIN_REF,
            expected_sha=SHA,
        )

        self.assertEqual(report["status"], "fail")
        self.assertFalse(report["public_api_origin_match"])
        self.assertIn("PUBLIC_ORIGIN must match WEFT_API_ORIGIN", report["failures"])
        rendered = json.dumps(report)
        self.assertNotIn("weft.example", rendered)
        self.assertNotIn("api.weft.example", rendered)

    def test_http_origin_and_sha_mismatch_fail_closed(self) -> None:
        environment = _valid_environment()
        environment["WEFT_API_ORIGIN"] = "http://api.weft.example"
        report = deploy_preflight.evaluate(
            environment,
            actual_sha=SHA,
            actual_ref=MAIN_REF,
            expected_sha="b" * 40,
        )

        self.assertEqual(report["status"], "fail")
        self.assertIn("origin must be an absolute HTTPS URL: WEFT_API_ORIGIN", report["failures"])
        self.assertFalse(report["public_api_origin_match"])
        self.assertNotIn("PUBLIC_ORIGIN must match WEFT_API_ORIGIN", report["failures"])
        self.assertIn("checked-out release SHA does not match expected release SHA", report["failures"])

    def test_non_main_release_ref_fails_closed(self) -> None:
        report = deploy_preflight.evaluate(
            _valid_environment(),
            actual_sha=SHA,
            actual_ref="refs/heads/codex/not-main",
            expected_sha=SHA,
        )

        self.assertEqual(report["status"], "fail")
        self.assertFalse(report["release_ref_allowed"])
        self.assertIn("deployment ref must be refs/heads/main", report["failures"])

    def test_origin_paths_credentials_and_invalid_ports_fail_closed(self) -> None:
        environment = _valid_environment()
        invalid_origins = {
            "https://api.weft.example/base": "path",
            "https://user:password@api.weft.example": "credentials",
            "https://api.weft.example:bad": "port",
            "https://api.weft.example?probe=1": "query",
            "https://api.weft.example#fragment": "fragment",
        }
        for origin, label in invalid_origins.items():
            with self.subTest(origin=label):
                environment["WEFT_API_ORIGIN"] = origin
                report = deploy_preflight.evaluate(
                    environment,
                    actual_sha=SHA,
                    actual_ref=MAIN_REF,
                    expected_sha=SHA,
                )
                self.assertEqual(report["status"], "fail")
                self.assertFalse(report["https_origins"]["WEFT_API_ORIGIN"])

    def test_main_writes_redacted_failure_report_and_returns_two(self) -> None:
        environment = _valid_environment()
        environment["WEFT_API_ORIGIN"] = "https://api.weft.example/base"
        with tempfile.TemporaryDirectory() as tmp:
            output = Path(tmp) / "preflight.json"
            stdout = io.StringIO()
            with patch.object(deploy_preflight, "_checked_out_sha", return_value=SHA), \
                 patch.dict(os.environ, {**environment, "GITHUB_REF": "refs/heads/release"}, clear=False), \
                 redirect_stdout(stdout):
                status = deploy_preflight.main(
                    ["--expected-sha", SHA, "--output", str(output)]
                )

            self.assertEqual(status, 2)
            report = json.loads(output.read_text(encoding="utf-8"))
            self.assertEqual(report["status"], "fail")
            self.assertIn("deployment ref must be refs/heads/main", report["failures"])
            self.assertNotIn("api.weft.example/base", stdout.getvalue())

    def test_main_writes_redacted_report_and_returns_success(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            output = Path(tmp) / "preflight.json"
            stdout = io.StringIO()
            with patch.object(deploy_preflight, "_checked_out_sha", return_value=SHA), \
                 patch.dict(os.environ, _valid_environment(), clear=False), \
                 redirect_stdout(stdout):
                status = deploy_preflight.main(
                    ["--release-ref", MAIN_REF, "--expected-sha", SHA, "--output", str(output)]
                )

            self.assertEqual(status, 0)
            self.assertEqual(json.loads(output.read_text(encoding="utf-8"))["status"], "pass")
            self.assertEqual(json.loads(stdout.getvalue())["release_sha"], SHA)


if __name__ == "__main__":
    unittest.main()
