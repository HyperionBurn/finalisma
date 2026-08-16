"""Read-only probe for the currently hosted Weft release surfaces.

The probe deliberately separates reachability from release alignment.  A
reachable old deployment is reported as ``DRIFT`` rather than being mistaken
for proof that the current source bundle is live.  It never authenticates,
mutates state, prints response bodies, or accepts bearer credentials.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from http.client import HTTPException
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener

DEFAULT_API_ORIGIN = "https://weft.switzerlandnorth.cloudapp.azure.com"
DEFAULT_SITE_ORIGIN = "https://finalisma.vercel.app"
_MAX_BODY_BYTES = 2 * 1024 * 1024
_API_HEADERS = ("strict-transport-security", "x-frame-options", "referrer-policy")
_LOGIN_HEADERS = _API_HEADERS + ("content-security-policy",)
_SITE_HEADERS = _LOGIN_HEADERS


class _NoRedirect(HTTPRedirectHandler):
    """Keep 3xx responses observable instead of following them silently."""

    def redirect_request(self, *_args, **_kwargs):
        return None


_OPENER = build_opener(_NoRedirect)


def _origin(value: str) -> str:
    value = value.strip().rstrip("/")
    parsed = urlsplit(value)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise ValueError(f"origin must be an absolute http(s) URL: {value!r}")
    if parsed.query or parsed.fragment:
        raise ValueError("origin must not contain a query string or fragment")
    return value


def _fetch(url: str, timeout: float) -> dict:
    request = Request(url, headers={"User-Agent": "weft-live-release-probe/1"})
    try:
        with _OPENER.open(request, timeout=timeout) as response:
            raw = response.read(_MAX_BODY_BYTES + 1)
            return _response(response.status, response.headers, raw)
    except HTTPError as exc:
        try:
            raw = exc.read(_MAX_BODY_BYTES + 1)
        finally:
            exc.close()
        return _response(exc.code, exc.headers, raw)
    except (HTTPException, OSError, URLError, TimeoutError) as exc:
        return {"status": None, "headers": {}, "bytes": 0, "error": str(exc)}


def _response(status: int, headers, raw: bytes) -> dict:
    return {
        "status": int(status),
        "headers": {
            name.lower(): value
            for name, value in headers.items()
            if name.lower()
            in {
                "content-security-policy",
                "location",
                "referrer-policy",
                "strict-transport-security",
                "x-frame-options",
            }
        },
        "bytes": len(raw),
        "truncated": len(raw) > _MAX_BODY_BYTES,
        "body": raw[:_MAX_BODY_BYTES].decode("utf-8", "replace"),
    }


def _has_headers(response: dict, names: tuple[str, ...]) -> bool:
    return all(response["headers"].get(name) for name in names)


def _has(response: dict, marker: str) -> bool:
    return marker in response.get("body", "")


def _summary(response: dict) -> dict:
    """Remove body contents before the result is printed."""
    return {
        key: value
        for key, value in response.items()
        if key != "body"
    }


def probe(api_origin: str, site_origin: str, timeout: float = 20.0) -> dict:
    api_origin = _origin(api_origin)
    site_origin = _origin(site_origin)

    endpoints = {
        "api_health": _fetch(f"{api_origin}/healthz", timeout),
        "api_root": _fetch(f"{api_origin}/", timeout),
        "api_login": _fetch(f"{api_origin}/login", timeout),
        "site_home": _fetch(f"{site_origin}/", timeout),
        "site_robots": _fetch(f"{site_origin}/robots.txt", timeout),
        "site_sitemap": _fetch(f"{site_origin}/sitemap.xml", timeout),
        "site_manifest": _fetch(f"{site_origin}/release-manifest.json", timeout),
    }

    api_health_ok = (
        endpoints["api_health"]["status"] == 200
        and _has(endpoints["api_health"], '"status"')
        and _has(endpoints["api_health"], '"ok"')
    )
    api_login_redirect_ok = (
        endpoints["api_root"]["status"] in {301, 302, 303, 307, 308}
        and endpoints["api_root"]["headers"].get("location", "").rstrip("/")
        .endswith("/login")
    )
    api_headers_ok = _has_headers(endpoints["api_health"], _API_HEADERS) and _has_headers(
        endpoints["api_login"], _LOGIN_HEADERS
    )
    site_home_ok = endpoints["site_home"]["status"] == 200
    site_release_markers_ok = all(
        (
            _has(endpoints["site_home"], marker)
            for marker in ('rel="canonical"', 'og:url', 'data-cohort-build')
        )
    )
    site_indexing_ok = (
        endpoints["site_robots"]["status"] == 200
        and _has(endpoints["site_robots"], "Sitemap:")
        and endpoints["site_sitemap"]["status"] == 200
    )
    site_manifest_ok = endpoints["site_manifest"]["status"] == 200
    site_headers_ok = _has_headers(endpoints["site_home"], _SITE_HEADERS)

    reachability_ok = api_health_ok and site_home_ok
    release_alignment_ok = (
        site_release_markers_ok
        and site_indexing_ok
        and site_manifest_ok
    )
    status = (
        "PASS"
        if reachability_ok and release_alignment_ok and api_login_redirect_ok
        and api_headers_ok and site_headers_ok
        else "DRIFT"
        if reachability_ok
        else "UNREACHABLE"
    )
    return {
        "probe": "weft-live-release-v1",
        "origins": {"api": api_origin, "site": site_origin},
        "status": status,
        "checks": {
            "reachability": reachability_ok,
            "api_health": api_health_ok,
            "api_root_redirects_to_login": api_login_redirect_ok,
            "api_security_headers": api_headers_ok,
            "site_home_reachable": site_home_ok,
            "site_release_markers": site_release_markers_ok,
            "site_indexing": site_indexing_ok,
            "site_release_manifest": site_manifest_ok,
            "site_security_headers": site_headers_ok,
            "release_alignment": release_alignment_ok,
        },
        "endpoints": {name: _summary(response) for name, response in endpoints.items()},
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--api-origin",
        default=os.environ.get("WEFT_API_ORIGIN", DEFAULT_API_ORIGIN),
    )
    parser.add_argument(
        "--site-origin",
        default=os.environ.get("WEFT_SITE_URL", DEFAULT_SITE_ORIGIN),
    )
    parser.add_argument("--timeout", type=float, default=20.0)
    parser.add_argument("--pretty", action="store_true")
    args = parser.parse_args(argv)
    try:
        result = probe(args.api_origin, args.site_origin, timeout=args.timeout)
    except ValueError as exc:
        print(json.dumps({"probe": "weft-live-release-v1", "status": "INVALID_ARGUMENT", "error": str(exc)}))
        return 4
    print(json.dumps(result, indent=2 if args.pretty else None, sort_keys=True))
    return {"PASS": 0, "DRIFT": 2, "UNREACHABLE": 3}[result["status"]]


if __name__ == "__main__":
    raise SystemExit(main())
