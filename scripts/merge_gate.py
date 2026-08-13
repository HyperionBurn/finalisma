#!/usr/bin/env python3
"""Pre-merge gate — mechanical checks, because discipline failed under pressure.

Process postmortem 2026-08-13, rules encoded here:
  R12 — the orchestrator owns integration; no dirty worktree may be merged.
  R11 — never infer success from output: assert STATE (exit codes, HEAD, files).
  R9  — the 0.5s test-count guard runs BEFORE the 7.5-minute full suite.

Usage:
  python -B scripts/merge_gate.py [--worktree DIR] [--expect-head SHA]

Checks, in order — each failure exits 1 with a named reason and a fix hint:
  1. worktree exists and is clean (``git status --porcelain`` empty).
  2. no merge/rebase/cherry-pick is in progress (no conflict-state files).
  3. the test-count sync guard passes (run the 0.5s test, not the suite).
  4. if ``--expect-head`` is given: HEAD must EQUAL it — i.e. a merge that was
     supposed to move the branch actually moved it, instead of aborting while
     the wrapper printed something that looked happy.

Exit 0 only when every check passes. Stdlib only, like the rest of the repo.
"""

from __future__ import annotations

import argparse
import subprocess
import sys
from pathlib import Path

_CONFLICT_STATE_FILES = ("MERGE_HEAD", "CHERRY_PICK_HEAD", "REVERT_HEAD", "rebase-merge", "rebase-apply")


def _run(cmd: list[str], cwd: Path) -> subprocess.CompletedProcess:
    return subprocess.run(cmd, cwd=str(cwd), capture_output=True, text=True)


def fail(reason: str, fix: str) -> None:
    print(f"MERGE GATE: REFUSED — {reason}")
    print(f"  fix: {fix}")
    sys.exit(1)


def check_clean_tree(git: str, cwd: Path) -> None:
    proc = _run([git, "status", "--porcelain"], cwd)
    if proc.returncode != 0:
        fail("git status failed (state unreadable)",
             f"run `git -C {cwd} status` by hand and read the error")
    if proc.stdout.strip():
        fail("worktree is dirty (R12: no dirty tree may be merged)",
             f"stage or discard changes in {cwd}, then rerun; `git status` shows the files")


def check_no_conflict_state(git: str, cwd: Path) -> None:
    proc = _run([git, "rev-parse", "--git-dir"], cwd)
    if proc.returncode != 0:
        fail("not a git repository", f"{cwd} is not inside a git worktree")
    git_dir = Path(proc.stdout.strip())
    if not git_dir.is_absolute():
        git_dir = cwd / git_dir
    for name in _CONFLICT_STATE_FILES:
        if (git_dir / name).exists():
            fail(f"a {name} state file exists — a merge/rebase is in progress",
                 "finish or abort it (`git merge --abort` / `git rebase --abort`) before merging")


def check_count_sync(cwd: Path) -> None:
    proc = _run([sys.executable, "-B", "-m", "unittest",
                 "tests.test_site.TestCountSyncTests"], cwd)
    if proc.returncode != 0:
        fail("the test-count sync guard is red (R9: fix the count before the full suite)",
             "run the guard directly for the exact mismatch; update the published numbers")


def check_head_moved(git: str, cwd: Path, expect: str) -> None:
    proc = _run([git, "rev-parse", "HEAD"], cwd)
    if proc.returncode != 0:
        fail("cannot resolve HEAD", f"run `git -C {cwd} rev-parse HEAD` by hand")
    head = proc.stdout.strip()
    if head != expect:
        fail(f"HEAD is {head}, expected {expect} (R11: the merge did not move the branch "
             "even though a wrapper may have printed success)",
             "re-run the merge and trust the EXIT CODE, not the output; if it aborted, resolve and retry")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--worktree", type=Path, default=Path("."),
                        help="worktree to check (default: current directory)")
    parser.add_argument("--expect-head", default=None,
                        help="expected HEAD sha after a merge — asserts the branch actually moved")
    args = parser.parse_args()

    cwd = args.worktree.resolve()
    if not cwd.is_dir():
        fail("worktree does not exist", f"check the path: {cwd}")

    git = "git"
    check_clean_tree(git, cwd)
    check_no_conflict_state(git, cwd)
    check_count_sync(cwd)
    if args.expect_head:
        check_head_moved(git, cwd, args.expect_head)

    print(f"MERGE GATE: PASS — {cwd} is clean, no conflict state, count-sync green"
          + (f", HEAD == {args.expect_head}" if args.expect_head else ""))


if __name__ == "__main__":
    main()
