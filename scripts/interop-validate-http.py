"""Weft real-MCP Streamable HTTP protocol validation driver.

Spawns the coordinator itself over HTTP (MCP Streamable HTTP transport), speaks
real MCP JSON-RPC over POST /mcp using stdlib http.client (initialize /
tools/list / tools/call), exercises the full pairing + verified-handoff
lifecycle, includes a deliberate negative case, and tears the child down in a
finally block. This is a genuine remote client: it does NOT import
weft_mcp internals or call the dispatcher directly.

Run:  timeout 180 python -B scripts/interop-validate-http.py
"""

from __future__ import annotations

import json
import socket
import subprocess
import sys
import tempfile
import time
import http.client
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "src"))

TEAM_ID = "demo"
REQUEST_TIMEOUT_S = 60
PROTOCOL_VERSION_PROBE = "2025-03-26"


def _find_free_port() -> int:
    """Bind a socket to port 0, read the allocated port, close it, return it.
    Deterministic, no collision: the OS hands us a free ephemeral port."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _wait_for_port(host: str, port: int, timeout_s: float = 10.0) -> None:
    """Poll TCP connect until the server accepts or we time out."""
    deadline = time.monotonic() + timeout_s
    last_err: Exception | None = None
    while time.monotonic() < deadline:
        try:
            with socket.create_connection((host, port), timeout=1.0):
                return
        except OSError as exc:
            last_err = exc
            time.sleep(0.1)
    raise RuntimeError(f"server did not accept connections on {host}:{port} within {timeout_s}s ({last_err})")


class InteropError(RuntimeError):
    pass


class HTTPClient:
    """Thin stdlib wrapper around POST /mcp for one JSON-RPC exchange at a time."""

    def __init__(self, host: str, port: int):
        self.host = host
        self.port = port
        self._conn: http.client.HTTPConnection | None = None

    def _connection(self) -> http.client.HTTPConnection:
        if self._conn is None:
            self._conn = http.client.HTTPConnection(self.host, self.port, timeout=REQUEST_TIMEOUT_S)
        return self._conn

    def close(self) -> None:
        if self._conn is not None:
            try:
                self._conn.close()
            except OSError:
                pass
            self._conn = None

    def rpc(self, transcript: list[str], request_id: int, method: str, params: dict | None = None) -> dict:
        payload = {"jsonrpc": "2.0", "id": request_id, "method": method}
        if params is not None:
            payload["params"] = params
        body = json.dumps(payload, separators=(",", ":"))
        transcript.append(f"> {body}")
        conn = self._connection()
        try:
            conn.request(
                "POST",
                "/mcp",
                body=body,
                headers={"Content-Type": "application/json", "Accept": "application/json"},
            )
            resp = conn.getresponse()
            resp_body = resp.read().decode("utf-8", errors="replace")
        except (OSError, http.client.HTTPException) as exc:
            raise InteropError(f"HTTP transport error on {method}: {exc}") from exc
        transcript.append(f"< HTTP {resp.status} {resp.reason}: {resp_body}")
        if resp.status != 200:
            raise InteropError(f"HTTP {resp.status} on {method}: {resp_body}")
        reply = json.loads(resp_body)
        if "error" in reply:
            raise InteropError(f"JSON-RPC error {reply['error']}")
        return reply.get("result", {})


def call_tool(client: HTTPClient, transcript: list[str], request_id: int, name: str, arguments: dict) -> dict:
    result = client.rpc(transcript, request_id, "tools/call", {"name": name, "arguments": arguments})
    content = result.get("content") or []
    text = content[0].get("text", "") if content else ""
    if result.get("isError"):
        raise InteropError(f"{name} failed: {text}")
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        return {"raw": text}


def main() -> int:
    scratch = tempfile.TemporaryDirectory(prefix="weft-interop-http-")
    workspace = Path(scratch.name)
    transcript: list[str] = []
    started = time.monotonic()
    client: HTTPClient | None = None

    port = _find_free_port()
    transcript.append(f"# allocated free port {port}")

    proc = subprocess.Popen(
        [
            sys.executable,
            "-B",
            "scripts/weft-mcp.py",
            "--transport",
            "http",
            "--host",
            "127.0.0.1",
            "--port",
            str(port),
            "--team-id",
            TEAM_ID,
            "--workspace",
            str(workspace),
            "--state",
            str(workspace / ".weft" / "state.db"),
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

    try:
        _wait_for_port("127.0.0.1", port, timeout_s=10.0)
        transcript.append(f"# TCP connect to 127.0.0.1:{port} succeeded")
        client = HTTPClient("127.0.0.1", port)

        # 1. initialize handshake
        init = client.rpc(transcript, 1, "initialize", {
            "protocolVersion": PROTOCOL_VERSION_PROBE,
            "capabilities": {},
        })
        protocol_version = init.get("protocolVersion")
        server_info = init.get("serverInfo")
        transcript.append(f"# initialize: protocol={protocol_version} server={server_info}")
        if not protocol_version or not server_info:
            raise InteropError("initialize returned no protocolVersion/serverInfo")

        # 2. tools/list
        tools = client.rpc(transcript, 2, "tools/list").get("tools", [])
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

        # 3. register two agents
        reg_a = call_tool(client, transcript, 3, "register_agent", {
            "team_id": TEAM_ID, "agent_id": "agent-a", "role": "architect", "name": "Planner",
        })
        token_a = reg_a["actor_token"]
        reg_b = call_tool(client, transcript, 4, "register_agent", {
            "team_id": TEAM_ID, "agent_id": "agent-b", "role": "builder", "name": "Builder",
        })
        token_b = reg_b["actor_token"]

        # pairing link + preview + join with consent=true
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

        # 4. create task, claim, write artifact, verify evidence, complete
        task_result = call_tool(client, transcript, 8, "create_task", {
            "team_id": TEAM_ID, "created_by": "agent-a",
            "title": "Interop review", "description": "Verify the retry boundary in handoff.txt",
            "scope": ["handoff.txt"], "preferred_agent": "agent-b",
            "idempotency_key": "interop-http-task-v1", "actor_token": token_a,
        })
        task_id = task_result["task"]["task_id"]
        claimed = call_tool(client, transcript, 9, "claim_task", {
            "team_id": TEAM_ID, "agent_id": "agent-b", "task_id": task_id, "actor_token": token_b,
        })
        fencing = claimed["fencing_token"]
        artifact = workspace / "handoff.txt"
        artifact.write_text("synthetic interop http evidence\n", encoding="utf-8")
        verified = call_tool(client, transcript, 10, "verify_task", {
            "team_id": TEAM_ID, "agent_id": "agent-b", "task_id": task_id,
            "fencing_token": fencing, "files": ["handoff.txt"],
            "checks": [{"name": "interop-check", "status": "passed", "evidence": "artifact present"}],
            "actor_token": token_b,
        })
        if not verified.get("passed"):
            raise InteropError(f"evidence gate did not pass: {verified}")
        completed = call_tool(client, transcript, 11, "complete_task", {
            "team_id": TEAM_ID, "agent_id": "agent-b", "task_id": task_id,
            "fencing_token": fencing, "summary": "Interop HTTP verified", "actor_token": token_b,
        })
        if completed.get("status") != "done":
            raise InteropError(f"complete status != done: {completed}")

        # 5. NEGATIVE case: reuse the one-use pairing link
        negative_transcript = []
        refused = False
        try:
            call_tool(client, transcript, 12, "join_pairing", {
                "token": join_token, "agent_id": "agent-c", "consent": True,
            })
            negative_transcript.append("JOIN_REUSE: NOT refused (unexpected)")
            refused = False
        except InteropError as exc:
            negative_transcript.append(f"JOIN_REUSE refused: {exc}")
            refused = True

        elapsed = time.monotonic() - started
        result = {
            "status": "ok",
            "transport": "http",
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
        if client is not None:
            client.close()
        proc.terminate()
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait(timeout=10)
        scratch.cleanup()


if __name__ == "__main__":
    raise SystemExit(main())
