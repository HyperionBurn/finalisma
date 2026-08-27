#!/usr/bin/env bash
# Final pre-deploy verification gate. Every line prints EXPECTED vs ACTUAL so
# nothing is assumed. Run from anywhere inside the repo (path is portable —
# see FAILURE-4 note below); push-code-to-vm.sh calls this first and refuses
# to deploy unless it exits 0.
#
# Exit codes (checked by push-code-to-vm.sh, never inferred from output):
#   0 = READY TO DEPLOY (every check passed)
#   1 = DO NOT DEPLOY — a real check failed
#   2 = INCONCLUSIVE — the suite itself could not be judged (harness problem,
#       see FAILURE 3 below). Distinct from 1 on purpose: "the code is
#       broken" and "we couldn't tell if the code is broken" require
#       different next actions, and conflating them once caused a working
#       harness problem to read as a code regression.
set -uo pipefail
# NOTE: deliberately NOT `set -e`. This gate runs dozens of independent
# checks (git grep with legitimately zero matches exits 1, `find` on an
# absent directory exits nonzero, a subprocess probe can legitimately fail);
# under `set -e` any one of those aborts the whole gate before it reaches the
# checks that matter, which is just FAILURE 3 wearing a different hat — a
# harness hiccup silently masquerading as "no verdict was reached" instead of
# being reported as its own row. Every check below handles its own failure
# explicitly instead.

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
PYTHON="${PYTHON:-python}"

pass=0; fail=0
chk() { # chk "label" "expected" "actual"
  if [ "$2" = "$3" ]; then printf "  PASS  %-46s %s\n" "$1" "$3"; pass=$((pass+1))
  else printf "  FAIL  %-46s expected %s, got %s\n" "$1" "$2" "$3"; fail=$((fail+1)); fi
}
chk_ge() { # chk_ge "label" "minimum" "actual" — a floor, not an exact match,
           # so the gate does not cry wolf every time something legitimately
           # grows (a page is added, a tool is added, more tests are written).
  if [ "$3" -ge "$2" ] 2>/dev/null; then printf "  PASS  %-46s %s (>= floor of %s)\n" "$1" "$3" "$2"; pass=$((pass+1))
  else printf "  FAIL  %-46s %s is below the floor of %s\n" "$1" "${3:-<none>}" "$2"; fail=$((fail+1)); fi
}

cd "$REPO_ROOT" || exit 1

echo "=== ARTIFACT SELECTION: verify what SHIPS, not the working tree ==="
# FAILURE 4 (real incident): the gate used to run against the dirty working
# tree (791 tests) while `git archive HEAD` shipped a different commit (788
# tests) — a build was nearly deployed that had never actually been
# verified; when it WAS extracted clean, it failed. push-code-to-vm.sh ships
# `git archive HEAD`, so EVERY check below (not only the test count) runs
# against that same archive, extracted into a fresh temp dir — not whatever
# happens to be sitting in the working tree.
#
# CHOSEN FIX: extract `git archive HEAD` and verify there, rather than
# refusing outright on a dirty tree. This is strictly more accurate: it
# verifies the exact artifact that will ship no matter what the working tree
# looks like, whereas refusing on "dirty" would also refuse for changes that
# have nothing to do with what ships (e.g. an unrelated scratch file sitting
# in the tree). A dirty tree still gets a loud, impossible-to-miss warning
# below, because uncommitted changes silently will not ship either way and
# that has burned this project before.
if command -v git >/dev/null 2>&1 && git rev-parse --git-dir >/dev/null 2>&1; then
  DIRTY="$(git status --porcelain 2>/dev/null || true)"
  DELETIONS="$(printf '%s\n' "$DIRTY" | grep -c '^ D' 2>/dev/null || true)"
  HEAD_SHA="$(git rev-parse --short HEAD 2>/dev/null || echo unknown)"
else
  echo "  FATAL: not a git repository — cannot determine what 'git archive HEAD' would ship"
  exit 1
fi
DELETIONS="${DELETIONS:-0}"

if [ -n "$DIRTY" ]; then
  echo "  WARNING: working tree has uncommitted changes. They will NOT be shipped"
  echo "           by 'git archive HEAD' — only HEAD ships. Dirty paths:"
  printf '%s\n' "$DIRTY" | sed 's/^/             /'
else
  echo "  INFO: working tree is clean — HEAD and the working tree match."
