"""Weft MCP Streamable HTTP protocol integration test.

Spawns the coordinator over HTTP, drives a genuine remote JSON-RPC client
against POST /mcp (no weft_mcp internals imported), asserts the full
pairing + verified-handoff lifecycle completes, and asserts the one-use
pairing link reuse is refused. Reuses the driver module's spawn/flow helpers
so the test exercises the exact same wire path as the committed transcript.

Run:  python -B -m unittest tests.test_interop_http -v
"""

from __future__ import annotations

import json
import socket
import subprocess
import sys
import tempfile
import time
import http.client
import unittest
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))
sys.path.insert(0, str(PROJECT_ROOT / "scripts"))

import importlib.util

_SPEC = importlib.util.spec_from_file_location(
    "_interop_validate_http",
    str(PROJECT_ROOT / "scripts" / "interop-validate-http.py"),
)
assert _SPEC is not None and _SPEC.loader is not None
_MOD = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(_MOD)

HTTPClient = _MOD.HTTPClient
InteropError = _MOD.InteropError
_find_free_port = _MOD._find_free_port
_wait_for_port = _MOD._wait_for_port
call_tool = _MOD.call_tool

TEAM_ID = "demo"


def _run_http_handoff(port: int, workspace: Path) -> dict:
    """Drive the full lifecycle against an already-spawned HTTP coordinator."""
    transcript: list[str] = []
    _wait_for_port("127.0.0.1", port, timeout_s=10.0)
    client = HTTPClient("127.0.0.1", port)
    try:
        init = client.rpc(transcript, 1, "initialize", {"protocolVersion": "2025-03-26", "capabilities": {}})
        protocol_version = init.get("protocolVersion")
        server_info = init.get("serverInfo")
        if not protocol_version or not server_info:
            raise InteropError("initialize returned no protocolVersion/serverInfo")

        tools = client.rpc(transcript, 2, "tools/list").get("tools", [])
        tool_names = {t["name"] for t in tools}
        required = {
            "register_agent", "create_pairing", "pairing_preview",
            "join_pairing", "create_task", "claim_task",
            "verify_task", "complete_task",
        }
        missing = required - tool_names
        if missing:
            raise InteropError(f"tools/list missing required tools: {missing}")

        reg_a = call_tool(client, transcript, 3, "register_agent", {
            "team_id": TEAM_ID, "agent_id": "agent-a", "role": "architect", "name": "Planner",
        })
        token_a = reg_a["actor_token"]
        reg_b = call_tool(client, transcript, 4, "register_agent", {
            "team_id": TEAM_ID, "agent_id": "agent-b", "role": "builder", "name": "Builder",
        })
        token_b = reg_b["actor_token"]

        pairing = call_tool(client, transcript, 5, "create_pairing", {
            "initiator_id": "agent-a", "team_id": TEAM_ID,
            "capabilities_offered": ["read", "comment"], "actor_token": token_a,
        })
        join_token = pairing["join_token"]
        preview = call_tool(client, transcript, 6, "pairing_preview", {"token": join_token})
        if preview.get("status") != "issued":
            raise InteropError(f"pairing preview status != issued: {preview}")
        joined = call_tool(client, transcript, 7, "join_pairing", {
            "token": join_token, "agent_id": "agent-b", "consent": True, "actor_token": token_b,
        })
        if joined.get("state") not in ("active", "open"):
            raise InteropError(f"join state not active/open: {joined}")

        task_result = call_tool(client, transcript, 8, "create_task", {
            "team_id": TEAM_ID, "created_by": "agent-a",
            "title": "Interop review", "description": "Verify the retry boundary in handoff.txt",
            "scope": ["handoff.txt"], "preferred_agent": "agent-b",
            "idempotency_key": "unittest-http-task-v1", "actor_token": token_a,
        })
        task_id = task_result["task"]["task_id"]
        claimed = call_tool(client, transcript, 9, "claim_task", {
            "team_id": TEAM_ID, "agent_id": "agent-b", "task_id": task_id, "actor_token": token_b,
        })
        fencing = claimed["fencing_token"]
        (workspace / "handoff.txt").write_text("unittest interop http evidence\n", encoding="utf-8")
        verified = call_tool(client, transcript, 10, "verify_task", {
            "team_id": TEAM_ID, "agent_id": "agent-b", "task_id": task_id,
            "fencing_token": fencing, "files": ["handoff.txt"],
            "checks": [{"name": "unittest-check", "status": "passed", "evidence": "artifact present"}],
            "actor_token": token_b,
        })
        if not verified.get("passed"):
            raise InteropError(f"evidence gate did not pass: {verified}")
        completed = call_tool(client, transcript, 11, "complete_task", {
            "team_id": TEAM_ID, "agent_id": "agent-b", "task_id": task_id,
            "fencing_token": fencing, "summary": "Unittest HTTP verified", "actor_token": token_b,
        })
        if completed.get("status") != "done":
            raise InteropError(f"complete status != done: {completed}")

        # NEGATIVE: reuse the one-use pairing link
        refused = False
        try:
            call_tool(client, transcript, 12, "join_pairing", {
                "token": join_token, "agent_id": "agent-c", "consent": True,
            })
            refused = False
        except InteropError as exc:
            if "pairing_unavailable" in str(exc) or "Pairing is consumed" in str(exc):
                refused = True
            else:
                raise

        return {
            "protocol_version": protocol_version,
            "server_info": server_info,
            "tool_count": len(tool_names),
            "join_state": joined.get("state"),
            "task_status": completed.get("status"),
            "evidence_passed": bool(verified.get("passed")),
            "negative_refused": refused,
        }
    finally:
        client.close()


class TestInteropHTTP(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.scratch = tempfile.TemporaryDirectory(prefix="weft-unittest-http-")
        cls.workspace = Path(cls.scratch.name)
        cls.port = _find_free_port()
        cls.proc = subprocess.Popen(
            [
                sys.executable,
                "-B",
                "scripts/weft-mcp.py",
                "--transport",
                "http",
                "--host",
                "127.0.0.1",
                "--port",
                str(cls.port),
                "--team-id",
                TEAM_ID,
                "--workspace",
                str(cls.workspace),
                "--state",
                str(cls.workspace / ".weft" / "state.db"),
                "--actor-auth",
                "trust",
            ],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            cwd=str(PROJECT_ROOT),
            encoding="utf-8",
            errors="replace",
        )

    @classmethod
    def tearDownClass(cls) -> None:
        cls.proc.terminate()
        try:
            cls.proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            cls.proc.kill()
            cls.proc.wait(timeout=10)
        cls.scratch.cleanup()

    def test_http_handoff_lifecycle(self) -> None:
        result = _run_http_handoff(self.port, self.workspace)
        self.assertEqual(result["protocol_version"], "2025-11-25")
        self.assertEqual(result["server_info"], {"name": "weft-mcp", "version": "0.1.0"})
        self.assertGreaterEqual(result["tool_count"], 58)
        self.assertEqual(result["join_state"], "active")
        self.assertEqual(result["task_status"], "done")
        self.assertTrue(result["evidence_passed"])
        self.assertTrue(result["negative_refused"], "one-use pairing link reuse must be refused")


if __name__ == "__main__":
    unittest.main()
