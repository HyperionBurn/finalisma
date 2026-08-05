"""Finalisma SDK — stdlib-only Python client for the Finalisma A2A protocol."""

from .client import (
    FinalismaClient,
    FinalismaError,
    AuthError,
    EvidenceError,
    NotFoundError,
    ConflictError,
    TimeoutError,
    TaskResult,
    PairingResult,
    JoinResult,
    SessionEvent,
    CredentialRotation,
)

__all__ = [
    "FinalismaClient",
    "FinalismaError",
    "AuthError",
    "EvidenceError",
    "NotFoundError",
    "ConflictError",
    "TimeoutError",
    "TaskResult",
    "PairingResult",
    "JoinResult",
    "SessionEvent",
    "CredentialRotation",
]