fi

ARCHIVE_DIR="$(mktemp -d 2>/dev/null || mktemp -d -t weft-verify)"
cleanup() { rm -rf "$ARCHIVE_DIR"; }
trap cleanup EXIT

if ! git archive HEAD 2>/dev/null | tar -x -C "$ARCHIVE_DIR" 2>/dev/null; then
  echo "  FATAL: 'git archive HEAD' failed to extract — cannot verify what would ship"
  exit 1
fi
file_count="$(find "$ARCHIVE_DIR" -type f 2>/dev/null | wc -l | tr -d ' ')"
echo "  extracted HEAD ($HEAD_SHA) into a clean temp dir — $file_count files — this IS what ships"
if [ ! -d "$ARCHIVE_DIR/src" ]; then
  echo "  FATAL: staged tree has no src/"
  exit 1
fi

cd "$ARCHIVE_DIR" || exit 1

echo
echo "=== BRAND / PROTOCOL NAMESPACE ==="
# Generalized rather than hardcoded to one brand name: this tree may be
# checked out pre- or post- a product rename, and the gate must be correct
# either way — it must not fail every run just because it was written
# against a different tree's naming. Detect the live protocol namespace from
# pyproject.toml's own declaration instead of guessing a literal string.
proto_value="$(grep -E '^\s*protocol\s*=' pyproject.toml 2>/dev/null | head -1 | sed -E 's/.*=[[:space:]]*"([^"]+)".*/\1/')"
proto_prefix="${proto_value%%.a2a*}"
if [ -z "$proto_prefix" ] || [ "$proto_prefix" = "$proto_value" ]; then
  echo "  FAIL  could not read a '<prefix>.a2a' protocol declaration from pyproject.toml"
  fail=$((fail+1))
else
  proto_count="$(grep -rE "${proto_prefix}\.a2a" --include='*.py' --include='*.md' --include='*.toml' . 2>/dev/null | wc -l | tr -d ' ')"
  echo "  INFO  protocol namespace in use: ${proto_prefix}.a2a  (referenced $proto_count times)"
  chk_ge "protocol constant not lost" 1 "${proto_count:-0}"
fi

mcp_dir="$(find src -maxdepth 1 -type d -name '*_mcp' 2>/dev/null | head -1)"
pkg_prefix="$(basename "${mcp_dir:-_mcp}" 2>/dev/null | sed -E 's/_mcp$//')"
triplet=0
for suffix in cloud mcp sdk; do
  [ -d "src/${pkg_prefix}_${suffix}" ] && triplet=$((triplet+1))
done
chk "packages present (\${prefix}_{cloud,mcp,sdk}, prefix=$pkg_prefix)" "3" "$triplet"

# Old-brand leftovers only mean something mid-migration (an old AND a new
# brand's package directories both present). With exactly one brand present
# there is nothing to have left behind — skip with an INFO line instead of
# forcing a permanent, uninformative failure that would never clear without
# a rename this task is explicitly not authorized to perform.
other_prefixes="$(find src -maxdepth 1 -type d -name '*_mcp' 2>/dev/null -exec basename {} \; | sed -E 's/_mcp$//' | grep -v -x "$pkg_prefix" || true)"
if [ -n "$other_prefixes" ]; then
  alt_pattern="$(printf '%s' "$other_prefixes" | paste -sd'|' -)"
  stale_refs="$(grep -rlE "(${alt_pattern})_(cloud|mcp|sdk)" -- src tests 2>/dev/null | wc -l | tr -d ' ')"
  chk "old package refs in src/tests (mid-migration)" "0" "${stale_refs:-0}"
else
  echo "  INFO  single package brand in this tree ($pkg_prefix) — no migration in progress, skipping stale-ref check"
fi

echo
echo "=== SITE INTEGRITY (this repo has destroyed site/ before) ==="
site_html="$(find site -name '*.html' 2>/dev/null | wc -l | tr -d ' ')"
docs_html="$(find site/docs -name '*.html' 2>/dev/null | wc -l | tr -d ' ')"
# Counts are diagnostics only. The committed manifest below is the gate: a
# deliberate route addition/removal must update one reviewed identity list in
# the same diff, so deleting one page and replacing it with another cannot
# preserve a healthy-looking number.
echo "  INFO  site html files: ${site_html:-0}; site/docs html files: ${docs_html:-0} (identity manifest is authoritative)"
PAGE_MANIFEST="scripts/site-page-manifest.txt"
page_manifest_check="$("$PYTHON" - "$PAGE_MANIFEST" site <<'PYEOF'
from pathlib import Path
import os
import shutil
import tempfile

manifest_path = Path(sys.argv[1])
site_root = Path(sys.argv[2])
if not manifest_path.is_file():
    print(f"manifest missing: {manifest_path}")
    raise SystemExit(1)

expected = []
seen = set()
# inv: seen contains every valid, unique manifest path read so far; term: the
# line iterator advances monotonically to EOF.
for raw_line in manifest_path.read_text(encoding="utf-8").splitlines():
    value = raw_line.strip()
    if not value or value.startswith("#"):
        continue
    relative = Path(value)
    normalized = relative.as_posix()
    if relative.is_absolute() or ".." in relative.parts or relative.suffix.lower() != ".html":
        print(f"invalid manifest path: {value}")
        raise SystemExit(1)
    if normalized in seen:
        print(f"duplicate manifest path: {normalized}")
        raise SystemExit(1)
    seen.add(normalized)
    expected.append(normalized)

if not expected:
    print("manifest contains no HTML paths")
    raise SystemExit(1)

expected_set = set(expected)
actual_set = {
    path.relative_to(site_root).as_posix()
    for path in site_root.rglob("*.html")
    if path.is_file()
}
missing = sorted(expected_set - actual_set)
unexpected = sorted(actual_set - expected_set)
if missing or unexpected:
    if missing:
        print("missing manifest paths: " + ", ".join(missing))
    if unexpected:
        print("unlisted HTML paths: " + ", ".join(unexpected))
    raise SystemExit(1)
print(f"exact HTML route set: {len(expected_set)} paths")
PYEOF
)"
page_manifest_rc=$?
if [ "$page_manifest_rc" -eq 0 ]; then
  echo "  PASS  exact HTML route manifest"
  pass=$((pass+1))
else
  echo "  FAIL  exact HTML route manifest"
  fail=$((fail+1))
fi
chk "uncommitted deletions (from the ORIGINAL working tree)" "0" "$DELETIONS"
if [ -f "web/scripts/verify-preservation.cjs" ]; then
  if command -v node >/dev/null 2>&1 && node "web/scripts/verify-preservation.cjs" >/dev/null 2>&1; then
    echo "  PASS  preservation verifier"; pass=$((pass+1))
  else
    echo "  FAIL  preservation verifier"; fail=$((fail+1))
  fi
else
  echo "  INFO  web/scripts/verify-preservation.cjs not present — skipping"
fi

echo
echo "=== TESTS (run against the extracted HEAD archive, not the working tree) ==="
# FAILURE 3 (real incident): this used to pipe the suite through `| tail -4`
# and treat an EMPTY capture as failure. A harness problem (python off PATH,
# an early kill, a lost pipe) was then indistinguishable from broken tests,
# and it discarded the evidence needed to tell them apart. It blocked a
# legitimate deploy. Silence must never be a verdict.
#
# Fix: write the FULL output to a file, then classify it with
# scripts/classify_suite_log.py, which checks the process exit code (never
# parses "OK"/"FAILED" text once an exit code exists) and reports one of
# three distinct outcomes — PASS / REAL FAILURE / INCONCLUSIVE-HARNESS — so
# a harness problem can never be silently folded into "the code is broken".
SUITE_LOG="${TMPDIR:-/tmp}/weft-final-verify-suite.log"
echo "  running (this can take a few minutes)... full output -> $SUITE_LOG"
PYTHONPATH=src timeout 1200 "$PYTHON" -B -m unittest discover -s tests -q >"$SUITE_LOG" 2>&1
suite_rc=$?

verdict_line="$("$PYTHON" "$SCRIPT_DIR/classify_suite_log.py" "$SUITE_LOG" "$suite_rc")"
verdict_rc=$?
echo "$verdict_line" | sed 's/^/  /'
n="$(grep -aoE 'Ran [0-9]+ test' "$SUITE_LOG" 2>/dev/null | tail -1 | grep -oE '[0-9]+' || true)"
# This file is the one deliberate ratchet value. Raising it requires a
# committed diff, while the actual count remains derived from this run's
# unittest discovery output; there is no second hardcoded floor.
TEST_COUNT_BASELINE_FILE="scripts/test-count-baseline.txt"
test_count_baseline="$(tr -d '[:space:]' < "$TEST_COUNT_BASELINE_FILE" 2>/dev/null || true)"
if [[ "$test_count_baseline" =~ ^[1-9][0-9]*$ ]]; then
  echo "  last-known-good test count: $test_count_baseline (from $TEST_COUNT_BASELINE_FILE)"
  test_count_baseline_valid=1
else
  echo "  FAIL  test-count baseline missing or invalid: $TEST_COUNT_BASELINE_FILE"
  fail=$((fail+1))
  test_count_baseline_valid=0
fi
echo "  test count  : ${n:-<none captured>}  (must not drop below the committed ratchet)"

case "$verdict_rc" in
  0)
    echo "  PASS  suite result"; pass=$((pass+1))
    if [ "$test_count_baseline_valid" -eq 1 ]; then
      chk_ge "test count (committed ratchet)" "$test_count_baseline" "${n:-0}"
    fi
    ;;
  2)
    echo "  INCONCLUSIVE  suite result — harness problem, not a code verdict. See $SUITE_LOG"
    tail -8 "$SUITE_LOG" 2>/dev/null | sed 's/^/           /'
    ;;
  *)
    echo "  FAIL  suite result — real test failures"; fail=$((fail+1))
    grep -aE '^(FAIL|ERROR):' "$SUITE_LOG" 2>/dev/null | head -8 | sed 's/^/           /'
    ;;
