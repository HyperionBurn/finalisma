"""Validated public-origin configuration for customer-facing links."""

from __future__ import annotations

import os
from urllib.parse import urlsplit

DEFAULT_PUBLIC_ORIGIN = "http://127.0.0.1:18788"


def normalize_origin(value: str | None, *, default: str) -> str:
    """Return a safe absolute HTTP(S) origin or raise ``ValueError``.

    Origins are used to construct bearer-bearing URLs. Paths, credentials,
    queries, fragments, control characters, and non-HTTP schemes are never
    accepted because a typo must not redirect a credential to an unintended
    destination.
    """
    candidate = (value or "").strip()
    if not candidate:
        return default
    if any(ch.isspace() or ord(ch) < 32 or ord(ch) == 127 for ch in candidate):
        raise ValueError("public origin must not contain whitespace or control characters")
    parsed = urlsplit(candidate)
    if parsed.scheme.lower() not in {"http", "https"} or not parsed.netloc:
        raise ValueError("public origin must be an absolute http(s) URL")
    if parsed.username is not None or parsed.password is not None:
        raise ValueError("public origin must not contain userinfo")
    if parsed.path not in ("", "/") or parsed.query or parsed.fragment:
        raise ValueError("public origin must not contain a path, query, or fragment")
    if parsed.hostname is None:
        raise ValueError("public origin must contain a host")
    try:
        parsed.port
    except ValueError as exc:
        raise ValueError("public origin contains an invalid port") from exc
    return f"{parsed.scheme.lower()}://{parsed.netloc}".rstrip("/")


def configured_origin(
    explicit: str | None = None,
    *,
    env_names: tuple[str, ...] = ("WEFT_PUBLIC_ORIGIN",),
    default: str = DEFAULT_PUBLIC_ORIGIN,
) -> str:
    """Resolve an explicit value, then the first nonblank environment value."""
    if explicit is not None:
        return normalize_origin(explicit, default=default)
    for name in env_names:
        raw = os.environ.get(name)
        if raw is not None and raw.strip():
            return normalize_origin(raw, default=default)
    return default
