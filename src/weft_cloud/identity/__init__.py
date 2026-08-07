"""Weft cloud identity plane.

Wave G: accounts, sessions, orgs/roles, invites — all behind the Wave F
storage interface, stdlib-only. Authoritative spec: docs/IDENTITY_DESIGN.md.

The package exposes ``ensure_schema`` so integration contracts can guarantee
the identity tables exist on a real backend.
"""

from __future__ import annotations

from typing import Any

from .accounts import AccountStore
from .context import RoleError, SessionContext
from .invites import InviteStore
from .orgs import OrgStore
from .schema import ensure_schema
from .sessions import SessionStore
from .tokens import AuthError

__all__ = [
    "AuthError",
    "RoleError",
    "SessionContext",
    "AccountStore",
    "SessionStore",
    "OrgStore",
    "InviteStore",
    "ensure_schema",
]


def ensure_identity_schema(backend: Any) -> None:
    """Alias used by integration seams that reference the identity package."""
    ensure_schema(backend)
