"""Finalisma SDK-tier validation driver.

Spawns the coordinator over Streamable HTTP (the SDK tier's real transport),
drives it through the SDK (`import finalisma_sdk`), exercises the full two-party
pairing + verified-handoff lifecycle AND the N-agent Room product claim, then
tears the child down in a finally block.

The SDK is the tier under test: three Python "hosts" each use FinalismaClient
as their client over http://127.0.0.1:<port>/mcp. The driver uses ONLY public
methods — the SDK's private escape-hatch dispatch is never used. Zero private
dispatch calls is the acceptance criterion for the SDK-ROOMS wave.

Run:  timeout 240 python -B scripts/interop-validate-sdk.py
"""

from __future__ import annotations

import json
import secrets
import socket
import subprocess
import sys
import tempfile
import time
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from finalisma_sdk import FinalismaClient, FinalismaError  # noqa: E402

TEAM_ID = "demo"
N_ROOM_AGENTS = 3


def _pick_free_port() -> int:
    """Bind a TCP socket to port 0, read the allocated port, close it."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _wait_tcp(host: str, port: int, timeout_s: float = 10.0) -> None:
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        try:
            with socket.create_connection((host, port), timeout=0.5):
                return
        except OSError:
            time.sleep(0.1)
    raise RuntimeError(f"coordinator did not accept TCP on {host}:{port} within {timeout_s}s")


def _redact(token: str) -> str:
    if not token or len(token) < 12:
        return "***"
    return token[:6] + "..." + token[-4:]


def main() -> int:
    scratch = tempfile.TemporaryDirectory(prefix="finalisma-sdk-interop-")
    workspace = Path(scratch.name)
    transcript: list[str] = []
    failures: list[str] = []
    started = time.monotonic()

    port = _pick_free_port()
    base_url = f"http://127.0.0.1:{port}/mcp"
    transcript.append(f"# free port: {port}")

    proc = subprocess.Popen(
        [
            sys.executable,
            "-B",
            "scripts/finalisma-mcp.py",
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
            str(workspace / ".finalisma" / "state.db"),
        ],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        cwd=str(PROJECT_ROOT),
        encoding="utf-8",
        errors="replace",
    )

    def log(msg: str) -> None:
        transcript.append(msg)

    def check(cond: bool, label: str) -> None:
        if cond:
            log(f"  PASS  {label}")
        else:
            log(f"  FAIL  {label}")
            failures.append(label)

    try:
        _wait_tcp("127.0.0.1", port)
        log("# coordinator TCP-accepting")

        # ---- Build N+1 SDK clients (3 room agents + 1 negative outsider) ----
        clients: dict[str, FinalismaClient] = {}
        tokens: dict[str, str] = {}

        for name in ["agent-a", "agent-b", "agent-c", "agent-outsider"]:
            # First registration is unauthenticated and returns a one-time actor_token.
            # The SDK's register() does not store it, so we must build the client with
            # the returned token for all subsequent authenticated calls.
            bootstrap = FinalismaClient(base_url, name, TEAM_ID)
            reg = bootstrap.register(name=name, role="generalist")
            tokens[name] = reg["actor_token"]
            bootstrap.close()
            clients[name] = FinalismaClient(base_url, name, TEAM_ID, actor_token=tokens[name])
            log(f"# register {name}: token={_redact(tokens[name])}")

        proto = clients["agent-a"].connect()
        log(f"# protocol: {proto.get('protocolVersion')} / {proto.get('serverInfo', {}).get('name')}")

        # ==================================================================
        # PART A — two-party pairing + verified handoff (SDK dedicated methods)
        # ==================================================================
        log("=== PART A: two-party pairing + verified handoff ===")

        pairing = clients["agent-a"].create_pairing_link(capabilities=["read", "comment"])
        link = pairing.join_url
        join_token = pairing.join_token
        log(f"# pairing link created: pairing_id={pairing.pairing_id} token={_redact(join_token)}")

        # Public helper join_pairing() now injects the client's actor_token, so
        # it succeeds on a coordinator that requires actor credentials.
        jr_b = clients["agent-b"].join_pairing(link, consent=True)
        joined_state = jr_b.state
        log(f"# agent-b joined pairing: state={joined_state}")
        check(joined_state in ("active", "open"), "pairing join state active/open")

        # A second pairing for the clean claim/verify handoff.
        pairing2 = clients["agent-a"].create_pairing_link(capabilities=["read", "comment"])
        link2 = pairing2.join_url
        jr_b2 = clients["agent-b"].join_pairing(link2, consent=True)
        check(jr_b2.state in ("active", "open"), "pairing #2 join state active/open")

        task_id = clients["agent-a"].create_task(
            scope=["handoff.txt"],
            description="Verify the retry boundary in handoff.txt",
            title="Interop SDK review",
            idempotency_key="sdk-handoff-v1",
        )
        log(f"# task created: {task_id}")
        check(bool(task_id), "task created")

        claimed = clients["agent-b"].claim(task_id)
        fencing = claimed.fencing_token
        log(f"# task claimed by agent-b: fencing={fencing}")
        check(fencing is not None, "claim returns fencing token")

        (workspace / "handoff.txt").write_text("synthetic SDK interop evidence\n", encoding="utf-8")
        verified = clients["agent-b"].submit_evidence(
            task_id,
            artifact_paths=["handoff.txt"],
            checks=[{"name": "sdk-interop-check", "status": "passed", "evidence": "artifact present"}],
            fencing_token=fencing,
        )
        log(f"# evidence submitted: passed={verified.get('passed')}")
        check(bool(verified.get("passed")), "evidence gate passed")

        completed = clients["agent-b"].complete(task_id, fencing_token=fencing, summary="SDK interop verified")
        log(f"# task completed: status={completed.status}")
        check(completed.status == "done", "task completed (done)")

        # ==================================================================
        # PART B — MULTI-AGENT ROOM (the product claim)
        # ==================================================================
        room_started = time.monotonic()
        log("=== PART B: N-agent room (product claim) ===")

        # Owner (agent-a) creates a room with cap >= 4 via the public API.
        room = clients["agent-a"].create_room(cap=4, name="interop-sdk-room")
        room_id = room.room_id
        link_token = room.link_token
        cap = room.cap
        log(f"# room created: room_id={room_id} cap={cap} state={room.state}")
        check(room.state == "forming", "room created in forming state")
        check(cap >= 4, "room cap >= 4")

        # Owner is auto-joined as first member; agent-b and agent-c join via link.
        for name in ["agent-b", "agent-c"]:
            jr = clients[name].join_room(room_id, link_token, consent=True, capabilities=["read"])
            log(f"# {name} joined room: status={jr.status} cursor={jr.cursor}")
            check(jr.status == "active", f"{name} joined room active")

        # (1) All 3 are members.
        info = clients["agent-a"].room_info(room_id)
        member_ids = sorted(m.agent_id for m in info.members)
        log(f"# room info: state={info.state} members={member_ids} count={info.member_count}")
        check(info.member_count == 3, "room has 3 members")
        check(member_ids == ["agent-a", "agent-b", "agent-c"], "member roster is {a,b,c}")
        check(info.state == "active", "room state active after joins")

        # (2a) Unicast: A sends to B.
        send_b = clients["agent-a"].send(room_id, target="agent-b", payload={"text": "unicast to B"})
        receipts_unicast = send_b.receipts
        log(f"# unicast A->B: receipts={[r['agent_id'] for r in receipts_unicast]} seq={send_b.seq}")
        check(
            sorted(r["agent_id"] for r in receipts_unicast) == ["agent-b"],
            "unicast receipt targets exactly [agent-b]",
        )
        check(all(r["status"] == "queued" for r in receipts_unicast), "unicast receipts queued")

        # (2b) Group: add B and C, send to group.
        clients["agent-a"].add_to_group(room_id, "builders", ["agent-b", "agent-c"])
        grp_members = clients["agent-a"].group_members(room_id, "builders")
        log(f"# group 'builders' members={grp_members}")
        check(grp_members == ["agent-b", "agent-c"], "group builders = {b,c}")

        send_grp = clients["agent-a"].send(room_id, target="builders", payload={"text": "group message"})
        receipts_group = send_grp.receipts
        log(f"# group send: receipts={[r['agent_id'] for r in receipts_group]}")
        check(
            sorted(r["agent_id"] for r in receipts_group) == ["agent-b", "agent-c"],
            "group receipt targets [agent-b, agent-c]",
        )

        # (2c) Broadcast "*" — receipts for the other two (exclude_sender default).
        send_bc = clients["agent-a"].send(room_id, target="*", payload={"text": "broadcast"})
        receipts_bc = send_bc.receipts
        log(f"# broadcast *: receipts={[r['agent_id'] for r in receipts_bc]}")
        check(
            sorted(r["agent_id"] for r in receipts_bc) == ["agent-b", "agent-c"],
            "broadcast receipt targets [agent-b, agent-c]",
        )

        # (3) Ordered event log + per-member cursor.
        cursors: dict[str, int] = {}
        for name in ["agent-a", "agent-b", "agent-c"]:
            poll = clients[name].room_poll(room_id, after_seq=0)
            events = poll.events
            seqs = [e.seq for e in events]
            log(f"# {name} poll(0): {len(events)} events, seqs={seqs}, next_seq={poll.next_seq}")
            check(seqs == sorted(seqs), f"{name} events returned in ascending seq order")
            check(len(set(seqs)) == len(seqs), f"{name} no duplicate seqs in poll")
            head = poll.cursor_head
            acked = clients[name].room_ack(room_id, seq=head)
            cursors[name] = acked
            log(f"# {name} ack to {head}: last_ack_seq={acked}")
            check(acked == head, f"{name} cursor advanced to {head}")

        # Reconnect: a fresh client with agent-b's identity+token (simulate disconnect).
        log("# reconnect: fresh client reusing agent-b identity + actor_token")
        reconnected_b = FinalismaClient(base_url, "agent-b", TEAM_ID, actor_token=tokens["agent-b"])
        replay_full = reconnected_b.room_poll(room_id, after_seq=0)
        replay_seqs = [e.seq for e in replay_full.events]
        log(f"# reconnect poll(0): {len(replay_full.events)} events, seqs={replay_seqs}")
        check(
            replay_seqs == sorted(set(replay_seqs)) and len(replay_seqs) == len(set(replay_seqs)),
            "reconnect replay: ordered, no duplicates",
        )
        replay_cursor = reconnected_b.room_poll(room_id, after_seq=cursors["agent-b"])
        log(f"# reconnect poll(from cursor {cursors['agent-b']}): {len(replay_cursor.events)} new events")
        check(len(replay_cursor.events) == 0, "reconnect from cursor: no already-acked events replayed")
        reconnected_b.close()

        room_elapsed = time.monotonic() - room_started

        # ==================================================================
        # NEGATIVE cases
        # ==================================================================
        log("=== NEGATIVE cases ===")

        # (N1) Non-member (agent-outsider) calls room_poll → member_required.
        neg1_refused = False
        neg1_code = ""
        try:
            clients["agent-outsider"].room_poll(room_id, after_seq=0)
            log("  FAIL  outsider poll NOT refused")
        except FinalismaError as exc:
            neg1_refused = True
            neg1_code = exc.code
            log(f"  PASS  outsider poll refused: code={exc.code} msg={exc.message}")
        check(neg1_refused, "outsider room_poll refused")
        check(neg1_code == "member_required", f"outsider poll error code == member_required (got {neg1_code})")

        # (N1b) Non-member calls room_info → member_required.
        neg1b_refused = False
        neg1b_code = ""
        try:
            clients["agent-outsider"].room_info(room_id)
            log("  FAIL  outsider room_info NOT refused")
        except FinalismaError as exc:
            neg1b_refused = True
            neg1b_code = exc.code
            log(f"  PASS  outsider room_info refused: code={exc.code}")
        check(neg1b_refused, "outsider room_info refused")
        check(neg1b_code == "member_required", f"outsider room_info error code == member_required (got {neg1b_code})")

        # (N2) Reuse room link under existing member agent-b with DIFFERENT actor_token → actor_auth_invalid.
        bogus_token = "rm_" + secrets.token_urlsafe(32)
        neg2_refused = False
        neg2_code = ""
        try:
            impostor = FinalismaClient(base_url, "agent-b", TEAM_ID, actor_token=bogus_token)
            impostor.join_room(room_id, link_token, consent=True)
            log("  FAIL  actor-overwrite join NOT refused")
            impostor.close()
        except FinalismaError as exc:
            neg2_refused = True
            neg2_code = exc.code
            log(f"  PASS  actor-overwrite join refused: code={exc.code} msg={exc.message}")
        check(neg2_refused, "actor-overwrite room_join refused")
        check(neg2_code == "actor_auth_invalid", f"actor-overwrite error code == actor_auth_invalid (got {neg2_code})")

        elapsed = time.monotonic() - started
        status = "ok" if not failures else "failed"
        result = {
            "status": status,
            "transport": "http",
            "sdk_tier": "finalisma_sdk.FinalismaClient over Streamable HTTP (public API only)",
            "protocol_version": proto.get("protocolVersion"),
            "server_info": proto.get("serverInfo"),
            "pairing_join_state": jr_b2.state,
            "task_status": completed.status,
            "evidence_passed": bool(verified.get("passed")),
            "room": {
                "room_id": room_id,
                "cap": cap,
                "state": info.state,
                "member_count": info.member_count,
                "members": member_ids,
                "unicast_receipts": [r["agent_id"] for r in receipts_unicast],
                "group_receipts": [r["agent_id"] for r in receipts_group],
                "broadcast_receipts": [r["agent_id"] for r in receipts_bc],
                "reconnect_replay_events": len(replay_full.events),
                "reconnect_from_cursor_events": len(replay_cursor.events),
            },
            "negative": {
                "outsider_poll_refused": neg1_refused,
                "outsider_poll_code": neg1_code,
                "outsider_info_refused": neg1b_refused,
                "outsider_info_code": neg1b_code,
                "actor_overwrite_refused": neg2_refused,
                "actor_overwrite_code": neg2_code,
            },
            "time_total_s": round(elapsed, 3),
            "time_room_s": round(room_elapsed, 3),
            "failures": failures,
            "transcript": transcript,
        }
        print(json.dumps(result, indent=2))
        return 0 if not failures else 1

    except (FinalismaError, RuntimeError) as exc:
        elapsed = time.monotonic() - started
        print(json.dumps({
            "status": "failed",
            "error": str(exc),
            "time_total_s": round(elapsed, 3),
            "failures": failures,
            "transcript": transcript,
        }, indent=2))
        return 1
    finally:
        for c in clients.values():
            c.close()
        proc.terminate()
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait(timeout=10)
        scratch.cleanup()


if __name__ == "__main__":
    raise SystemExit(main())
