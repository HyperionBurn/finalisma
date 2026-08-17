"""Weft: a small, interoperable agent-to-agent MCP bridge."""

from .core import (
    WEFT_PROTOCOL,
    WEFT_VERSION,
    MCP_PROTOCOL_VERSION,
    WeftError,
    WeftStore,
)

__all__ = [
    "WEFT_PROTOCOL",
    "WEFT_VERSION",
    "MCP_PROTOCOL_VERSION",
    "WeftError",
    "WeftStore",
]
