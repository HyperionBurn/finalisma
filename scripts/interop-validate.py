"""Weft real-MCP stdio protocol validation driver.

Spawns the coordinator itself over STDIO (MCP's primary transport), speaks
real MCP JSON-RPC (initialize / tools/list / tools/call), exercises the full
pairing + verified-handoff lifecycle, includes a deliberate negative case,
and tears the child down in a finally block.

Run:  timeout 180 python -B scripts/interop-validate.py
"""

from __future__ import annotations

import json
import subprocess
import sys
import tempfile
import time
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "src"))

TEAM_ID = "demo"
REQUEST_TIMEOUT_S = 60


class InteropError(RuntimeError):
    pass


def main() -> int:
    scratch = tempfile.TemporaryDirectory(prefix="weft-interop-")
    workspace = Path(scratch.name)
    transcript: list[str] = []
    started = time.monotonic()

    proc = subprocess.Popen(
        [
            sys.executable,
            "-B",
            "scripts/weft-mcp.py",
            "--transport",
            "stdio",
            "--team-id",
            TEAM_ID,
            "--workspace",
            str(workspace),
            "--state",
            str(workspace / ".weft" / "state.db"),
        ],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        cwd=str(PROJECT_ROOT),
        encoding="utf-8",
        errors="replace",
    )

    def rpc(request_id: int, method: str, params: dict | None = None) -> dict:
        payload = {"jsonrpc": "2.0", "id": request_id, "method": method}
        if params is not None:
            payload["params"] = params
        transcript.append(f"> {json.dumps(payload, separators=(",", ":"))}")
        assert proc.stdin is not None and proc.stdout is not None
        proc.stdin.write(json.dumps(payload, separators=(",", ":")) + "\n")
        proc.stdin.flush()
        line = proc.stdout.readline()
        if not line:
            raise InteropError("server closed stdin without a reply")
        transcript.append(f"< {line.strip()}")
        reply = json.loads(line)
        if "error" in reply:
            raise InteropError(f"JSON-RPC error {reply['error']}")
        return reply.get("result", {})

    def call_tool(request_id: int, name: str, arguments: dict) -> dict:
        result = rpc(request_id, "tools/call", {"name": name, "arguments": arguments})
        content = result.get("content") or []
        text = content[0].get("text", "") if content else ""
        if result.get("isError"):
            raise InteropError(f"{name} failed: {text}")
        try:
            return json.loads(text)
        except json.JSONDecodeError:
            return {"raw": text}

    try:
        # 1. initialize handshake
        init = rpc(1, "initialize", {"protocolVersion": "2025-03-26", "capabilities": {}})
        protocol_version = init.get("protocolVersion")
        server_info = init.get("serverInfo")
        transcript.append(f"# initialize: protocol={protocol_version} server={server_info}")
        if not protocol_version or not server_info:
            raise InteropError("initialize returned no protocolVersion/serverInfo")

        # 2. tools/list
        tools = rpc(2, "tools/list").get("tools", [])
        tool_names = [t["name"] for t in tools]
        transcript.append(f"# tools/list: {len(tools)} tools")
        required_tools = {
            "register_agent",
            "create_pairing",
            "pairing_preview",
            "join_pairing",
            "create_task",
            "claim_task",
            "verify_task",
            "complete_task",
        }
        missing = required_tools - set(tool_names)
        if missing:
            raise InteropError(f"tools/list missing required tools: {missing}")

        # 3. register two agents (stdio auto = trusted, but pass tokens explicitly)
        reg_a = call_tool(3, "register_agent", {"team_id": TEAM_ID, "agent_id": "agent-a", "role": "architect", "name": "Planner"})
        token_a = reg_a["actor_token"]
        reg_b = call_tool(4, "register_agent", {"team_id": TEAM_ID, "agent_id": "agent-b", "role": "builder", "name": "Builder"})
        token_b = reg_b["actor_token"]

        # pairing link + preview + join with consent=true
        pairing = call_tool(5, "create_pairing", {"initiator_id": "agent-a", "team_id": TEAM_ID, "capabilities_offered": ["read", "comment"], "actor_token": token_a})
        join_token = pairing["join_token"]
        preview = call_tool(6, "pairing_preview", {"token": join_token})
        if preview.get("status") != "issued":
            raise InteropError(f"pairing preview status != issued: {preview}")
        joined = call_tool(7, "join_pairing", {"token": join_token, "agent_id": "agent-b", "consent": True, "actor_token": token_b})
        if joined.get("state") not in ("active", "open"):
            raise InteropError(f"join state not active/open: {joined}")

        # 4. create task, claim, write artifact, verify evidence, complete
        task_result = call_tool(
            8,
            "create_task",
            {"team_id": TEAM_ID, "created_by": "agent-a", "title": "Interop review", "description": "Verify the retry boundary in handoff.txt", "scope": ["handoff.txt"], "preferred_agent": "agent-b", "idempotency_key": "interop-task-v1", "actor_token": token_a},
        )
        task_id = task_result["task"]["task_id"]
        claimed = call_tool(9, "claim_task", {"team_id": TEAM_ID, "agent_id": "agent-b", "task_id": task_id, "actor_token": token_b})
        fencing = claimed["fencing_token"]
        artifact = workspace / "handoff.txt"
        artifact.write_text("synthetic interop evidence\n", encoding="utf-8")
        verified = call_tool(
            10,
            "verify_task",
            {"team_id": TEAM_ID, "agent_id": "agent-b", "task_id": task_id, "fencing_token": fencing, "files": ["handoff.txt"], "checks": [{"name": "interop-check", "status": "passed", "evidence": "artifact present"}], "actor_token": token_b},
        )
        if not verified.get("passed"):
            raise InteropError(f"evidence gate did not pass: {verified}")
        completed = call_tool(11, "complete_task", {"team_id": TEAM_ID, "agent_id": "agent-b", "task_id": task_id, "fencing_token": fencing, "summary": "Interop verified", "actor_token": token_b})
        if completed.get("status") != "done":
            raise InteropError(f"complete status != done: {completed}")

        # 5. NEGATIVE case: reuse the one-use pairing link
        negative_transcript = []
        try:
            call_tool(12, "join_pairing", {"token": join_token, "agent_id": "agent-c", "consent": True})
            negative_transcript.append("JOIN_REUSE: NOT refused (unexpected)")
            refused = False
        except InteropError as exc:
            negative_transcript.append(f"JOIN_REUSE refused: {exc}")
            refused = True

        elapsed = time.monotonic() - started
        result = {
            "status": "ok",
            "transport": "stdio",
            "protocol_version": protocol_version,
            "server_info": server_info,
            "tool_count": len(tool_names),
            "pairing_preview": preview.get("status"),
            "join_state": joined.get("state"),
            "task_status": completed.get("status"),
            "evidence_passed": bool(verified.get("passed")),
            "negative_pairing_link_reuse_refused": refused,
            "time_to_verified_handoff_s": round(elapsed, 3),
            "transcript": transcript + negative_transcript,
        }
        print(json.dumps(result, indent=2))
        return 0 if refused else 2
    except InteropError as exc:
        print(json.dumps({"status": "failed", "error": str(exc), "transcript": transcript}, indent=2))
        return 1
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait(timeout=10)
        scratch.cleanup()


if __name__ == "__main__":
    raise SystemExit(main())
