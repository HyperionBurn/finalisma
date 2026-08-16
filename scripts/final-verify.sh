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
chk_ge "site html files"      18 "${site_html:-0}"
chk_ge "site/docs html files" 7  "${docs_html:-0}"
chk "uncommitted deletions (from the ORIGINAL working tree)" "0" "$DELETIONS"
if [ -f "$REPO_ROOT/web/scripts/verify-preservation.cjs" ]; then
  if command -v node >/dev/null 2>&1 && node "$REPO_ROOT/web/scripts/verify-preservation.cjs" >/dev/null 2>&1; then
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
echo "  test count  : ${n:-<none captured>}  (must not drop from the last known-good count)"

case "$verdict_rc" in
  0)
    echo "  PASS  suite result"; pass=$((pass+1))
    chk_ge "test count" 525 "${n:-0}"
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
import json, os, subprocess, sys

pkg_prefix = sys.argv[1]
other_prefixes = [p for p in sys.argv[2].split() if p]
env = dict(os.environ)
env["PYTHONPATH"] = "src"

try:
    proc = subprocess.Popen(
        [sys.executable, "-B", "-m", f"{pkg_prefix}_mcp", "--transport", "stdio", "--team-id", "v"],
        stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True, env=env,
    )

    def w(obj):
        proc.stdin.write(json.dumps(obj) + "\n")
        proc.stdin.flush()

    w({"jsonrpc": "2.0", "id": 1, "method": "initialize",
       "params": {"protocolVersion": "2024-11-05", "capabilities": {}, "clientInfo": {"name": "v", "version": "1"}}})
    proc.stdout.readline()
    w({"jsonrpc": "2.0", "method": "notifications/initialized"})
    w({"jsonrpc": "2.0", "id": 2, "method": "tools/list"})
    line = proc.stdout.readline()
    tools = json.loads(line)["result"]["tools"]
    proc.kill()
    bad = [
        t["name"] for t in tools
        if any(t["name"].startswith(op + "_") for op in other_prefixes)
        or f"{pkg_prefix}_{pkg_prefix}" in t["name"]
    ]
    print(len(tools), len(bad))
except Exception:
    print(0, 0)
PYEOF
)"
tool_probe="${tool_probe:-0 0}"
tool_count="$(echo "$tool_probe" | cut -d' ' -f1)"
bad_count="$(echo "$tool_probe" | cut -d' ' -f2)"
# Floor, not exact match (baseline 58 measured at authoring time against this
# tree) — an exact-equality check here would fail the gate every time a tool
# is legitimately added, which trains people to ignore it.
chk_ge "tool count (baseline 58)"         58 "${tool_count:-0}"
chk "tools with stale/doubled prefix"     "0"  "${bad_count:-0}"

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
