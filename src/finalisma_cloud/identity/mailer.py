"""Identity — pluggable mailer interface + Stage-1 local outbox mailer.

Stage 1 (this wave): ``LocalOutboxMailer`` writes the email to
``cloud_identity_outbox`` instead of sending. A future worker (Stage 2) drains
the same outbox via a real provider (Postmark, SES). Callers depend only on the
``Mailer`` ABC, never on a concrete provider.

Authoritative spec: docs/IDENTITY_DESIGN.md sections 5.1, 5.4.
"""

from __future__ import annotations

import uuid
from abc import ABC, abstractmethod
from typing import Any

from finalisma_cloud.storage import utc_now_iso


def _new_id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex}"


class Mailer(ABC):
    """Pluggable email delivery. Stage 1: local outbox (no send)."""

    @abstractmethod
    def send(self, tenant_id: str, to_email: str, subject: str, body: str) -> None:
        ...


class LocalOutboxMailer(Mailer):
    """Writes the email to ``cloud_identity_outbox``; never actually sends.

    The outbox row is durable (survives a crash) and a Stage-2 worker can
    drain it through a real provider without touching callers.
    """

    def __init__(self, backend: Any) -> None:
        self.backend = backend

    def send(self, tenant_id: str, to_email: str, subject: str, body: str) -> None:
        entry_id = _new_id("idem")
        now_iso = utc_now_iso()
        with self.backend.transaction() as tx:
            tx.execute(
                "INSERT INTO cloud_identity_outbox(entry_id, tenant_id, to_email, subject, body, created_at) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (entry_id, tenant_id, to_email, subject, body, now_iso),
            )
            tx.commit()
