"""Identity — token generation + hashing, shared auth error.

All raw tokens are generated with ``secrets.token_urlsafe`` (CSPRNG) and
stored ONLY as SHA-256 digests. Raw tokens are returned to the caller exactly
once (at creation) and never persisted, logged, or serialised.

Authoritative spec: docs/IDENTITY_DESIGN.md sections 4.1, 13.
"""

from __future__ import annotations

import hashlib
import secrets


class AuthError(Exception):
    """Authentication/token refusal.

    ``str(exc) == exc.args[0] == exc.code`` — the code is machine-readable
    (e.g. ``invalid_credentials``, ``invalid_token``, ``invalid_session``).
    The message never contains the raw password or raw token.
    """

    def __init__(self, code: str = "invalid_token"):
        super().__init__(code)
        self.code = code


def generate_token(prefix: str) -> str:
    """Return ``{prefix}_{token_urlsafe(32)}`` — a fresh opaque token.

    The caller receives the raw value exactly once; only
    :func:`hash_token` output is ever persisted.
    """
    return f"{prefix}_{secrets.token_urlsafe(32)}"


def hash_token(raw: str) -> str:
    """SHA-256 hex digest of a raw token — the ONLY form stored at rest."""
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()
