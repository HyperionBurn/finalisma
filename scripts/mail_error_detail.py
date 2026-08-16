#!/usr/bin/env python3
"""Turn a mail-send exception into structured failure detail.

Why this exists: the real production outbox recorded `last_error` as the
literal string `"permanent"` for all 104 failed rows, with no SMTP code and
no reason. When real delivery breaks, nobody can diagnose it from that —
every failure looks identical whether the cause was a bad recipient, an
auth failure, a rate limit, or the SMTP host being unreachable.

This module classifies a caught exception from a send attempt into a small,
structured, JSON-serializable record instead of a flattened string:

    {"category": "recipient_refused", "smtp_code": 550,
     "reason": "Mailbox unavailable", "retryable": False}

Rules, both enforced and covered by tests/test_deploy_ops.py:

- Never includes the email body or subject. Callers must not pass them in —
  there is no parameter for it, so it is structurally impossible to leak.
- Never includes credentials. Nothing here touches SMTP auth material.
- `retryable` follows RFC 5321 convention: 4xx codes are transient
  (retryable), 5xx codes are permanent (not retryable). Errors with no SMTP
  code (connection/timeout/DNS failures) are treated as retryable, since
  those are almost always transient infrastructure issues.

Status in this repository: the outbox in this codebase
(`finalisma_cloud.identity.mailer.LocalOutboxMailer`) is Stage 1 — it writes
to `cloud_identity_outbox` and never actually sends, per its own docstring
("A future worker (Stage 2) drains the same outbox via a real provider").
There is therefore no live SMTP send path in this tree to attach this to yet.
This module is the ready-to-wire replacement for the flattened-string bug,
provided now so Stage 2 does not have to reinvent it, not a claim that
Stage 2 has been patched here.

Usage::

    from mail_error_detail import classify_mail_error
    try:
        smtp_conn.sendmail(...)
    except Exception as exc:
        detail = classify_mail_error(exc)
        # store detail.to_json() as last_error instead of "permanent"
"""

from __future__ import annotations

import json
import re
from dataclasses import asdict, dataclass


@dataclass(frozen=True)
class MailErrorDetail:
    category: str
    smtp_code: "int | None"
    reason: str
    retryable: bool

    def to_json(self) -> str:
        return json.dumps(asdict(self), sort_keys=True)


def _category_for(exc: BaseException) -> str:
    name = type(exc).__name__
    mapping = {
        "SMTPRecipientsRefused": "recipient_refused",
        "SMTPSenderRefused": "sender_refused",
        "SMTPDataError": "data_rejected",
        "SMTPHeloError": "helo_rejected",
        "SMTPAuthenticationError": "auth_failed",
        "SMTPNotSupportedError": "extension_unsupported",
        "SMTPConnectError": "connect_failed",
        "SMTPServerDisconnected": "server_disconnected",
        "SMTPResponseException": "smtp_error",
        "TimeoutError": "timeout",
        "socket.timeout": "timeout",
        "ConnectionRefusedError": "connect_refused",
        "OSError": "network_error",
        "gaierror": "dns_error",
    }
    return mapping.get(name, "unknown")


def _extract_smtp_code(exc: BaseException) -> "int | None":
    """Best-effort extraction of an SMTP status code from common smtplib
    exception shapes, without importing smtplib (keeps this dependency-free
    and testable with plain fakes that only need to look like the real
    exceptions, not subclass them)."""
    code = getattr(exc, "smtp_code", None)
    if isinstance(code, int):
        return code

    # SMTPRecipientsRefused carries {recipient: (code, message)}.
    recipients = getattr(exc, "recipients", None)
    if isinstance(recipients, dict) and recipients:
        first = next(iter(recipients.values()))
        if isinstance(first, tuple) and first and isinstance(first[0], int):
            return first[0]

    # Fall back to sniffing a leading 3-digit code out of the message text,
    # e.g. "550 5.1.1 Mailbox unavailable".
    text = str(exc)
    m = re.match(r"\s*\(?(\d{3})[\s,)]", text)
    if m:
        return int(m.group(1))
    return None


def _reason_text(exc: BaseException) -> str:
    """A short, safe-to-store reason string. Truncated defensively — this
    must never become a place to accidentally dump a message body that got
    embedded in an exception by a misbehaving library."""
    text = str(exc).strip() or type(exc).__name__
    text = " ".join(text.split())  # collapse newlines/whitespace
    return text[:200]


def classify_mail_error(exc: BaseException) -> MailErrorDetail:
    """Classify a send-time exception into structured, storable detail."""
    smtp_code = _extract_smtp_code(exc)
    category = _category_for(exc)

    if smtp_code is not None:
        retryable = 400 <= smtp_code < 500
    else:
        # No SMTP code at all means we never got a protocol-level reply —
        # that is a connection/timeout/DNS class of problem, which is almost
        # always transient infrastructure, not a permanently bad recipient.
        retryable = category not in ("recipient_refused", "sender_refused", "data_rejected", "auth_failed")

    return MailErrorDetail(
        category=category,
        smtp_code=smtp_code,
        reason=_reason_text(exc),
        retryable=retryable,
    )
