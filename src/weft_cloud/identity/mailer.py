"""Identity — pluggable mailer interface + Stage-1 outbox + Stage-2 SMTP mailer.

Stage 1 (this wave): ``LocalOutboxMailer`` writes the email to
``cloud_identity_outbox`` instead of sending. A Stage-2 worker (see
``outbox_worker.py``) drains the same outbox via a real provider (Postmark,
SES) — or, when SMTP settings are present in the environment, via
``SmtpMailer``. Callers depend only on the ``Mailer`` ABC, never on a concrete
provider.

Selection: ``build_mailer`` picks ``SmtpMailer`` when ``FINALISMA_SMTP_HOST`` is
set (STARTTLS + AUTH on the submission port) and falls back to
``LocalOutboxMailer`` — so local development and the test suite never need a
mail server. A partially-set SMTP configuration fails loudly at startup naming
the missing variable; a missing configuration is not an error.

Authoritative spec: docs/IDENTITY_DESIGN.md sections 5.1, 5.4.
"""

from __future__ import annotations

import os
import smtplib
import uuid
from abc import ABC, abstractmethod
from email.message import EmailMessage
from typing import Any, Mapping

from weft_cloud.storage import utc_now_iso

_SMTP_HOST_ENV = "FINALISMA_SMTP_HOST"
_SMTP_PORT_ENV = "FINALISMA_SMTP_PORT"
_SMTP_USERNAME_ENV = "FINALISMA_SMTP_USERNAME"
_SMTP_PASSWORD_ENV = "FINALISMA_SMTP_PASSWORD"
_SMTP_FROM_ENV = "FINALISMA_SMTP_FROM"


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


class SmtpMailer(Mailer):
    """Stage-2 mailer: delivers over SMTP with STARTTLS + AUTH.

    Standard library only (``smtplib`` + ``email.message``). ``smtp_factory``
    is injectable so tests can substitute an in-memory SMTP double; the default
    is ``smtplib.SMTP``. A real connection is never opened unless the caller
    passes a real factory.
    """

    def __init__(
        self,
        *,
        host: str,
        port: int = 587,
        username: str | None = None,
        password: str | None = None,
        from_addr: str,
        use_starttls: bool = True,
        timeout: float = 15.0,
        smtp_factory: Any = smtplib.SMTP,
    ) -> None:
        self.host = host
        self.port = port
        self.username = username
        self.password = password
        self.from_addr = from_addr
        self.use_starttls = use_starttls
        self.timeout = timeout
        self._smtp_factory = smtp_factory

    def send(self, tenant_id: str, to_email: str, subject: str, body: str) -> None:
        message = EmailMessage()
        message["From"] = self.from_addr
        message["To"] = to_email
        message["Subject"] = subject
        message.set_content(body)
        with self._smtp_factory(self.host, self.port, timeout=self.timeout) as smtp:
            if self.use_starttls:
                smtp.starttls()
            if self.username is not None:
                smtp.login(self.username, self.password)
            smtp.send_message(message)


def _parse_port(name: str, raw: str | None, default: int) -> int:
    if raw is None or raw == "":
        return default
    try:
        value = int(raw)
    except (TypeError, ValueError):
        raise ValueError(f"{name} must be an integer, got {raw!r}")
    if not (1 <= value <= 65535):
        raise ValueError(f"{name} must be in 1..65535, got {value}")
    return value


def smtp_config_from_env(environ: Mapping[str, str] | None = None) -> dict[str, Any] | None:
    """Parse SMTP settings from the environment into a config dict.

    Returns ``None`` when SMTP is disabled (no ``FINALISMA_SMTP_HOST``) — the
    caller keeps the Stage-1 outbox mailer. When the host IS set, every other
    setting is required and a missing one raises ``ValueError`` naming the
    variable, so a half-configured deploy fails loudly at startup instead of
    failing every send at runtime.
    """
    environ = os.environ if environ is None else environ
    host = (environ.get(_SMTP_HOST_ENV) or "").strip()
    if not host:
        return None
    port = _parse_port(_SMTP_PORT_ENV, environ.get(_SMTP_PORT_ENV), 587)
    username = (environ.get(_SMTP_USERNAME_ENV) or "").strip()
    password = environ.get(_SMTP_PASSWORD_ENV) or ""
    from_addr = (environ.get(_SMTP_FROM_ENV) or "").strip()
    for var, value in (
        (_SMTP_USERNAME_ENV, username),
        (_SMTP_PASSWORD_ENV, password),
        (_SMTP_FROM_ENV, from_addr),
    ):
        if not value:
            raise ValueError(
                f"{var} is required when {_SMTP_HOST_ENV} is set"
            )
    return {
        "host": host,
        "port": port,
        "username": username,
        "password": password,
        "from_addr": from_addr,
    }


def build_mailer(backend: Any, environ: Mapping[str, str] | None = None) -> Mailer:
    """Select the mailer at startup: SMTP when configured, else the local outbox.

    With no SMTP settings this returns ``LocalOutboxMailer`` and behaviour is
    byte-for-byte unchanged. With SMTP settings present it returns
    ``SmtpMailer`` pre-loaded with the parsed host/port/credentials/from-address.
    """
    config = smtp_config_from_env(environ)
    if config is None:
        return LocalOutboxMailer(backend)
    return SmtpMailer(**config)
