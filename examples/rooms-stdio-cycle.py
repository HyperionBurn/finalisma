"""Full room lifecycle against the real MCP server over stdio.

Drives the self-hosted coordinator (``scripts/weft-mcp.py``, stdio transport)
through the complete room cycle: register -> create -> join -> send -> poll ->
ack -> receipts -> remove_member -> leave, then proves the removed member is
refused on its next request and that the child process left no listener
behind.

This is the same wire surface the Weft SDK maps onto (``create_room``,
``join_room``, ``send``, ``room_poll``, ``room_ack``, ``room_receipts``,
``room_remove_member``, ``leave_room``) but spoken raw, the way an MCP host
speaks it: newline-delimited JSON-RPC 2.0 frames on stdin/stdout. Stdlib
only; no third-party packages.

Usage (from the repo root)::

    python -B examples/rooms-stdio-cycle.py

Wrap it in a timeout (``timeout 120`` on Unix, or the tool-level timeout)
as a belt-and-braces guard. Scratch state is written under the system temp
directory, never into the repo.
"""

import json
import subprocess
import sys
import tempfile
import time
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
SERVER_SCRIPT = REPO_ROOT / "scripts" / "weft-mcp.py"

TEAM = "demo"
AGENT_A = "agent-a"
AGENT_B = "agent-b"

_rpc_id = 0
_results = []


def rpc(proc, name, arguments):
    """One newline-delimited JSON-RPC tools/call round-trip."""
    global _rpc_id
    _rpc_id += 1
    request = {
        "jsonrpc": "2.0",
        "id": _rpc_id,
        "method": "tools/call",
        "params": {"name": name, "arguments": arguments},
    }
    proc.stdin.write(json.dumps(request) + "\n")
    proc.stdin.flush()
    deadline = time.monotonic() + 30.0
    while time.monotonic() < deadline:
        line = proc.stdout.readline()
        if not line:
            raise RuntimeError(f"{name}: server closed stdout mid-call")
        reply = json.loads(line)
        if reply.get("id") != _rpc_id:
            continue
        result = reply.get("result", {})
        if result.get("isError"):
            text = result["content"][0]["text"]
            raise RuntimeError(f"{name} failed: {text}")
        content = result.get("structuredContent", result)
        _results.append((name, content))
        return content
    raise TimeoutError(f"{name}: no response within 30s")


def netstat_snapshot():
    """(set of LISTENING lines, set of lines mentioning our pid)."""
    try:
        out = subprocess.run(
            ["netstat", "-ano"], capture_output=True, text=True, timeout=15
        ).stdout
    except (OSError, subprocess.TimeoutExpired):
        return set(), set()
    lines = set(out.splitlines())
    listening = {ln for ln in lines if "LISTENING" in ln}
    return listening, lines


