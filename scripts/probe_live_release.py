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
import re
import sys
from http.client import HTTPException
from html.parser import HTMLParser
from urllib.error import HTTPError, URLError
from urllib.parse import urljoin, urlsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener

_MAX_BODY_BYTES = 2 * 1024 * 1024
_ALWAYS_HEADERS = (
    "strict-transport-security",
    "x-content-type-options",
    "referrer-policy",
    "x-frame-options",
    "permissions-policy",
)
_API_HEADERS = _ALWAYS_HEADERS
_LOGIN_HEADERS = _ALWAYS_HEADERS + ("content-security-policy",)
_SITE_HEADERS = _LOGIN_HEADERS
_MEDIA = {
    "site_demo_mp4": ("/assets/weft-demo.mp4", "video/mp4"),
    "site_demo_webm": ("/assets/weft-demo.webm", "video/webm"),
    "site_demo_captions": ("/assets/weft-demo.vtt", "text/vtt"),
    "site_demo_poster": ("/assets/weft-demo-poster.png", "image/png"),
}
_MANIFEST_MEDIA = (
    "weft-demo.mp4",
    "weft-demo.webm",
    "weft-demo.vtt",
    "weft-demo-poster.png",
)


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
                "content-length",
                "content-type",
                "location",
                "permissions-policy",
                "referrer-policy",
                "strict-transport-security",
                "x-content-type-options",
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


