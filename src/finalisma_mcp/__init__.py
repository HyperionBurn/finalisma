"""Finalisma: a small, interoperable agent-to-agent MCP bridge."""

from .core import (
    FINALISMA_PROTOCOL,
    FINALISMA_VERSION,
    MCP_PROTOCOL_VERSION,
    FinalismaError,
    FinalismaStore,
)

__all__ = [
    "FINALISMA_PROTOCOL",
    "FINALISMA_VERSION",
    "MCP_PROTOCOL_VERSION",
    "FinalismaError",
    "FinalismaStore",
]
