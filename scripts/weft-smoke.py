"""Run the complete local Weft handoff without installing dependencies."""

from __future__ import annotations

import json
import sys
import tempfile
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from weft_mcp.core import WeftError, WeftStore  # noqa: E402


def main() -> int:
    with tempfile.TemporaryDirectory(prefix="weft-smoke-") as temporary:
        root = Path(temporary)
        with WeftStore(root / "state.db", root) as store:
            store.register_agent("demo", "agent-a", "Planner", "architect", "gpt-5.6-luna", ["planning"])
            store.register_agent("demo", "agent-b", "Reviewer", "security", "opencode-go/mimo-v2.5", ["security", "testing"])

            pairing = store.create_pairing("agent-a", "demo", capabilities_offered=["read", "comment"])
            preview = store.pairing_preview(pairing["join_token"])
            joined = store.join_pairing(pairing["join_token"], "agent-b", model="opencode-go/mimo-v2.5", capabilities=["security"], consent=True)
            task_result = store.create_task(
                "demo",
                "agent-a",
                "Review the retry boundary",
                "Inspect the handoff artifact and return a security finding.",
                scope=["handoff.txt"],
                preferred_agent="agent-b",
                idempotency_key="smoke-task-v1",
            )
            task = task_result["task"]
            claimed = store.claim_task("demo", "agent-b", task["task_id"])
            (root / "handoff.txt").write_text("synthetic handoff evidence\n", encoding="utf-8")
            verified = store.verify_task(
                "demo",
                "agent-b",
                task["task_id"],
                claimed["fencing_token"],
                ["handoff.txt"],
                [{"name": "smoke-check", "status": "passed", "evidence": "synthetic artifact present"}],
            )
            completed = store.complete_task("demo", "agent-b", task["task_id"], claimed["fencing_token"], "Smoke handoff verified")
            result = {
                "status": "ok",
                "pairing_preview": preview["status"],
                "session_state": joined["state"],
                "task_status": completed["status"],
                "evidence_passed": verified["passed"],
                "raw_credentials_printed": False,
            }
        print(json.dumps(result, indent=2))
        return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except WeftError as exc:
        print(json.dumps({"status": "failed", "error": exc.as_dict()}), file=sys.stderr)
        raise SystemExit(1) from exc
