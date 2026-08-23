"""Generate the standalone, downloadable ``weft-mcp-bridge.py``.

**The problem this solves.** The hosted connector-config generator
(``src/weft_cloud/web/config_gen.py``) emits a ``command`` + ``args`` stdio
launch for MCP hosts. That launch needs an actual file on the CUSTOMER's
machine. Two things that look like fixes are not:

* ``scripts/weft-mcp.py`` only works if ``src/weft_mcp`` (the whole package)
  sits next to it — true on our server, never true on a customer's laptop.
* ``python -m weft_mcp`` only works if the ``weft_mcp`` package is installed
  — it is not published to PyPI and the source repository is private, so
  ``pip install`` has nothing to reach.

**The fix.** ``src/weft_mcp/stdio_bridge.py`` already imports nothing but the
standard library and has no relative imports — it is already a self-contained
program, just missing a CLI entry point. This script wraps that module's
source verbatim with a small ``argparse`` front door and writes the result to
``site/downloads/weft-mcp-bridge.py``, which ``WeftWebApp`` serves at a
stable, unauthenticated URL (see ``_PUBLIC_GET_PATHS`` in
``src/weft_cloud/web/app.py``). A customer downloads that ONE file, points
their MCP host's config at it, and never needs the private repository or a
PyPI package.

``src/weft_mcp/stdio_bridge.py`` remains the single source of truth for the
bridge logic — this script never hand-duplicates it, only re-wraps it, so the
two can never drift apart silently. ``--check`` mode enforces that: it
regenerates the output in memory and fails loudly if the committed file does
not match, exactly the drift a future edit to ``stdio_bridge.py`` could
otherwise introduce unnoticed.

Usage::

    python scripts/build_weft_mcp_bridge.py          # (re)write the output file
    python scripts/build_weft_mcp_bridge.py --check  # verify it is up to date; exits 1 if not
"""

from __future__ import annotations

import argparse
import ast
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SOURCE = ROOT / "src" / "weft_mcp" / "stdio_bridge.py"
OUTPUT = ROOT / "site" / "downloads" / "weft-mcp-bridge.py"

# The exact import line stdio_bridge.py is known to start its import block
# with. The CLI section needs ``argparse``, which stdio_bridge.py itself has
# no reason to import, so it is inserted here — right beside the rest of the
# top-of-file imports, not bolted on at the end. If a future edit to
# stdio_bridge.py changes this import block's shape, this marker will no
# longer match and generation fails loudly (see below) instead of silently
# emitting a file with a misplaced import.
_IMPORT_MARKER = "import http.client\n"

_HEADER = '''#!/usr/bin/env python3
# GENERATED FILE — do not hand-edit.
#
# Source of truth: src/weft_mcp/stdio_bridge.py (the bridge logic, unchanged
# below) plus the CLI front door appended at the end of this file. Regenerate
# with `python scripts/build_weft_mcp_bridge.py` from a checkout of
# https://github.com/HyperionBurn/finalisma — running THIS file does not
# require that checkout; it requires only a Python 3.11+ standard library.
'''

_NEW_DOCSTRING = '''"""Weft MCP bridge — standalone, no-install download.

This ONE file is the entire program. It has no dependencies beyond the
Python standard library and no sibling files to go with it — download it,
point your MCP host at it, and it runs. You do not need the weft-mcp
package (not published to PyPI), and you do not need the source
repository (private).

What it does: bridges a stdio MCP client (Claude Desktop, Cursor, Codex,
Claude Code — anything that launches an MCP server as a `command` + `args`
subprocess) to the HOSTED Weft service's Streamable-HTTP `POST /mcp`
endpoint. Every JSON-RPC message your MCP host writes to this process's
stdin is forwarded to `<remote>/mcp` with `Authorization: Bearer <token>`;
the reply comes back on stdout. Your host sees the hosted room tools as if
they were a local process.

Usage::

    python weft-mcp-bridge.py --remote https://<your-weft-origin> --token-env WEFT_TOKEN

- `--remote <origin>` — the hosted service's base URL. Required: this file
  only speaks to a hosted endpoint: it has no local coordinator mode.
- `--token-env <NAME>` — the name of an environment variable holding your
  bearer token (an `agk_` agent key, minted from the dashboard's Connector
  config page, or an `fss_` session token). Defaults to `WEFT_TOKEN`. The
  token is read from that environment variable ONLY, never from a
  command-line argument — argv is visible to every process on the machine.

Get an updated copy any time by re-downloading from the same URL you got
this one from; the file is small and safe to replace outright.

Full behavior, failure-mode discipline, and the wire contract are documented
below, inline, in the forwarding code this file wraps.
"""'''

