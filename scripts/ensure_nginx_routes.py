#!/usr/bin/env python3
"""Ensure the selected nginx site has the public Weft routes.

This helper edits one explicitly selected site file. It does not choose a file
from sites-enabled and it does not run nginx -t or reload nginx. The caller
owns backup, syntax validation, and reload/rollback sequencing.
"""

from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path


# Accept an existing prefix route with or without the optional `^~` modifier.
# The nginx site may already have a valid `location /j/` block; adding a second
# block with `^~` is invalid and can make nginx reject the whole config.
JOIN_PATTERN = re.compile(r"location\s+(?:\^~\s+)?/j/\s*\{")
MCP_EXACT_PATTERN = re.compile(r"location\s*=\s*/mcp\s*\{")
MCP_PREFIX_PATTERN = re.compile(r"location\s+/mcp\s*\{")
SERVER_NAME_PATTERN = re.compile(r"\bserver_name\s+([^;]+);", re.MULTILINE)
ROOT_LOCATION_PATTERN = re.compile(
    r"(?ms)^(?P<indent>[ \t]*)location[ \t]+/[ \t]*\{\n"
    r"(?P<body>[^{}]*?)^(?P=indent)\}"
)

JOIN_BLOCK = (
    "    location ^~ /j/ {\n"
    "        proxy_pass http://127.0.0.1:18788;\n"
    "        proxy_set_header Host $host;\n"
    "        proxy_set_header X-Forwarded-Proto $scheme;\n"
    "        proxy_read_timeout 60s;\n"
    "        proxy_buffering off;\n"
    "    }\n"
)
MCP_BLOCK = (
    "    location = /mcp {\n"
    "        proxy_pass http://127.0.0.1:18788;\n"
    "        proxy_set_header Host $host;\n"
    "        proxy_set_header X-Forwarded-Proto $scheme;\n"
    "        proxy_read_timeout 300s;\n"
    "        proxy_buffering off;\n"
    "    }\n"
)


def _web_proxy_block(indent: str) -> str:
    return (
        f"{indent}location / {{\n"
        f"{indent}    proxy_pass http://127.0.0.1:18789;\n"
        f"{indent}    proxy_set_header Host $host;\n"
        f"{indent}    proxy_set_header X-Forwarded-Proto $scheme;\n"
        f"{indent}    proxy_read_timeout 60s;\n"
        f"{indent}}}"
    )


def _ensure_web_catch_all(text: str) -> tuple[str, bool]:
    """Ensure every selected server's catch-all serves the customer web app.

    A static-site catch-all can make ``/signup`` return a polished 404 while
    health and MCP remain green. Only replace the known static-site shape. A
    different catch-all fails closed instead of overwriting an operator's
    unrelated routing.
    """
    matches = list(ROOT_LOCATION_PATTERN.finditer(text))
    if not matches:
        raise ValueError("could not find a safe location / insertion point for web app routing")
    changed = False

    def replace(match: re.Match[str]) -> str:
        nonlocal changed
        body = match.group("body")
        if re.search(r"\bproxy_pass\s+http://127\.0\.0\.1:18789\s*;", body):
            return match.group(0)
        if "root /opt/weft/site" in body or re.search(r"\btry_files\b", body):
            changed = True
            return _web_proxy_block(match.group("indent"))
        raise ValueError(
            "catch-all location / does not proxy to the Weft web app on port 18789"
        )

    updated = ROOT_LOCATION_PATTERN.sub(replace, text)
    return updated, changed


def _has_server_name(text: str, expected: str) -> bool:
    active = "\n".join(line.split("#", 1)[0] for line in text.splitlines())
    return any(expected in match.group(1).split() for match in SERVER_NAME_PATTERN.finditer(active))


def _insert_before_catch_all(text: str, block: str, label: str) -> str:
    match = re.search(r"(?m)^[ \t]*location[ \t]+/[ \t]*\{", text)
    if match is None:
        raise ValueError(f"could not find a safe location / insertion point for {label}")
    return text[:match.start()] + block + text[match.start():]


def ensure_routes(path: Path, server_name: str) -> bool:
    """Ensure routes in path and return whether the file changed."""

    if not path.is_file():
        raise ValueError(f"nginx site file does not exist: {path}")
    text = path.read_text(encoding="utf-8")
    if not _has_server_name(text, server_name):
        raise ValueError(f"{server_name!r} is not declared by {path}")

    updated, root_changed = _ensure_web_catch_all(text)
    if not JOIN_PATTERN.search(updated):
        updated = _insert_before_catch_all(updated, JOIN_BLOCK, "/j/")

    if not MCP_EXACT_PATTERN.search(updated):
        prefix = MCP_PREFIX_PATTERN.search(updated)
        if prefix:
            updated = updated[:prefix.start()] + "location = /mcp {" + updated[prefix.end():]
        else:
            updated = _insert_before_catch_all(updated, MCP_BLOCK, "= /mcp")

    if updated != text or root_changed:
        path.write_text(updated, encoding="utf-8")
        return True
    return False


def _main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, required=True)
    parser.add_argument("--server-name", required=True)
    args = parser.parse_args(argv)
    try:
        changed = ensure_routes(args.config, args.server_name)
    except (OSError, ValueError) as exc:
        print(f"FATAL: {exc}", file=sys.stderr)
        return 1
    print("  /j/ and exact /mcp routes ensured" if changed else "  explicit /j/ and /mcp routes already present")
    return 0


if __name__ == "__main__":
    raise SystemExit(_main(sys.argv[1:]))
