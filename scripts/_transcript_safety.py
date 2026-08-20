"""Credential-safe transcript helpers for local interop drivers."""

from __future__ import annotations

import json
import re


_CREDENTIAL_PREFIX_RE = re.compile(
    r"\b(?:fst_actor|fst_pair|fst_session|agk|rm|whsec)_[A-Za-z0-9_-]+"
)


def redact_text(value: str) -> str:
    return _CREDENTIAL_PREFIX_RE.sub("***", value)


def _redact_json(value: object) -> object:
    if isinstance(value, dict):
        return {
            name: ("***" if (name == "token" or name.endswith("_token")) and isinstance(item, str) else _redact_json(item))
            for name, item in value.items()
        }
    if isinstance(value, list):
        return [_redact_json(item) for item in value]
    if isinstance(value, str):
        return redact_text(value)
    return value


def redact_transcript_line(line: str) -> str:
    try:
        parsed = json.loads(line)
    except json.JSONDecodeError:
        return redact_text(line)
    return json.dumps(_redact_json(parsed), ensure_ascii=False, separators=(",", ":"))
