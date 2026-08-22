"""Fail-closed, read-only preflight for the production deployment workflow.

The command checks deployment configuration without printing variable values or
secret contents. It records only names, presence booleans, origin validity, the
shared public/API-origin contract, and the checked-out release SHA in a redacted
JSON report. ``PUBLIC_ORIGIN`` and ``WEFT_API_ORIGIN`` must identify the same
canonical HTTPS origin served by the public reverse proxy. ``WEFT_SITE_URL``
may identify the separate static marketing site.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
from pathlib import Path
from typing import Mapping
from urllib.parse import urlsplit


REQUIRED_VARIABLES = (
    "WEFT_VM",
    "WEFT_NGINX_CONF",
    "WEFT_NGINX_SERVER_NAME",
    "PUBLIC_ORIGIN",
    "WEFT_API_ORIGIN",
    "WEFT_SITE_URL",
)
REQUIRED_SECRETS = (
    "WEFT_SSH_PRIVATE_KEY_CONTENT",
    "WEFT_SSH_KNOWN_HOSTS_CONTENT",
    "WEFT_MCP_PROBE_TOKEN",
)
ORIGIN_VARIABLES = ("PUBLIC_ORIGIN", "WEFT_API_ORIGIN", "WEFT_SITE_URL")
ALLOWED_RELEASE_REF = "refs/heads/main"
_SHA_RE = re.compile(r"^[0-9a-f]{40}$")


def _present(environment: Mapping[str, str], name: str) -> bool:
    return bool(environment.get(name, "").strip())


def _valid_https_origin(value: str) -> bool:
    candidate = value.strip()
    if not candidate or any(
        ch.isspace() or ord(ch) < 32 or ord(ch) == 127 for ch in candidate
    ):
        return False
    parsed = urlsplit(candidate)
    try:
        hostname = parsed.hostname
        parsed.port
    except ValueError:
        return False
    return bool(
        parsed.scheme.lower() == "https"
        and hostname
        and parsed.netloc
        and not parsed.username
        and not parsed.password
        and parsed.path in ("", "/")
        and not parsed.query
        and not parsed.fragment
    )


def _normalized_https_origin(value: str) -> str | None:
    """Return a redaction-safe comparison key for a valid HTTPS origin."""

    candidate = value.strip()
    if not _valid_https_origin(candidate):
        return None
    parsed = urlsplit(candidate)
    hostname = parsed.hostname
    if not hostname:
        return None
    try:
        port = parsed.port
    except ValueError:
        return None

    host = hostname.lower()
    if ":" in host and not host.startswith("["):
        host = f"[{host}]"
    if port is not None and port != 443:
        host = f"{host}:{port}"
    return f"https://{host}"


def evaluate(
    environment: Mapping[str, str],
    *,
    actual_sha: str,
    actual_ref: str,
    expected_sha: str | None = None,
) -> dict:
    """Return a redacted preflight report without exposing configuration values."""

    expected = (expected_sha or "").strip().lower() or None
    failures: list[str] = []
    variable_presence = {
        name: _present(environment, name) for name in REQUIRED_VARIABLES
    }
    secret_presence = {
        name: _present(environment, name) for name in REQUIRED_SECRETS
    }
    origin_validity = {
        name: _valid_https_origin(environment.get(name, ""))
        if variable_presence[name]
        else False
        for name in ORIGIN_VARIABLES
    }

    for name, present in variable_presence.items():
        if not present:
            failures.append(f"missing production variable: {name}")
    for name, present in secret_presence.items():
        if not present:
            failures.append(f"missing production secret: {name}")
    for name, valid in origin_validity.items():
        if not valid:
            failures.append(f"origin must be an absolute HTTPS URL: {name}")

    public_api_origin_match = False
    if origin_validity["PUBLIC_ORIGIN"] and origin_validity["WEFT_API_ORIGIN"]:
        public_api_origin_match = (
            _normalized_https_origin(environment["PUBLIC_ORIGIN"])
            == _normalized_https_origin(environment["WEFT_API_ORIGIN"])
        )
        if not public_api_origin_match:
            failures.append("PUBLIC_ORIGIN must match WEFT_API_ORIGIN")

    actual = actual_sha.strip().lower()
    release_ref = actual_ref.strip()
    if release_ref != ALLOWED_RELEASE_REF:
        failures.append(f"deployment ref must be {ALLOWED_RELEASE_REF}")
    if not _SHA_RE.fullmatch(actual):
        failures.append("checked-out release SHA is not a 40-character commit SHA")
    if expected is not None:
        if not _SHA_RE.fullmatch(expected):
            failures.append("expected release SHA is not a 40-character commit SHA")
        elif actual != expected:
            failures.append("checked-out release SHA does not match expected release SHA")

    return {
        "schema_version": 1,
        "status": "pass" if not failures else "fail",
        "release_ref": release_ref,
        "release_ref_allowed": release_ref == ALLOWED_RELEASE_REF,
        "release_sha": actual,
        "expected_release_sha": expected,
        "release_sha_matches": expected is None or actual == expected,
        "deployment_target_configured": variable_presence["WEFT_VM"],
        "required_variables": variable_presence,
        "required_secrets": secret_presence,
        "https_origins": origin_validity,
        "public_api_origin_match": public_api_origin_match,
        "failures": failures,
    }


def _checked_out_sha() -> str:
    result = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        check=True,
        capture_output=True,
        text=True,
    )
    return result.stdout.strip()


def _parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--release-ref",
        default=os.environ.get("GITHUB_REF", ""),
        help="Git ref selected for deployment. Only refs/heads/main is allowed.",
    )
    parser.add_argument(
        "--expected-sha",
        default=os.environ.get("WEFT_RELEASE_SHA"),
        help="Expected checked-out commit SHA. Defaults to WEFT_RELEASE_SHA.",
    )
    parser.add_argument(
        "--output",
        type=Path,
        help="Write the redacted JSON report to this path as well as stdout.",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv or sys.argv[1:])
    report = evaluate(
        os.environ,
        actual_sha=_checked_out_sha(),
        actual_ref=args.release_ref,
        expected_sha=args.expected_sha,
    )
    rendered = json.dumps(report, indent=2, sort_keys=True) + "\n"
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(rendered, encoding="utf-8")
    sys.stdout.write(rendered)
    return 0 if report["status"] == "pass" else 2


if __name__ == "__main__":
    raise SystemExit(main())
