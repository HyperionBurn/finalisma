#!/usr/bin/env python3
"""Check that a release test-count baseline does not decrease unexpectedly.

The release gate compares the committed baseline in the candidate ref with
the same file at ``origin/main``. A decrease is rejected unless the operator
supplies the exact override token and a short, human-readable reason. The
caller prints the returned status, so an accepted exception remains visible
in the gate transcript instead of silently weakening the ratchet.
"""

from __future__ import annotations

import argparse
import re
import sys
from dataclasses import dataclass


PASS = "PASS"
OVERRIDE = "OVERRIDE"
FAIL = "FAIL"
ERROR = "ERROR"
MAX_REASON_LENGTH = 240


@dataclass(frozen=True)
class RatchetResult:
    """Outcome of comparing a candidate baseline with ``origin/main``."""

    status: str
    current: int
    origin_main: int
    reason: str
    detail: str

    @property
    def allowed(self) -> bool:
        return self.status in {PASS, OVERRIDE}


def _valid_positive_count(value: int) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and value > 0


def _clean_reason(reason: str) -> str:
    if not isinstance(reason, str):
        return ""
    return reason.strip()


def _valid_reason(reason: str) -> bool:
    cleaned = _clean_reason(reason)
    if not cleaned or len(cleaned) > MAX_REASON_LENGTH:
        return False
    # A reason is copied into the gate transcript. Reject line breaks and
    # control characters so an override cannot forge additional log records.
    return not any(ord(character) < 0x20 or ord(character) == 0x7F for character in cleaned)


def evaluate(
    current: int,
    origin_main: int,
    *,
    override: str = "",
    reason: str = "",
) -> RatchetResult:
    """Return whether ``current`` is allowed against ``origin_main``.

    Equal or higher values pass normally. A lower value passes only with the
    exact override value ``"1"`` and a bounded, printable reason. Invalid
    counts are programmer/configuration errors and are reported to the CLI as
    ``ERROR`` rather than being treated as an allowed decrease.
    """
    if not _valid_positive_count(current) or not _valid_positive_count(origin_main):
        raise ValueError("baseline counts must be positive integers")

    cleaned_reason = _clean_reason(reason)
    if current >= origin_main:
        return RatchetResult(
            PASS,
            current,
            origin_main,
            cleaned_reason,
            "candidate baseline is not below origin/main",
        )

    if override != "1":
        return RatchetResult(
            FAIL,
            current,
            origin_main,
            cleaned_reason,
            "candidate baseline decreased; exact override token is required",
        )
    if not _valid_reason(cleaned_reason):
        return RatchetResult(
            FAIL,
            current,
            origin_main,
            cleaned_reason,
            f"override requires a printable reason of 1-{MAX_REASON_LENGTH} characters",
        )
    return RatchetResult(
        OVERRIDE,
        current,
        origin_main,
        cleaned_reason,
        "candidate baseline decrease explicitly overridden",
    )


def _format_result(result: RatchetResult) -> str:
    # Keep the transcript one physical line even when a rejected override
    # contains control characters; the reason is operator input, not trusted
    # gate syntax. Truncation bounds the output from a hostile environment.
    safe_reason = re.sub(r"[\x00-\x1F\x7F]", "?", result.reason)[:MAX_REASON_LENGTH]
    suffix = f" reason={safe_reason}" if safe_reason else ""
    return (
        f"{result.status} current={result.current} origin/main={result.origin_main}"
        f" detail={result.detail}{suffix}"
    )


def _main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("current", type=int)
    parser.add_argument("origin_main", type=int)
    parser.add_argument("--override", default="")
    parser.add_argument("--reason", default="")
    args = parser.parse_args(argv)
    try:
        result = evaluate(
            args.current,
            args.origin_main,
            override=args.override,
            reason=args.reason,
        )
    except ValueError as exc:
        print(f"{ERROR} detail={exc}")
        return 2
    print(_format_result(result))
    return 0 if result.allowed else 1


if __name__ == "__main__":
    raise SystemExit(_main(sys.argv[1:]))
