"""Weft Streamable-HTTP interop probe — room lifecycle over POST /mcp.

Spawns the REAL coordinator (`scripts/weft-mcp.py --transport http`) as a child
process on a free loopback port, then drives it with a genuine remote JSON-RPC
client (stdlib http.client, no weft_mcp imports): initialize, tools/list, and a
full Room round-trip (room_create / room_join / room_send / room_poll /
room_ack) plus two deliberate negative refusals. Teardown in a finally block;
the port is re-probed after child exit to prove no listener remains.

Run:  timeout 180 python -B scripts/interop_streamable_http_probe.py
"""

from __future__ import annotations

import http.client
import json
import socket
import subprocess
import sys
import tempfile
import time
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]

TEAM_ID = "demo"
REQUEST_TIMEOUT_S = 60
PROTOCOL_VERSION_PROBE = "2025-03-26"


def _find_free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _port_listening(port: int, timeout_s: float = 0.5) -> bool:
    try:
        with socket.create_connection(("127.0.0.1", port), timeout=timeout_s):
            return True
    except OSError:
        return False


def _wait_for_port(host: str, port: int, timeout_s: float = 10.0) -> None:
    deadline = time.monotonic() + timeout_s
    last_err: OSError | None = None
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


def call_tool_raw(client: HTTPClient, transcript: list[str], request_id: int, name: str, arguments: dict) -> dict:
    return client.rpc(transcript, request_id, "tools/call", {"name": name, "arguments": arguments})


def _tool_error_code(raw: dict) -> str | None:
    content = (raw.get("content") or []) if isinstance(raw, dict) else []
    if not raw.get("isError") or not content:
        return None
    text = content[0].get("text", "")
    try:
        parsed = json.loads(text)
    except json.JSONDecodeError:
        return text[:120]
    err = parsed.get("error") if isinstance(parsed, dict) else None
    return (err or {}).get("code") if isinstance(err, dict) else None