def main():
    if not SERVER_SCRIPT.exists():
        raise SystemExit(f"server not found: {SERVER_SCRIPT}")
    scratch_dir = Path(tempfile.mkdtemp(prefix="weft-rooms-example-"))
    state_db = scratch_dir / "state.db"

    listening_before, _ = netstat_snapshot()
    proc = subprocess.Popen(
        [sys.executable, "-B", str(SERVER_SCRIPT), "--state", str(state_db)],
        cwd=str(REPO_ROOT),
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        text=True,
        encoding="utf-8",
    )
    try:
        proto = rpc(proc, "protocol", {})
        print(f"[protocol] {proto.get('protocol')} v{proto.get('version')}")

        reg_a = rpc(proc, "register_agent", {
            "team_id": TEAM, "agent_id": AGENT_A, "name": "Planner",
            "role": "coordinator", "capabilities": ["planning"],
        })
        tok_a = reg_a["actor_token"]
        print(f"[register] {AGENT_A} registered, actor_token {tok_a[:8]}... (len {len(tok_a)})")

        reg_b = rpc(proc, "register_agent", {
            "team_id": TEAM, "agent_id": AGENT_B, "name": "Builder",
            "role": "coding", "capabilities": ["coding"],
        })
        tok_b = reg_b["actor_token"]
        print(f"[register] {AGENT_B} registered, actor_token {tok_b[:8]}... (len {len(tok_b)})")

        room = rpc(proc, "room_create", {
            "team_id": TEAM, "owner_agent_id": AGENT_A, "cap": 3,
            "name": "example-cycle", "ttl_seconds": 3600, "actor_token": tok_a,
        })
        room_id = room["room_id"]
        link_token = room["link_token"]
        print(f"[room_create] room={room_id} state={room['state']} cap={room['cap']} "
              f"owner={room['owner_agent_id']} link={room.get('shareable_link')}")

        ja = rpc(proc, "room_join", {
            "team_id": TEAM, "room_id": room_id, "link_token": link_token,
            "agent_id": AGENT_A, "consent": True, "capabilities": ["planning"],
            "actor_token": tok_a,
        })
        print(f"[room_join] owner re-join (idempotent): status={ja['status']} cursor={ja['cursor']}")

        jb = rpc(proc, "room_join", {
            "team_id": TEAM, "room_id": room_id, "link_token": link_token,
            "agent_id": AGENT_B, "consent": True, "capabilities": ["coding"],
            "actor_token": tok_b,
        })
        print(f"[room_join] {AGENT_B}: status={jb['status']} cursor={jb['cursor']}")

        sent = rpc(proc, "room_send", {
            "team_id": TEAM, "room_id": room_id, "sender_agent_id": AGENT_A,
            "target_spec": AGENT_B, "payload": {"text": "plan approved"},
            "message_kind": "result", "exclude_sender": True, "actor_token": tok_a,
        })
        entry_ids = [r_["entry_id"] for r_ in sent["receipts"]]
        print(f"[room_send] {AGENT_A} -> {AGENT_B} seq={sent['seq']} "
              f"receipts={[(r['agent_id'], r['read_status']) for r in sent['receipts']]}")

        polled = rpc(proc, "room_poll", {
            "team_id": TEAM, "room_id": room_id, "agent_id": AGENT_B,
            "after_seq": 0, "actor_token": tok_b,
        })
        kinds = [(ev["seq"], ev["kind"], ev.get("message_kind")) for ev in polled["events"]]
        print(f"[room_poll] {AGENT_B} sees {len(polled['events'])} events {kinds} "
              f"next_seq={polled['next_seq']} cursor_head={polled['cursor_head']} "
              f"behind_by={polled['behind_by']} has_more={polled['has_more']}")
        assert polled["behind_by"] == 0, "cursor-default poll must report behind_by 0"
        assert any(ev["seq"] == sent["seq"] for ev in polled["events"]), \
            "recipient must see the unicast event"

        acked = rpc(proc, "room_ack", {
            "team_id": TEAM, "room_id": room_id, "agent_id": AGENT_B,
            "seq": polled["cursor_head"], "actor_token": tok_b,
        })
        print(f"[room_ack] {AGENT_B} acked through {acked['last_ack_seq']} "
              f"(receipts marked read: {acked.get('receipts_read')})")

        receipts = rpc(proc, "room_receipts", {
            "team_id": TEAM, "room_id": room_id, "agent_id": AGENT_A,
            "entry_ids": entry_ids, "actor_token": tok_a,
        })["receipts"]
        print(f"[room_receipts] {[(r['entry_id'], r['status'], r['read_status']) for r in receipts]}")
        assert all(r["read_status"] == "read" for r in receipts), \
            "acked recipient's durable receipts must be read"

        info = rpc(proc, "room_info", {
            "team_id": TEAM, "room_id": room_id, "agent_id": AGENT_A, "actor_token": tok_a,
        })
        print(f"[room_info] state={info['state']} member_count={info['member_count']} "
              f"roster={[(m['agent_id'], m['status']) for m in info['members']]}")

        removed = rpc(proc, "room_remove_member", {
            "team_id": TEAM, "room_id": room_id, "owner_agent_id": AGENT_A,
            "target_agent_id": AGENT_B, "actor_token": tok_a,
        })
        print(f"[room_remove_member] owner removed {AGENT_B}: status={removed['status']}")

        refused = None
        try:
            rpc(proc, "room_poll", {
                "team_id": TEAM, "room_id": room_id, "agent_id": AGENT_B,
                "after_seq": 0, "actor_token": tok_b,
            })
        except RuntimeError as exc:
            refused = str(exc)
        assert refused is not None, \
            "removed member must be refused on its next request"
        assert ("member_required" in refused) or ("room_not_found" in refused), \
            f"refusal must name a known code, got: {refused}"
        print(f"[removed-member request] refused as expected: {refused}")

        info_after = rpc(proc, "room_info", {
            "team_id": TEAM, "room_id": room_id, "agent_id": AGENT_A, "actor_token": tok_a,
        })
        print(f"[room_info] after removal: member_count={info_after['member_count']} "
              f"roster={[(m['agent_id'], m['status']) for m in info_after['members']]}")

        left = rpc(proc, "room_leave", {
            "team_id": TEAM, "room_id": room_id, "agent_id": AGENT_A, "actor_token": tok_a,
        })
        print(f"[room_leave] {AGENT_A}: status={left['status']}")

        print("CYCLE OK")
        return 0
    finally:
        proc.stdin.close()
        proc.terminate()
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait(timeout=10)
        listening_after, pid_lines = netstat_snapshot()
        new_listeners = listening_after - listening_before
        assert proc.poll() is not None, "server process did not exit"
        assert not new_listeners, f"listeners left behind: {new_listeners}"
        print(f"[cleanup] child exited (rc={proc.returncode}), "
              f"new LISTENING sockets={len(new_listeners)}")
        for line in sorted(pid_lines):
            if str(proc.pid) in line:
                print(f"[cleanup] WARNING: netstat still shows pid {proc.pid}: {line}")
        scratch_dir and _rmtree(scratch_dir)


def _rmtree(path):
    import shutil
    shutil.rmtree(str(path), ignore_errors=True)


if __name__ == "__main__":
    raise SystemExit(main())
