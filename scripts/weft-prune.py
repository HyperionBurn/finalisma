"""Preview or apply operator-invoked Weft retention cleanup."""

from __future__ import annotations

import argparse
import json
import os

from pathlib import Path
import sys

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from weft_mcp.core import WeftError, WeftStore  # noqa: E402


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Preview or apply Weft terminal-history retention cleanup")
    parser.add_argument("--state", default=os.environ.get("WEFT_STATE", ".weft/state.db"))
    parser.add_argument("--workspace", default=os.environ.get("WEFT_WORKSPACE", "."))
    parser.add_argument("--retention-days", type=int, default=30)
    parser.add_argument("--apply", action="store_true", help="Actually delete rows; omit for a dry run")
    args = parser.parse_args(argv)
    try:
        with WeftStore(args.state, args.workspace) as store:
            result = store.prune_expired(args.retention_days * 86_400, apply=args.apply)
        print(json.dumps(result, indent=2))
        return 0
    except WeftError as exc:
        print(json.dumps({"status": "failed", "error": exc.as_dict()}))
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