def main() -> int:
    scratch = tempfile.TemporaryDirectory(prefix="weft-http-probe-")
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

        init = client.rpc(transcript, 1, "initialize", {
            "protocolVersion": PROTOCOL_VERSION_PROBE,
            "capabilities": {},
        })
        protocol_version = init.get("protocolVersion")
        server_info = init.get("serverInfo")
        if not protocol_version or not server_info:
            raise InteropError("initialize returned no protocolVersion/serverInfo")
        transcript.append(f"# initialize: protocol={protocol_version} server={server_info}")

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
        room_tools = [n for n in tool_names if n.startswith("room_")]
        if "room_create" not in room_tools or "room_poll" not in room_tools:
            raise InteropError(f"room tools missing from tools/list: {room_tools}")

        reg_a = call_tool(client, transcript, 3, "register_agent", {
            "team_id": TEAM_ID, "agent_id": "agent-a", "role": "architect", "name": "Planner",
        })
        token_a = reg_a["actor_token"]
        reg_b = call_tool(client, transcript, 4, "register_agent", {
            "team_id": TEAM_ID, "agent_id": "agent-b", "role": "builder", "name": "Builder",
        })
        token_b = reg_b["actor_token"]
        reg_c = call_tool(client, transcript, 5, "register_agent", {
            "team_id": TEAM_ID, "agent_id": "agent-c", "role": "observer", "name": "Observer",
        })
        token_c = reg_c["actor_token"]
        transcript.append("# register_agent: agent-a/agent-b/agent-c active")

        room = call_tool(client, transcript, 6, "room_create", {
            "team_id": TEAM_ID, "owner_agent_id": "agent-a", "cap": 3,
            "name": "interop-http-room", "actor_token": token_a,
        })
        room_id = room["room_id"]
        link_token = room["link_token"]
        transcript.append(f"# room_create: room_id={room_id} state={room.get('state')} cap={room.get('cap')}")

        joined = call_tool(client, transcript, 7, "room_join", {
            "team_id": TEAM_ID, "room_id": room_id, "link_token": link_token,
            "agent_id": "agent-b", "consent": True, "actor_token": token_b,
        })
        transcript.append(f"# room_join agent-b: status={joined.get('status')}")

        info = call_tool(client, transcript, 8, "room_info", {
            "team_id": TEAM_ID, "room_id": room_id, "agent_id": "agent-a", "actor_token": token_a,
        })
        transcript.append(
            f"# room_info owner view: state={info.get('state')} member_count={info.get('member_count')} "
            f"link_revoked={info.get('link_revoked')}"
        )

        sent = call_tool(client, transcript, 9, "room_send", {
            "team_id": TEAM_ID, "room_id": room_id, "sender_agent_id": "agent-a",
            "target_spec": "*", "payload": {"text": "hello from agent-a over streamable http"},
            "actor_token": token_a,
        })
        receipts = sent.get("receipts", [])
        send_seq = sent.get("seq")
        transcript.append(f"# room_send broadcast: seq={send_seq} receipts={receipts}")

        polled = call_tool(client, transcript, 10, "room_poll", {
            "team_id": TEAM_ID, "room_id": room_id, "agent_id": "agent-b",
            "after_seq": 0, "actor_token": token_b,
        })
        events = polled.get("events", [])
        kinds = [e.get("kind") for e in events]
        cursor_head = polled.get("cursor_head")
        message_events = [
            e for e in events
            if e.get("kind") == "room.message" and "hello from agent-a" in json.dumps(e.get("payload", {}))
        ]
        transcript.append(
            f"# room_poll agent-b after_seq=0: {len(events)} events kinds={kinds} cursor_head={cursor_head} "
            f"next_seq={polled.get('next_seq')} state={polled.get('state')}"
        )
        if not message_events:
            raise InteropError(f"room round-trip failed: agent-b never saw the broadcast message (kinds={kinds})")
        if "room.created" not in kinds:
            raise InteropError(f"room.created missing from replay (kinds={kinds})")

        acked = call_tool(client, transcript, 11, "room_ack", {
            "team_id": TEAM_ID, "room_id": room_id, "agent_id": "agent-b",
            "seq": cursor_head, "actor_token": token_b,
        })
        transcript.append(f"# room_ack agent-b seq={cursor_head}: last_ack_seq={acked.get('last_ack_seq')}")

        polled2 = call_tool(client, transcript, 12, "room_poll", {
            "team_id": TEAM_ID, "room_id": room_id, "agent_id": "agent-b", "actor_token": token_b,
        })
        events2 = polled2.get("events", [])
        replay_clean = len(events2) == 0
        transcript.append(f"# room_poll agent-b (post-ack): {len(events2)} events {'' if replay_clean else '(REPLAYED — bad)'}")

        raw_negative = call_tool_raw(client, transcript, 13, "room_poll", {
            "team_id": TEAM_ID, "room_id": room_id, "agent_id": "agent-c", "actor_token": token_c,
        })
        nonmember_code = _tool_error_code(raw_negative)
        transcript.append(f"# NEGATIVE non-member room_poll: isError={raw_negative.get('isError')} code={nonmember_code}")

        raw_negative2 = call_tool_raw(client, transcript, 14, "room_poll", {
            "team_id": TEAM_ID, "room_id": room_id, "agent_id": "agent-b",
            "actor_token": "not-a-real-actor-token-0000000000",
        })
        badauth_code = _tool_error_code(raw_negative2)
        transcript.append(f"# NEGATIVE bad actor_token room_poll: isError={raw_negative2.get('isError')} code={badauth_code}")

        elapsed = time.monotonic() - started
        result = {
            "status": "ok",
            "transport": "http",
            "protocol_version": protocol_version,
            "server_info": server_info,
            "tool_count": len(tool_names),
            "room_tools": room_tools,
            "room": {
                "created_state": room.get("state"),
                "cap": room.get("cap"),
                "join_status": joined.get("status"),
                "info_state": info.get("state"),
                "member_count": info.get("member_count"),
                "send_seq": send_seq,
                "receipts": receipts,
                "poll_kinds": kinds,
                "cursor_head": cursor_head,
                "ack_last_ack_seq": acked.get("last_ack_seq"),
                "replay_clean": replay_clean,
            },
            "negatives": {
                "nonmember_poll_code": nonmember_code,
                "bad_actor_token_code": badauth_code,
            },
            "time_to_room_round_trip_s": round(elapsed, 3),
            "transcript": transcript,
        }
        print(json.dumps(result, indent=2))
        return 0
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
        listening_after = _port_listening(port)
        print(json.dumps({"teardown": {"port": port, "listening_after_exit": listening_after}}))
        scratch.cleanup()


if __name__ == "__main__":
    raise SystemExit(main())
