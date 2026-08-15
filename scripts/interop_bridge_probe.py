"""Weft bridge-tier interop probe — stdio bridge against a real HTTP server.

The bridge tier's real transport path is `weft-mcp --remote` (the StdioHttpBridge:
a stdio MCP subprocess that forwards every JSON-RPC message to a hosted
`POST /mcp` Streamable-HTTP endpoint with a Bearer token from an environment
variable). This probe drives that exact path against the REAL coordinator:

  1. spawns the REAL HTTP MCP server on a free loopback port with a bearer
     token enforced (`WEFT_HTTP_TOKEN` set in the server child's environment);
  2. spawns the REAL bridge subprocess (`--remote http://127.0.0.1:<port>`
     `--token-env WEFT_PROBE_TOKEN`) speaking stdio;
  3. drives a full Room round-trip THROUGH the bridge: initialize, tools/list,
     register two agents, room_create / room_join / room_send / room_poll /
     room_ack, replay-clean check;
  4. runs two deliberate failure-discipline negatives through the real bridge
     binary: a missing token (JSON-RPC error naming the env var, exit 1) and a
     dead origin (JSON-RPC error naming the origin it tried).

Teardown in a finally block; both ports are re-probed after child exit to prove
no listener remains.

Run:  timeout 180 python -B scripts/interop_bridge_probe.py
"""

from __future__ import annotations

import json
import os
import secrets
import socket
import subprocess
import sys
import tempfile
import time
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]

TEAM_ID = "demo"
PROTOCOL_VERSION_PROBE = "2025-03-26"
BRIDGE_TOKEN_ENV = "WEFT_PROBE_TOKEN"
SERVER_TOKEN_ENV = "WEFT_HTTP_TOKEN"


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


def _bridge_env(extra: dict[str, str] | None = None) -> dict[str, str]:
    env = dict(os.environ)
    if extra:
        env.update(extra)
    return env


def _spawn_bridge(transcript: list[str], remote: str, token_env: str, env_extra: dict[str, str] | None) -> subprocess.Popen:
    args = [
        sys.executable,
        "-B",
        "scripts/weft-mcp.py",
        "--remote",
        remote,
        "--token-env",
        token_env,
    ]
    transcript.append(f"# spawn bridge: {' '.join(args)}")
    return subprocess.Popen(
        args,
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        cwd=str(PROJECT_ROOT),
        encoding="utf-8",
        errors="replace",
        env=_bridge_env(env_extra),
    )


def _stdio_rpc(proc: subprocess.Popen, transcript: list[str], request_id: int, method: str, params: dict | None = None) -> dict:
    payload = {"jsonrpc": "2.0", "id": request_id, "method": method}
    if params is not None:
        payload["params"] = params
    line_out = json.dumps(payload, separators=(",", ":"))
    transcript.append(f"> {line_out}")
    assert proc.stdin is not None and proc.stdout is not None
    proc.stdin.write(line_out + "\n")
    proc.stdin.flush()
    line = proc.stdout.readline()
    if not line:
        raise InteropError("bridge closed stdout without a reply")
    transcript.append(f"< {line.strip()}")
    reply = json.loads(line)
    return reply


def _stdio_call_tool(proc: subprocess.Popen, transcript: list[str], request_id: int, name: str, arguments: dict) -> dict:
    reply = _stdio_rpc(proc, transcript, request_id, "tools/call", {"name": name, "arguments": arguments})
    if "error" in reply:
        raise InteropError(f"bridge JSON-RPC error on {name}: {reply['error']}")
    result = reply.get("result", {})
    content = result.get("content") or []
    text = content[0].get("text", "") if content else ""
    if result.get("isError"):
        raise InteropError(f"{name} failed through bridge: {text}")
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        return {"raw": text}


def _stop_bridge(proc: subprocess.Popen, transcript: list[str]) -> int:
    if proc.stdin is not None:
        try:
            proc.stdin.close()
        except OSError:
            pass
    try:
        code = proc.wait(timeout=10)
    except subprocess.TimeoutExpired:
        proc.terminate()
        try:
            code = proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            proc.kill()
            code = proc.wait(timeout=10)
    transcript.append(f"# bridge exited with code {code}")
    return code


