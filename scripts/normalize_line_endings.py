#!/usr/bin/env python3
"""Strip CR so a script that just crossed Windows -> Linux is safe to run.

Why this exists (FAILURE 2, real incident): redeploy-weft.sh reached the VM
with CRLF line endings (committed/edited on a Windows box with
core.autocrlf=true). bash read the first real line as `set -euo pipefail\r`,
treated `pipefail\r` as an unknown option, and the cutover aborted mid-deploy.

`.gitattributes` (`*.sh text eol=lf`) is the prevention layer: it makes git
check these files out with LF even on Windows, so this should never trigger
in the common case. This script is the belt-and-suspenders layer for
everything else — a file edited with attributes disabled, copied by a tool
that ignores git entirely, or hand-patched on the VM. push-code-to-vm.sh runs
it on every script immediately after scp, before anything is executed.

Usage as a library::

    from normalize_line_endings import normalize
    text, changed = normalize(raw_bytes)

Usage as a CLI::

    python3 normalize_line_endings.py <file> [<file> ...]
        Rewrites each file in place (only touches disk if something changed)
        and prints "normalized: <path>" or "already-lf: <path>" per file.

    python3 normalize_line_endings.py --check <file> [<file> ...]
        Does not write. Exits 1 and lists offending files if any file would
        change; exits 0 if every file is already LF-only. Suitable for a
        pre-flight gate.
"""

from __future__ import annotations

import sys
from pathlib import Path


def normalize(data: bytes) -> "tuple[bytes, bool]":
    """Convert CRLF and lone CR to LF. Returns (normalized_bytes, changed)."""
    normalized = data.replace(b"\r\n", b"\n").replace(b"\r", b"\n")
    return normalized, normalized != data


def normalize_file(path: Path) -> bool:
    """Normalize `path` in place. Returns True if the file was rewritten."""
    original = path.read_bytes()
    normalized, changed = normalize(original)
    if changed:
        path.write_bytes(normalized)
    return changed


def _main(argv: "list[str]") -> int:
    check_only = False
    args = list(argv)
    if args and args[0] == "--check":
        check_only = True
        args = args[1:]

    if not args:
        print("usage: normalize_line_endings.py [--check] <file> [<file> ...]", file=sys.stderr)
        return 2

    offenders: list[str] = []
    missing: list[str] = []
    for raw in args:
        path = Path(raw)
        if not path.is_file():
            missing.append(raw)
            continue
        original = path.read_bytes()
        _normalized, changed = normalize(original)
        if not changed:
            print(f"already-lf: {path}")
            continue
        if check_only:
            offenders.append(raw)
            print(f"would-normalize: {path}")
        else:
            normalize_file(path)
            print(f"normalized: {path}")

    if missing:
        for raw in missing:
            print(f"missing: {raw}", file=sys.stderr)
        return 2
    if check_only and offenders:
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(_main(sys.argv[1:]))
