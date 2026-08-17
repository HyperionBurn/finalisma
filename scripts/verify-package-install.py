#!/usr/bin/env python3
"""Verify that an isolated Python interpreter can install and launch Weft.

This is an explicit release gate, not part of standard unit-test discovery. It
installs the current source tree into the interpreter supplied by the caller
and runs the installed module without ``PYTHONPATH`` or a checkout launcher.
"""

from __future__ import annotations

import argparse
import subprocess
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def _run(python: Path, *args: str) -> None:
    command = [str(python), *args]
    print("+", " ".join(command), flush=True)
    subprocess.run(command, cwd=ROOT, check=True)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Install the current tree into a venv and verify python -m weft_mcp."
    )
    parser.add_argument(
        "--python",
        required=True,
        type=Path,
        help="Python executable belonging to the target venv",
    )
    args = parser.parse_args(argv)
    python = args.python.resolve()
    if not python.is_file():
        parser.error(f"target Python executable does not exist: {python}")
    _run(python, "-m", "pip", "install", "--no-deps", str(ROOT))
    _run(python, "-B", "-m", "weft_mcp", "--help")
    print("Package install and module entry point verified.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
