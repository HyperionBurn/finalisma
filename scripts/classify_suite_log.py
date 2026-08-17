#!/usr/bin/env python3
"""Turn a captured `python -m unittest` run into one of three verdicts.

Why this exists (FAILURE 3, real incident): final-verify.sh used to pipe the
suite through `| tail -4` and treat an EMPTY capture as failure. A harness
problem (python off PATH, the process killed early, a lost pipe, a timeout
with no output yet) was then indistinguishable from genuinely broken tests,
and the gate printed "DO NOT DEPLOY" for a run that never actually happened.
That blocked a legitimate deploy and destroyed the evidence needed to tell
the two situations apart, because nothing was written to disk.

The fix has three parts, all enforced here:

1. Three outcomes, not two.

   - PASS               — the harness ran to completion and unittest reported
                           success.
   - REAL FAILURE        — the harness ran to completion and unittest reported
                           failing/erroring tests.
   - INCONCLUSIVE-HARNESS — we cannot tell, because the run never produced a
                           `Ran N tests` line at all (crash, timeout before
                           output, python missing, discovery found nothing).
                           This is never silently folded into either PASS or
                           REAL FAILURE.

2. Never infer a verdict from an empty capture. The caller is responsible for
   writing the *full* stdout+stderr to a log file (not a tail'd pipe); this
   module only classifies text that is handed to it, and treats "no evidence
   of a completed run" as INCONCLUSIVE, not as failure.

3. Check exit codes, never parse output when an exit code exists. Once we
   know the harness actually completed (a `Ran N tests` line is present), the
   process exit code — not a regex for the word "OK" — decides PASS vs REAL
   FAILURE. Exit-code text like "OK"/"FAILED" is only a fallback for callers
   that truly cannot supply an exit code.

Usage as a library (this is what tests/test_deploy_gate.py exercises)::

    from classify_suite_log import classify
    verdict = classify(log_text, exit_code=0)
    verdict.label        # "PASS" | "REAL FAILURE" | "INCONCLUSIVE-HARNESS"
    verdict.process_exit  # 0 | 1 | 2  (what the calling script should exit with)
    verdict.test_count    # int | None

Usage as a CLI (this is what final-verify.sh and suite-check.sh call)::

    python3 classify_suite_log.py <log-file> [<exit-code>]

Prints one of:

    VERDICT: PASS — Ran 525 tests, OK
    VERDICT: REAL FAILURE — Ran 525 tests, 2 failing/erroring
    VERDICT: INCONCLUSIVE-HARNESS — no 'Ran N tests' line in <log-file>

and exits 0 / 1 / 2 respectively, so the calling shell script can branch on
`$?` alone.
"""

from __future__ import annotations

import re
import sys
from dataclasses import dataclass
from pathlib import Path

PASS = "PASS"
REAL_FAILURE = "REAL FAILURE"
INCONCLUSIVE = "INCONCLUSIVE-HARNESS"

_RAN_RE = re.compile(r"^Ran (\d+) tests? in", re.MULTILINE)
_OK_RE = re.compile(r"^OK\b", re.MULTILINE)
_FAILED_RE = re.compile(r"^(FAILED|ERROR)\b", re.MULTILINE)


@dataclass(frozen=True)
class Verdict:
    label: str
    process_exit: int
    test_count: "int | None"
    reason: str

    @property
    def is_pass(self) -> bool:
        return self.label == PASS


def classify(log_text: str, exit_code: "int | None" = None) -> Verdict:
    """Classify a captured unittest run. See module docstring for the rules."""
    matches = _RAN_RE.findall(log_text or "")
    if not matches:
        return Verdict(
            INCONCLUSIVE,
            2,
            None,
            "no 'Ran N tests' line found — the harness did not produce evidence "
            "of a completed run (crash, timeout, python missing, or a lost pipe)",
        )

    # unittest prints one "Ran N tests" line per invocation; the last one wins
    # if a caller ever concatenates retries into the same log.
    n = int(matches[-1])

    if n == 0:
        return Verdict(
            INCONCLUSIVE,
            2,
            0,
            "'Ran 0 tests' — discovery found nothing, which means -s/-p or "
            "PYTHONPATH is almost certainly misconfigured, not that the suite "
            "is empty on purpose",
        )

    if exit_code is not None:
        # Exit codes, never parsed output, once we know a real run happened.
        if exit_code == 0:
            return Verdict(PASS, 0, n, f"Ran {n} tests, exit code 0")
        return Verdict(REAL_FAILURE, 1, n, f"Ran {n} tests, exit code {exit_code}")

    # No exit code available (e.g. classifying a log file after the fact,
    # with the process already gone) — fall back to the textual markers.
    if _OK_RE.search(log_text) and not _FAILED_RE.search(log_text):
        return Verdict(PASS, 0, n, f"Ran {n} tests, 'OK' present, no exit code supplied")
    return Verdict(REAL_FAILURE, 1, n, f"Ran {n} tests, no 'OK' line, no exit code supplied")


def _main(argv: "list[str]") -> int:
    if not argv:
        print("usage: classify_suite_log.py <log-file> [<exit-code>]", file=sys.stderr)
        return 2
    log_path = Path(argv[0])
    exit_code = None
    if len(argv) > 1 and argv[1] != "":
        try:
            exit_code = int(argv[1])
        except ValueError:
            print(f"warning: '{argv[1]}' is not a valid exit code, ignoring", file=sys.stderr)

    try:
        text = log_path.read_text(encoding="utf-8", errors="replace")
    except OSError as exc:
        print(f"VERDICT: {INCONCLUSIVE} — could not read {log_path}: {exc}")
        return 2

    verdict = classify(text, exit_code)
    if verdict.label == PASS:
        print(f"VERDICT: {verdict.label} — {verdict.reason}")
    elif verdict.label == REAL_FAILURE:
        print(f"VERDICT: {verdict.label} — {verdict.reason}")
        for line in text.splitlines():
            if line.startswith(("FAIL:", "ERROR:")):
                print(f"  {line}")
    else:
        print(f"VERDICT: {verdict.label} — {verdict.reason}")
        print(f"  full output: {log_path}")
    return verdict.process_exit


if __name__ == "__main__":
    raise SystemExit(_main(sys.argv[1:]))
