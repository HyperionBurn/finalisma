"""Finalisma cloud spine — the hosted-service plane.

Wraps the coordinator's data as a multi-tenant service. Owns the storage
interface (swappable engine), structural tenancy, plan-driven quotas, rate
limits, migrations, and a durable event/outbox mirror. v1 runs on stdlib
SQLite-WAL; Postgres is a later scale decision behind the same interface.

Authoritative spec: docs/CLOUD_SPINE_DESIGN.md.
"""

from .storage import StorageBackend, StorageTransaction, SqliteWalBackend
from .tenancy import TenantContext, TenantIsolationError
from .quotas import PlanLimits, PLANS, QuotaError

__all__ = [
    "StorageBackend",
    "StorageTransaction",
    "SqliteWalBackend",
    "TenantContext",
    "TenantIsolationError",
    "PlanLimits",
    "PLANS",
    "QuotaError",
]