class _PageFacts(HTMLParser):
    """Collect only release-contract metadata; never expose page text."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.canonical: list[str] = []
        self.og_urls: list[str] = []
        self.og_images: list[str] = []
        self.og_site_names: list[str] = []
        self.og_titles: list[str] = []
        self.og_descriptions: list[str] = []
        self.og_image_alts: list[str] = []
        self.twitter_cards: list[str] = []
        self.twitter_titles: list[str] = []
        self.twitter_descriptions: list[str] = []
        self.twitter_images: list[str] = []
        self.twitter_image_alts: list[str] = []
        self.links: list[str] = []
        self.video_posters: list[str] = []
        self.sources: list[str] = []
        self.captions: list[str] = []
        self.caption_languages: list[str] = []
        self.caption_defaults: list[bool] = []

    def handle_starttag(self, tag: str, attrs) -> None:
        values = {name.lower(): value or "" for name, value in attrs}
        tag = tag.lower()
        if tag == "link" and "canonical" in values.get("rel", "").lower().split():
            if values.get("href"):
                self.canonical.append(values["href"])
        elif tag == "meta":
            property_name = values.get("property", "").lower()
            if property_name == "og:url" and values.get("content"):
                self.og_urls.append(values["content"])
            elif property_name == "og:image" and values.get("content"):
                self.og_images.append(values["content"])
            elif property_name == "og:site_name" and values.get("content"):
                self.og_site_names.append(values["content"])
            elif property_name == "og:title" and values.get("content"):
                self.og_titles.append(values["content"])
            elif property_name == "og:description" and values.get("content"):
                self.og_descriptions.append(values["content"])
            elif property_name == "og:image:alt" and values.get("content"):
                self.og_image_alts.append(values["content"])
            elif values.get("name", "").lower() == "twitter:card" and values.get("content"):
                self.twitter_cards.append(values["content"])
            elif values.get("name", "").lower() == "twitter:title" and values.get("content"):
                self.twitter_titles.append(values["content"])
            elif values.get("name", "").lower() == "twitter:description" and values.get("content"):
                self.twitter_descriptions.append(values["content"])
            elif values.get("name", "").lower() == "twitter:image" and values.get("content"):
                self.twitter_images.append(values["content"])
            elif values.get("name", "").lower() == "twitter:image:alt" and values.get("content"):
                self.twitter_image_alts.append(values["content"])
        elif tag == "a" and values.get("href"):
            self.links.append(values["href"])
        elif tag == "video":
            if values.get("poster"):
                self.video_posters.append(values["poster"])
        elif tag == "source" and values.get("src"):
            self.sources.append(values["src"])
        elif tag == "track" and "captions" in values.get("kind", "").lower().split():
            if values.get("src"):
                self.captions.append(values["src"])
                self.caption_languages.append(values.get("srclang", "").lower())
                self.caption_defaults.append("default" in values)


def _page_facts(response: dict) -> _PageFacts:
    facts = _PageFacts()
    try:
        facts.feed(response.get("body", ""))
        facts.close()
    except (TypeError, ValueError):
        pass
    return facts


def _site_asset(value: str, site_origin: str) -> bool:
    parsed = urlsplit(value)
    return (not parsed.scheme and not parsed.netloc and value.startswith("/")) or (
        value.startswith(f"{site_origin}/")
    )


def _metadata_ok(response: dict, expected_url: str, site_origin: str) -> bool:
    if response.get("status") != 200:
        return False
    facts = _page_facts(response)
    return (
        expected_url in facts.canonical
        and expected_url in facts.og_urls
        and facts.og_site_names == ["Weft"]
        and bool(facts.og_titles)
        and bool(facts.og_descriptions)
        and bool(facts.og_images)
        and all(_site_asset(image, site_origin) for image in facts.og_images)
        and bool(facts.og_image_alts)
        and facts.twitter_cards == ["summary_large_image"]
        and facts.twitter_titles == facts.og_titles
        and facts.twitter_descriptions == facts.og_descriptions
        and bool(facts.twitter_images)
        and all(_site_asset(image, site_origin) for image in facts.twitter_images)
        and bool(facts.twitter_image_alts)
    )


def _resolved_references(values: list[str], page_url: str) -> set[str]:
    """Resolve page-relative media references before comparing contracts."""

    return {urljoin(page_url, value) for value in values}


def _json_body(response: dict) -> dict | None:
    if response.get("status") != 200:
        return None
    try:
        value = json.loads(response.get("body", ""))
    except (TypeError, ValueError):
        return None
    return value if isinstance(value, dict) else None


def _sitemap_urls(response: dict, site_origin: str) -> list[str]:
    if response.get("status") != 200:
        return []
    return re.findall(
        rf"(?is)<loc>\s*({re.escape(site_origin)}/[^<\s]*)\s*</loc>",
        response.get("body", ""),
    )


def _manifest_ok(response: dict, site_origin: str, sitemap_response: dict) -> bool:
    manifest = _json_body(response)
    if manifest is None:
        return False
    media_hashes = manifest.get("media_sha256")
    sitemap_urls = _sitemap_urls(sitemap_response, site_origin)
    return (
        manifest.get("schema") == "weft.site-release/v1"
        and manifest.get("origin") == site_origin
        and isinstance(manifest.get("page_count"), int)
        and manifest["page_count"] > 0
        and isinstance(manifest.get("sitemap_url_count"), int)
        and manifest["sitemap_url_count"] > 0
        and manifest["sitemap_url_count"] <= manifest["page_count"]
        and manifest["sitemap_url_count"] == len(sitemap_urls)
        and manifest["page_count"] == len(sitemap_urls)
        and isinstance(media_hashes, dict)
        and all(
            isinstance(media_hashes.get(name), str)
            and re.fullmatch(r"[0-9a-fA-F]{64}", media_hashes[name])
            for name in _MANIFEST_MEDIA
        )
    )


def _media_ok(response: dict, expected_content_type: str) -> bool:
    content_type = response.get("headers", {}).get("content-type", "").split(";", 1)[0].strip().lower()
    return response.get("status") == 200 and response.get("bytes", 0) > 0 and content_type == expected_content_type


def _captions_ok(response: dict) -> bool:
    return _media_ok(response, "text/vtt") and response.get("body", "").lstrip().startswith("WEBVTT")


def _summary(response: dict) -> dict:
    """Remove body contents before the result is printed."""
    return {
        key: value
        for key, value in response.items()
        if key != "body"
    }


def _safe_location(value: object) -> str:
    """Keep only the origin and path of a redirect target."""
    parsed = urlsplit(str(value))
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        return "<redacted-location>"
    host = parsed.hostname
    if ":" in host and not host.startswith("["):
        host = f"[{host}]"
    try:
        port = parsed.port
    except ValueError:
        port = None
    if port:
        host = f"{host}:{port}"
    return f"{parsed.scheme}://{host}{parsed.path or '/'}"


def _safe_endpoint_facts(response: dict) -> dict:
    """Return operator-useful facts without exposing body or token details."""
    headers = response.get("headers", {})
    facts = {
        "status": response.get("status"),
        "content_type": headers.get("content-type", ""),
        "bytes": response.get("bytes", 0),
        "truncated": bool(response.get("truncated", False)),
    }
    if headers.get("location"):
        facts["location"] = _safe_location(headers["location"])
    if response.get("error"):
        facts["error"] = "transport_error"
    return facts


def _diagnostics(checks: dict[str, bool], endpoints: dict[str, dict]) -> list[dict]:
    """Explain failed checks using redacted endpoint facts only."""
    specs = (
        ("reachability", "API health and site home must return HTTP 200", ("api_health", "site_home")),
        ("api_health", "API health must return HTTP 200 with a status/ok marker", ("api_health",)),
        ("api_root_redirects_to_login", "API root must redirect to /login", ("api_root",)),
        ("api_security_headers", "API responses must include the required security headers", ("api_health", "api_root", "api_login", "api_signup")),
        ("api_signup_reachable", "API signup must return HTTP 200", ("api_signup",)),
        ("site_home_reachable", "Site home must return HTTP 200", ("site_home",)),
        ("site_docs_reachable", "Site docs must return HTTP 200", ("site_docs",)),
        ("site_quickstart_reachable", "Site quickstart must return HTTP 200", ("site_quickstart",)),
        ("site_demo_reachable", "Site demo must return HTTP 200", ("site_demo",)),
        ("site_404_reachable", "Site 404 route must return the expected edge status", ("site_404",)),
        ("site_release_markers", "Site home must contain canonical, og:url, and data-cohort-build markers", ("site_home",)),
        ("site_signup_cta_target", "Site home must link to the probed API signup origin", ("site_home", "api_signup")),
        ("site_canonical_og_metadata", "The home, docs, quickstart, and demo pages must expose aligned social metadata", ("site_home", "site_docs", "site_quickstart", "site_demo")),
        ("site_demo_captions_media", "The demo must reference and serve both video formats and default captions", ("site_demo", *tuple(_MEDIA))),
        ("site_404_content", "The 404 route must expose the branded missing-path message", ("site_404",)),
        ("site_indexing", "Robots and sitemap must point to the probed site origin", ("site_robots", "site_sitemap")),
        ("site_release_manifest", "The release manifest must align with the sitemap and media contract", ("site_manifest", "site_sitemap")),
        ("site_security_headers", "All site and media responses must include the required security headers", ("site_home", "site_docs", "site_quickstart", "site_demo", "site_404", "site_robots", "site_sitemap", "site_manifest", *tuple(_MEDIA))),
        ("release_alignment", "All hosted release-contract checks must pass together", tuple(endpoints)),
    )
    return [
        {
            "check": check,
            "expected": expected,
            "endpoints": {
                name: _safe_endpoint_facts(endpoints[name])
                for name in endpoint_names
            },
        }
        for check, expected, endpoint_names in specs
        if not checks.get(check, False)
    ]


def probe(api_origin: str, site_origin: str, timeout: float = 20.0) -> dict:
    api_origin = _origin(api_origin)
    site_origin = _origin(site_origin)

    endpoints = {
        "api_health": _fetch(f"{api_origin}/healthz", timeout),
        "api_root": _fetch(f"{api_origin}/", timeout),
        "api_login": _fetch(f"{api_origin}/login", timeout),
        "api_signup": _fetch(f"{api_origin}/signup", timeout),
        "site_home": _fetch(f"{site_origin}/", timeout),
        "site_docs": _fetch(f"{site_origin}/docs", timeout),
        "site_quickstart": _fetch(f"{site_origin}/docs/quickstart", timeout),
        "site_demo": _fetch(f"{site_origin}/demo", timeout),
        "site_404": _fetch(f"{site_origin}/404", timeout),
        "site_robots": _fetch(f"{site_origin}/robots.txt", timeout),
        "site_sitemap": _fetch(f"{site_origin}/sitemap.xml", timeout),
        "site_manifest": _fetch(f"{site_origin}/release-manifest.json", timeout),
    }
    for name, (path, _content_type) in _MEDIA.items():
        endpoints[name] = _fetch(f"{site_origin}{path}", timeout)

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
    api_headers_ok = (
        _has_headers(endpoints["api_health"], _API_HEADERS)
        and _has_headers(endpoints["api_root"], _API_HEADERS)
        and _has_headers(endpoints["api_login"], _LOGIN_HEADERS)
        and _has_headers(endpoints["api_signup"], _LOGIN_HEADERS)
    )
    site_home_ok = endpoints["site_home"]["status"] == 200
    site_docs_ok = endpoints["site_docs"]["status"] == 200
    site_quickstart_ok = endpoints["site_quickstart"]["status"] == 200
    site_demo_ok = endpoints["site_demo"]["status"] == 200
    # Vercel serves a direct /404 clean URL as the custom 404 document with
    # either a 200 or 404 status depending on the edge path; the branded body
    # is the invariant that distinguishes it from a generic fallback.
    site_404_ok = endpoints["site_404"]["status"] in {200, 404}
    site_release_markers_ok = all(
        (
            _has(endpoints["site_home"], marker)
            for marker in ('rel="canonical"', 'og:url', 'data-cohort-build')
        )
    )
    sitemap_urls = _sitemap_urls(endpoints["site_sitemap"], site_origin)
    site_indexing_ok = (
        endpoints["site_robots"]["status"] == 200
        and bool(
            re.search(
                rf"(?mi)^\s*Sitemap:\s*{re.escape(site_origin)}/sitemap\.xml\s*$",
                endpoints["site_robots"].get("body", ""),
            )
        )
        and endpoints["site_sitemap"]["status"] == 200
        and _has(endpoints["site_sitemap"], "<urlset")
        and bool(sitemap_urls)
        and all(url.startswith(f"{site_origin}/") for url in sitemap_urls)
    )
    site_manifest_ok = _manifest_ok(
        endpoints["site_manifest"], site_origin, endpoints["site_sitemap"]
    )
    expected_metadata = {
        "site_home": f"{site_origin}/",
        "site_docs": f"{site_origin}/docs/index.html",
        "site_quickstart": f"{site_origin}/docs/quickstart.html",
        "site_demo": f"{site_origin}/demo.html",
    }
    site_metadata_ok = all(
        _metadata_ok(endpoints[name], expected_url, site_origin)
        for name, expected_url in expected_metadata.items()
    )
    site_signup_cta_ok = f"{api_origin}/signup" in _page_facts(endpoints["site_home"]).links
    demo_facts = _page_facts(endpoints["site_demo"])
    demo_url = f"{site_origin}/demo.html"
    demo_sources = _resolved_references(demo_facts.sources, demo_url)
    demo_captions = _resolved_references(demo_facts.captions, demo_url)
    demo_posters = _resolved_references(demo_facts.video_posters, demo_url)
    site_demo_media_ok = (
        site_demo_ok
        and f"{site_origin}/assets/weft-demo.mp4" in demo_sources
        and f"{site_origin}/assets/weft-demo.webm" in demo_sources
        and f"{site_origin}/assets/weft-demo.vtt" in demo_captions
        and f"{site_origin}/assets/weft-demo-poster.png" in demo_posters
        and "en" in demo_facts.caption_languages
        and any(demo_facts.caption_defaults)
        and all(
            _captions_ok(endpoints[name]) if name == "site_demo_captions"
            else _media_ok(endpoints[name], content_type)
            for name, (_path, content_type) in _MEDIA.items()
        )
    )
    site_404_content_ok = (
        site_404_ok and _has(endpoints["site_404"], "This path is not in the account.")
    )
    site_headers_ok = all(
        _has_headers(endpoints[name], _SITE_HEADERS)
        for name in (
            "site_home",
            "site_docs",
            "site_quickstart",
            "site_demo",
            "site_404",
            "site_robots",
            "site_sitemap",
            "site_manifest",
            *_MEDIA,
        )
    )

    reachability_ok = api_health_ok and site_home_ok
    release_alignment_ok = (
        site_release_markers_ok
        and site_docs_ok
        and site_quickstart_ok
        and site_demo_ok
        and site_404_content_ok
        and site_metadata_ok
        and site_signup_cta_ok
        and endpoints["api_signup"]["status"] == 200
        and site_demo_media_ok
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
    checks = {
        "reachability": reachability_ok,
        "api_health": api_health_ok,
        "api_root_redirects_to_login": api_login_redirect_ok,
        "api_security_headers": api_headers_ok,
        "api_signup_reachable": endpoints["api_signup"]["status"] == 200,
        "site_home_reachable": site_home_ok,
        "site_docs_reachable": site_docs_ok,
        "site_quickstart_reachable": site_quickstart_ok,
        "site_demo_reachable": site_demo_ok,
        "site_404_reachable": site_404_ok,
        "site_release_markers": site_release_markers_ok,
        "site_signup_cta_target": site_signup_cta_ok,
        "site_canonical_og_metadata": site_metadata_ok,
        "site_demo_captions_media": site_demo_media_ok,
        "site_404_content": site_404_content_ok,
        "site_indexing": site_indexing_ok,
        "site_release_manifest": site_manifest_ok,
        "site_security_headers": site_headers_ok,
        "release_alignment": release_alignment_ok,
    }
    return {
        "probe": "weft-live-release-v1",
        "origins": {"api": api_origin, "site": site_origin},
        "status": status,
        "checks": checks,
        "diagnostics": _diagnostics(checks, endpoints),
        "endpoints": {name: _summary(response) for name, response in endpoints.items()},
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--api-origin",
        default=None,
        help="verified API origin, or set WEFT_API_ORIGIN",
    )
    parser.add_argument(
        "--site-origin",
        default=None,
        help="verified static-site origin, or set WEFT_SITE_URL",
    )
    parser.add_argument("--timeout", type=float, default=20.0)
    parser.add_argument("--pretty", action="store_true")
    args = parser.parse_args(argv)
    api_origin = (args.api_origin or os.environ.get("WEFT_API_ORIGIN", "")).strip()
    site_origin = (args.site_origin or os.environ.get("WEFT_SITE_URL", "")).strip()
    missing = []
    if not api_origin:
        missing.append("--api-origin or WEFT_API_ORIGIN")
    if not site_origin:
        missing.append("--site-origin or WEFT_SITE_URL")
    if missing:
        print(json.dumps({
            "probe": "weft-live-release-v1",
            "status": "INVALID_ARGUMENT",
            "error": "Explicit verified origins are required: " + ", ".join(missing) + ".",
        }))
        return 4
    try:
        result = probe(api_origin, site_origin, timeout=args.timeout)
    except ValueError as exc:
        print(json.dumps({"probe": "weft-live-release-v1", "status": "INVALID_ARGUMENT", "error": str(exc)}))
        return 4
    print(json.dumps(result, indent=2 if args.pretty else None, sort_keys=True))
    return {"PASS": 0, "DRIFT": 2, "UNREACHABLE": 3}[result["status"]]


if __name__ == "__main__":
    raise SystemExit(main())