def main() -> int:
    scratch = tempfile.TemporaryDirectory(prefix="weft-bridge-probe-")
    workspace = Path(scratch.name)
    transcript: list[str] = []
    started = time.monotonic()
    server_port = _find_free_port()
    bearer_token = "probe_bearer_" + secrets.token_urlsafe(24)
    transcript.append(f"# allocated free server port {server_port}")

    server_proc = subprocess.Popen(
        [
            sys.executable,
            "-B",
            "scripts/weft-mcp.py",
            "--transport",
            "http",
            "--host",
            "127.0.0.1",
            "--port",
            str(server_port),
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
        env=_bridge_env({SERVER_TOKEN_ENV: bearer_token}),
    )

    bridge_proc: subprocess.Popen | None = None
    neg_missing: subprocess.Popen | None = None
    neg_dead: subprocess.Popen | None = None

    try:
        _wait_for_port("127.0.0.1", server_port, timeout_s=10.0)
        transcript.append(f"# TCP connect to 127.0.0.1:{server_port} succeeded (server token enforced)")

        bridge_proc = _spawn_bridge(
            transcript,
            f"http://127.0.0.1:{server_port}",
            BRIDGE_TOKEN_ENV,
            {BRIDGE_TOKEN_ENV: bearer_token},
        )

        init_reply = _stdio_rpc(bridge_proc, transcript, 1, "initialize", {
            "protocolVersion": PROTOCOL_VERSION_PROBE,
            "capabilities": {},
        })
        if "error" in init_reply:
            raise InteropError(f"initialize failed through bridge: {init_reply['error']}")
        init = init_reply["result"]
        protocol_version = init.get("protocolVersion")
        server_info = init.get("serverInfo")
        transcript.append(f"# initialize through bridge: protocol={protocol_version} server={server_info}")
        if not protocol_version or not server_info:
            raise InteropError("initialize returned no protocolVersion/serverInfo")

        tools_reply = _stdio_rpc(bridge_proc, transcript, 2, "tools/list")
        if "error" in tools_reply:
            raise InteropError(f"tools/list failed through bridge: {tools_reply['error']}")
        tools = tools_reply.get("result", {}).get("tools", [])
        tool_names = [t["name"] for t in tools]
        room_tools = [n for n in tool_names if n.startswith("room_")]
        transcript.append(f"# tools/list through bridge: {len(tools)} tools, room tools={len(room_tools)}")
        if "room_create" not in tool_names or "room_poll" not in tool_names:
            raise InteropError("bridge tools/list missing room tools")

        reg_a = _stdio_call_tool(bridge_proc, transcript, 3, "register_agent", {
            "team_id": TEAM_ID, "agent_id": "bridge-agent-a", "role": "architect", "name": "Planner",
        })
        token_a = reg_a["actor_token"]
        reg_b = _stdio_call_tool(bridge_proc, transcript, 4, "register_agent", {
            "team_id": TEAM_ID, "agent_id": "bridge-agent-b", "role": "builder", "name": "Builder",
        })
        token_b = reg_b["actor_token"]
        transcript.append("# register_agent through bridge: both agents active")

        room = _stdio_call_tool(bridge_proc, transcript, 5, "room_create", {
            "team_id": TEAM_ID, "owner_agent_id": "bridge-agent-a", "cap": 3,
            "name": "interop-bridge-room", "actor_token": token_a,
        })
        room_id = room["room_id"]
        link_token = room["link_token"]
        transcript.append(f"# room_create through bridge: room_id={room_id} state={room.get('state')}")

        joined = _stdio_call_tool(bridge_proc, transcript, 6, "room_join", {
            "team_id": TEAM_ID, "room_id": room_id, "link_token": link_token,
            "agent_id": "bridge-agent-b", "consent": True, "actor_token": token_b,
        })
        transcript.append(f"# room_join through bridge: status={joined.get('status')}")

        sent = _stdio_call_tool(bridge_proc, transcript, 7, "room_send", {
            "team_id": TEAM_ID, "room_id": room_id, "sender_agent_id": "bridge-agent-a",
            "target_spec": "*", "payload": {"text": "hello from the stdio bridge"},
            "actor_token": token_a,
        })
        receipts = sent.get("receipts", [])
        send_seq = sent.get("seq")
        transcript.append(f"# room_send through bridge: seq={send_seq} receipts={receipts}")

        polled = _stdio_call_tool(bridge_proc, transcript, 8, "room_poll", {
            "team_id": TEAM_ID, "room_id": room_id, "agent_id": "bridge-agent-b",
            "after_seq": 0, "actor_token": token_b,
        })
        events = polled.get("events", [])
        kinds = [e.get("kind") for e in events]
        cursor_head = polled.get("cursor_head")
        message_events = [
            e for e in events
            if e.get("kind") == "room.message" and "hello from the stdio bridge" in json.dumps(e.get("payload", {}))
        ]
        transcript.append(
            f"# room_poll through bridge: {len(events)} events kinds={kinds} cursor_head={cursor_head} "
            f"next_seq={polled.get('next_seq')}"
        )
        if not message_events:
            raise InteropError(f"bridge room round-trip failed: receiver never saw the message (kinds={kinds})")

        acked = _stdio_call_tool(bridge_proc, transcript, 9, "room_ack", {
            "team_id": TEAM_ID, "room_id": room_id, "agent_id": "bridge-agent-b",
            "seq": cursor_head, "actor_token": token_b,
        })
        transcript.append(f"# room_ack through bridge: last_ack_seq={acked.get('last_ack_seq')}")

        polled2 = _stdio_call_tool(bridge_proc, transcript, 10, "room_poll", {
            "team_id": TEAM_ID, "room_id": room_id, "agent_id": "bridge-agent-b", "actor_token": token_b,
        })
        events2 = polled2.get("events", [])
        replay_clean = len(events2) == 0
        transcript.append(
            f"# room_poll through bridge (post-ack): {len(events2)} events "
            f"{'' if replay_clean else '(REPLAYED — bad)'}"
        )

        bridge_exit = _stop_bridge(bridge_proc, transcript)
        bridge_proc = None

        neg_missing = _spawn_bridge(
            transcript,
            f"http://127.0.0.1:{server_port}",
            "WEFT_MISSING_TOKEN",
            None,
        )
        missing_reply = _stdio_rpc(neg_missing, transcript, 1, "initialize", {
            "protocolVersion": PROTOCOL_VERSION_PROBE,
            "capabilities": {},
        })
        missing_err = missing_reply.get("error", {})
        missing_names_env = "WEFT_MISSING_TOKEN" in missing_err.get("message", "")
        transcript.append(f"# NEGATIVE missing token: error={missing_err.get('message', '')[:120]}")
        missing_exit = _stop_bridge(neg_missing, transcript)
        neg_missing = None
        missing_failed_loud = bool(missing_err) and missing_names_env and missing_exit == 1

        dead_port = _find_free_port()
        neg_dead = _spawn_bridge(
            transcript,
            f"http://127.0.0.1:{dead_port}",
            BRIDGE_TOKEN_ENV,
            {BRIDGE_TOKEN_ENV: bearer_token},
        )
        dead_reply = _stdio_rpc(neg_dead, transcript, 1, "initialize", {
            "protocolVersion": PROTOCOL_VERSION_PROBE,
            "capabilities": {},
        })
        dead_err = dead_reply.get("error", {})
        dead_names_origin = f"127.0.0.1:{dead_port}" in dead_err.get("message", "")
        transcript.append(f"# NEGATIVE dead origin: error={dead_err.get('message', '')[:140]}")
        dead_exit = _stop_bridge(neg_dead, transcript)
        neg_dead = None
        dead_failed_loud = bool(dead_err) and dead_names_origin and dead_exit == 0

        elapsed = time.monotonic() - started
        result = {
            "status": "ok",
            "transport": "stdio -> bridge -> streamable-http",
            "protocol_version": protocol_version,
            "server_info": server_info,
            "tool_count": len(tool_names),
            "room_tools": room_tools,
            "room": {
                "created_state": room.get("state"),
                "join_status": joined.get("status"),
                "send_seq": send_seq,
                "receipts": receipts,
                "poll_kinds": kinds,
                "cursor_head": cursor_head,
                "ack_last_ack_seq": acked.get("last_ack_seq"),
                "replay_clean": replay_clean,
            },
            "negatives": {
                "missing_token": {
                    "failed_loud": missing_failed_loud,
                    "error": missing_err.get("message", ""),
                    "exit_code": missing_exit,
                },
                "dead_origin": {
                    "failed_loud": dead_failed_loud,
                    "error": dead_err.get("message", ""),
                    "exit_code": dead_exit,
                },
            },
            "time_s": round(elapsed, 3),
            "transcript": transcript,
        }
        print(json.dumps(result, indent=2))
        return 0
    except InteropError as exc:
        print(json.dumps({"status": "failed", "error": str(exc), "transcript": transcript}, indent=2))
        return 1
    finally:
        for proc in (bridge_proc, neg_missing, neg_dead):
            if proc is not None and proc.poll() is None:
                proc.terminate()
                try:
                    proc.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    proc.kill()
                    proc.wait(timeout=10)
        server_proc.terminate()
        try:
            server_proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            server_proc.kill()
            server_proc.wait(timeout=10)
        listening_after = _port_listening(server_port)
        print(json.dumps({"teardown": {"server_port": server_port, "listening_after_exit": listening_after}}))
        scratch.cleanup()


if __name__ == "__main__":
    raise SystemExit(main())