esac

echo
echo "=== TOOL SURFACE ==="
tool_probe="$(PYTHONPATH=src timeout 180 "$PYTHON" - "$pkg_prefix" "$other_prefixes" 2>/dev/null <<'PYEOF'
import sys

try:
    from weft_cloud.mcp import HostedMCPDispatcher
    from weft_cloud.service import create_service
    from scripts.probe_hosted_mcp_surface import EXPECTED_TOOL_NAMES

    # Exercise the hosted dispatcher that owns the public /mcp catalog. A
    # self-hosted ``weft_mcp`` process intentionally exposes a different,
    # larger tool family and is not the production contract being checked.
    temp_dir = tempfile.mkdtemp(prefix="weft-final-tool-")
    service = None
    try:
        service = create_service(os.path.join(temp_dir, "state.db"))
        dispatcher = HostedMCPDispatcher(service)
        response = dispatcher.handle_json_rpc(
            {"jsonrpc": "2.0", "id": 1, "method": "tools/list"},
            ctx=object(),
        )
        tools = response["result"]["tools"]
    finally:
        if service is not None:
            service.backend.close()
        shutil.rmtree(temp_dir, ignore_errors=True)
    actual_names = [
        tool.get("name") for tool in tools
        if isinstance(tool, dict) and isinstance(tool.get("name"), str)
    ]
    expected_names = set(EXPECTED_TOOL_NAMES)
    actual_set = set(actual_names)
    missing = expected_names - actual_set
    unexpected = actual_set - expected_names
    duplicate = len(actual_names) != len(actual_set)
    bad = len(missing) + len(unexpected) + (1 if duplicate else 0)
    print(len(actual_names), len(expected_names), bad)
except Exception:
    print(0, 0, 1)
PYEOF
)"
tool_probe="${tool_probe:-0 0 1}"
read -r tool_count expected_tool_count bad_count <<< "$tool_probe"
echo "  INFO  tool surface: ${tool_count:-0} actual, ${expected_tool_count:-0} expected names"
# The exact expected set is imported from probe_hosted_mcp_surface.py so this
# and the post-deploy probe cannot drift into two tool lists.
chk "tool surface matches EXPECTED_TOOL_NAMES" "0" "${bad_count:-1}"

echo
echo "  ---- $pass passed, $fail failed ----"
if [ "$fail" -eq 0 ] && [ "$verdict_rc" -eq 0 ]; then
  echo "  READY TO DEPLOY"
  exit 0
elif [ "$verdict_rc" -eq 2 ] && [ "$fail" -eq 0 ]; then
  echo "  INCONCLUSIVE — the harness did not produce a judgeable suite run. Fix the"
  echo "  harness (see $SUITE_LOG) and re-run; this is not evidence the code is broken."
  exit 2
else
  echo "  DO NOT DEPLOY"
  exit 1
fi
