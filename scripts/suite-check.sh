#!/usr/bin/env bash
# Standalone, robust suite runner. PASS / REAL FAILURE / INCONCLUSIVE-HARNESS,
# never a guess from an empty capture.
#
# FAILURE 3 (real incident): the old version of this check piped
# `timeout 900 python -m unittest ... | tail -4` and inferred failure from an
# empty capture. Silence and failure looked identical, so a harness problem
# (python not on PATH, an early kill, a slow run) reported "DO NOT DEPLOY"
# with nothing to diagnose. This writes the FULL output to a file, checks the
# process exit code (never parses "OK"/"FAILED" text once an exit code
# exists), and reports one of three distinct verdicts via
# scripts/classify_suite_log.py so the two callers of that logic (this file
# and final-verify.sh) cannot drift apart.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
cd "$REPO_ROOT"

PYTHON="${PYTHON:-python}"
LOG="${1:-${TMPDIR:-/tmp}/weft-suite.log}"

echo "  repo        : $REPO_ROOT"
echo "  python      : $(command -v "$PYTHON" || echo 'NOT FOUND')"
"$PYTHON" -V 2>&1 | sed 's/^/  version     : /' || true

echo "  running full suite (PYTHONPATH=src, tests/) ..."
set +e
PYTHONPATH=src "$PYTHON" -B -m unittest discover -s tests -q >"$LOG" 2>&1
rc=$?
set -e

echo "  exit code   : $rc"
echo "  last lines  :"
tail -6 "$LOG" 2>/dev/null | sed 's/^/    /' || true
echo "  full output : $LOG"
echo

"$PYTHON" "$SCRIPT_DIR/classify_suite_log.py" "$LOG" "$rc"
exit $?
