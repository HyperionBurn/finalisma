"""Cloud-plane tenancy — structural isolation at the storage boundary.

The danger in any multi-tenant system is a caller that forgets to pass a tenant
and silently reads across tenants. This module makes that impossible:

1. The storage interface requires ``tenant_id`` as a positional parameter on
   every tenant-owned method (no default) — a missing tenant raises ``TypeError``.
2. ``TenantContext`` is constructed ONCE per authenticated request and is the
   only way handlers reach the backend. Its ``require_tenant()`` guard rejects
   a wrong tenant before the backend call.
3. The backend scopes every query by ``WHERE tenant_id = ?`` — there is no
   cross-tenant read path.

Authoritative spec: docs/CLOUD_SPINE_DESIGN.md section 3.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any


class TenantIsolationError(Exception):
    """Raised when a caller attempts to access another tenant's data."""

    def __init__(self, message: str = "cross-tenant access denied"):
        super().__init__(message)
        self.code = "tenant_isolation_error"


@dataclass(frozen=True)
class TenantContext:
    """The single chokepoint: one per authenticated request.

    Handlers receive ``ctx: TenantContext`` — never a raw ``tenant_id``. The
    context carries the authenticated actor, the resolved plan, and the storage
    backend. Every storage call is made through this context's tenant.
    """

    tenant_id: str
    actor_id: str
    plan: Any          # PlanLimits — typed in quotas.py to avoid a cycle
    backend: Any       # StorageBackend — typed in storage.py to avoid a cycle

    def require_tenant(self, requested_tenant_id: str) -> None:
        """Guard: a handler may ONLY act within its own tenant.

        Rejects a wrong tenant before any backend call. There is no code path
        where a cross-tenant read is expressible without first passing this
        guard with the wrong tenant explicitly named.
        """
        if requested_tenant_id != self.tenant_id:
            raise TenantIsolationError("cross-tenant access denied")