_CLI_SECTION = '''

# ---------------------------------------------------------------------------
# CLI entry point. Nothing above this line imports anything outside the
# standard library, and nothing above it needs any other file — that is the
# whole point of shipping this as one download.
# ---------------------------------------------------------------------------


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="weft-mcp-bridge.py",
        description=(
            "Bridge a stdio MCP client to the hosted Weft service's "
            "POST /mcp endpoint."
        ),
    )
    parser.add_argument(
        "--remote",
        required=True,
        help=(
            "Hosted Weft origin, e.g. https://weft.example.com "
            "(the bridge POSTs to <remote>/mcp)"
        ),
    )
    parser.add_argument(
        "--token-env",
        default="WEFT_TOKEN",
        help=(
            "Environment variable holding the bearer token (default: "
            "WEFT_TOKEN). Never pass the token itself on the command line."
        ),
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    try:
        bridge = StdioHttpBridge(args.remote, args.token_env)
    except ValueError as exc:
        raise SystemExit(f"weft-mcp-bridge: {exc}") from exc
    return run_stdio_bridge(bridge)


if __name__ == "__main__":
    raise SystemExit(main())
'''


def _strip_module_docstring(source: str) -> str:
    """Return ``source`` with its leading module docstring removed.

    Uses ``ast`` (not a quote-matching regex) so this is correct regardless
    of what punctuation the docstring itself contains. Raises ``ValueError``
    if the source has no leading docstring — a structural assumption this
    generator depends on.
    """
    tree = ast.parse(source)
    if not tree.body or not (
        isinstance(tree.body[0], ast.Expr)
        and isinstance(tree.body[0].value, ast.Constant)
        and isinstance(tree.body[0].value.value, str)
    ):
        raise ValueError(f"{SOURCE} has no leading module docstring")
    end_line = tree.body[0].end_lineno
    lines = source.splitlines(keepends=True)
    remainder = "".join(lines[end_line:])
    return remainder.lstrip("\n")


def generate_source() -> str:
    """Build the full standalone bridge script as a single string."""
    original = SOURCE.read_text(encoding="utf-8")
    body = _strip_module_docstring(original)
    if _IMPORT_MARKER not in body:
        raise ValueError(
            f"{SOURCE} no longer starts its import block with "
            f"{_IMPORT_MARKER!r} — update _IMPORT_MARKER in this generator "
            "to match, then re-run"
        )
    body = body.replace(_IMPORT_MARKER, f"import argparse\n{_IMPORT_MARKER}", 1)
    parts = [_HEADER, _NEW_DOCSTRING, "\n\n", body.rstrip("\n"), "\n", _CLI_SECTION]
    result = "".join(parts)
    # Fails loudly here rather than shipping a syntax error to a customer.
    compile(result, str(OUTPUT), "exec")
    return result


def _main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--check",
        action="store_true",
        help="verify the committed output is up to date; write nothing, exit 1 if stale",
    )
    args = parser.parse_args(argv)
    generated = generate_source()

    if args.check:
        if not OUTPUT.is_file():
            print(f"MISSING: {OUTPUT} has not been generated yet", file=sys.stderr)
            return 1
        current = OUTPUT.read_text(encoding="utf-8")
        if current != generated:
            print(
                f"STALE: {OUTPUT} does not match src/weft_mcp/stdio_bridge.py.\n"
                "Run `python scripts/build_weft_mcp_bridge.py` (no --check) to regenerate.",
                file=sys.stderr,
            )
            return 1
        print(f"up to date: {OUTPUT}")
        return 0

    OUTPUT.parent.mkdir(parents=True, exist_ok=True)
    with OUTPUT.open("w", encoding="utf-8", newline="\n") as handle:
        handle.write(generated)
    print(f"wrote {OUTPUT} ({len(generated.encode('utf-8'))} bytes)")
    return 0


if __name__ == "__main__":
    raise SystemExit(_main(sys.argv[1:]))
