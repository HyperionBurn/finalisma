"""Evidence driver: two independent stdio bridges share one hosted room.

This starts one local hosted cloud service and two separate
``scripts/weft-mcp.py --remote`` subprocesses with different account tokens.
Bridge A creates a room, bridge B joins through the returned link, A sends a
message, and B polls it.  The output is intentionally concrete so the run can
be attached to a release handover without claiming more than it proves.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import threading
import uuid
from http.server import ThreadingHTTPServer
from pathlib import Path
from urllib import request

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from weft_cloud.service import WeftCloudService, _CloudHTTPHandler  # noqa: E402
from weft_cloud.storage import SqliteWalBackend  # noqa: E402


def _post(base: str, path: str, body: dict, token: str | None = None) -> tuple[int, dict]:
    data = json.dumps(body, separators=(",", ":")).encode("utf-8")
    req = request.Request(base + path, data=data, method="POST")
    req.add_header("Content-Type", "application/json")
    if token:
        req.add_header("Authorization", f"Bearer {token}")
    with request.urlopen(req, timeout=10) as response:
        raw = response.read()
        return response.status, json.loads(raw.decode("utf-8")) if raw else {}


def _rpc(process: subprocess.Popen[str], call: dict) -> dict:
    assert process.stdin is not None
    assert process.stdout is not None
    process.stdin.write(json.dumps(call, separators=(",", ":")) + "\n")
    process.stdin.flush()
    line = process.stdout.readline()
    if not line:
        raise RuntimeError("bridge exited without a JSON-RPC response")
    response = json.loads(line)
    if "error" in response:
        raise RuntimeError(f"bridge JSON-RPC error: {response['error']}")
    return response


def _spawn(base: str, token_env: str, token: str) -> subprocess.Popen[str]:
    env = dict(os.environ)
    env[token_env] = token
    env["PYTHONUNBUFFERED"] = "1"
    return subprocess.Popen(
        [
            sys.executable,
            "-B",
            "scripts/weft-mcp.py",
            "--remote",
            base,
            "--token-env",
            token_env,
        ],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        text=True,
        encoding="utf-8",
        errors="replace",
        cwd=str(ROOT),
        env=env,
        bufsize=1,
    )


def _close(process: subprocess.Popen[str]) -> None:
    if process.stdin is not None:
        try:
            process.stdin.close()
        except OSError:
            pass
    try:
        process.terminate()
        process.wait(timeout=10)
    except (OSError, subprocess.TimeoutExpired):
        process.kill()
        process.wait(timeout=10)


def main() -> int:
    temp_root = ROOT / ".tmp" / f"two-bridge-{os.getpid()}-{uuid.uuid4().hex}"
    temp_root.mkdir(parents=True, exist_ok=False)
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), _CloudHTTPHandler)
    base = f"http://127.0.0.1:{httpd.server_address[1]}"
    service = WeftCloudService(SqliteWalBackend(str(temp_root / "cloud.db")))
    _CloudHTTPHandler.service = service
    server_thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    server_thread.start()
    processes: list[subprocess.Popen[str]] = []
    try:
        _, owner = _post(
            base,
            "/v1/auth/signup",
            {"email": "owner@example.com", "password": "password-123"},
        )
        _, joiner = _post(
            base,
            "/v1/auth/signup",
            {"email": "joiner@example.com", "password": "password-123"},
        )
        print(f"owner account:  {owner['account_id']}")
        print(f"joiner account: {joiner['account_id']}")

        bridge_a = _spawn(base, "WEFT_TOKEN_A", owner["session_token"])
        bridge_b = _spawn(base, "WEFT_TOKEN_B", joiner["session_token"])
        processes = [bridge_a, bridge_b]

        init_a = _rpc(
            bridge_a,
            {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "initialize",
                "params": {"protocolVersion": "2025-03-26", "capabilities": {}},
            },
        )
        print(
            "A initialize  -> "
            f"{init_a['result']['serverInfo']['name']} "
            f"protocol={init_a['result']['protocolVersion']}"
        )

        created = _rpc(
            bridge_a,
            {
                "jsonrpc": "2.0",
                "id": 2,
                "method": "tools/call",
                "params": {"name": "room_create", "arguments": {"cap": 4}},
            },
        )
        room = created["result"]["structuredContent"]
        room_id = room["room_id"]
        link_token = room["link_token"]
        print(f"A room_create -> room_id={room_id} link={link_token[:12]}...")

        init_b = _rpc(
            bridge_b,
            {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "initialize",
                "params": {"protocolVersion": "2025-03-26", "capabilities": {}},
            },
        )
        print(
            "B initialize  -> "
            f"{init_b['result']['serverInfo']['name']} "
            f"protocol={init_b['result']['protocolVersion']}"
        )

        joined = _rpc(
            bridge_b,
            {
                "jsonrpc": "2.0",
                "id": 2,
                "method": "tools/call",
                "params": {
                    "name": "room_join",
                    "arguments": {
                        "room_id": room_id,
                        "link_token": link_token,
                        "consent": True,
                    },
                },
            },
        )
        print(f"B room_join   -> status={joined['result']['structuredContent']['status']}")

        sent = _rpc(
            bridge_a,
            {
                "jsonrpc": "2.0",
                "id": 3,
                "method": "tools/call",
                "params": {
                    "name": "room_send",
                    "arguments": {
                        "room_id": room_id,
                        "target_spec": "*",
                        "payload": {"kind": "message", "text": "hello from bridge A"},
                    },
                },
            },
        )
        print(
            "A room_send   -> "
            f"receipts={len(sent['result']['structuredContent']['receipts'])}"
        )

        polled = _rpc(
            bridge_b,
            {
                "jsonrpc": "2.0",
                "id": 3,
                "method": "tools/call",
                "params": {"name": "room_poll", "arguments": {"room_id": room_id}},
            },
        )
        events = polled["result"]["structuredContent"]["events"]
        messages = [event for event in events if event["kind"] == "room.message"]
        delivered = any(
            event.get("payload", {}).get("payload", {}).get("text")
            == "hello from bridge A"
            for event in messages
        )
        print(f"B room_poll   -> messages={len(messages)} delivered={delivered}")
        print(f"RESULT: two-bridge shared-room message delivered: {delivered}")
        return 0 if delivered else 1
    finally:
        for process in processes:
            _close(process)
        httpd.shutdown()
        httpd.server_close()
        service.backend.close()
        shutil.rmtree(temp_root, ignore_errors=True)


if __name__ == "__main__":
    raise SystemExit(main())
