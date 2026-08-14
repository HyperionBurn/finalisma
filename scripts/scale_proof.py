#!/usr/bin/env python3
"""Scale proof harness — the 10-agent and 50-agent claims, measured, not asserted.

Process postmortem 2026-08-13, CORE: a ten-agent-one-minute anecdote was
promoted to a claim. This harness makes the claim re-runnable and grades it
against the acceptance bar agreed in the build room:

  1. delivery loss = 0 (receipts are ground truth; every routed recipient
     must also be able to READ every message addressed to them),
  2. every recipient's local ordering is strictly increasing and a
     subsequence of the global seq order,
  3. unicast leak = 0 (every non-addressee sees redacted/not_the_addressee
     and the payload is replaced wholesale),
  4. wake latency p50 measured from real timestamps, never asserted,
  5. soak: the broadcast ring runs for --minutes (default 10).

The harness must FAIL against a broken build: --selftest injects a deliberate
delivery drop and asserts the harness reports failure (a guard never seen red
guards nothing).

Usage:
  python -B scripts/scale_proof.py --agents 10 --minutes 1
  python -B scripts/scale_proof.py --agents 50 --minutes 10
  python -B scripts/scale_proof.py --selftest

Default target is a local cloud instance (WeftCloudService over real HTTP on a
loopback port — the same surface an MCP host uses). Remote mode:
  python -B scripts/scale_proof.py --target remote --base https://host --token fss_...
Remote mode respects the production signup rate limit by pacing signups;
50-account claims still require the plan cap to actually admit 50.

Stdlib only. Exit 0 when every check passes; exit 1 otherwise.
"""

from __future__ import annotations

import argparse
import json
import sys
import threading
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

REMOTE_SIGNUP_PACE_SECONDS = 4.0  # stay under 20/IP/900s with one caller


def _iso_to_epoch(iso: str) -> float:
    try:
        return datetime.fromisoformat(iso.replace("Z", "+00:00")).timestamp()
    except ValueError:
        return 0.0


class Client:
    def __init__(self, base: str, token: str, request_id: int = 1):
        self.base = base
        self.token = token
        self._rid = request_id
        self.rate_retries = 0

    def _request(self, path: str, body: dict, timeout: int = 30) -> tuple[int, dict]:
        data = json.dumps(body).encode("utf-8")
        req = urllib.request.Request(self.base + path, data=data, method="POST")
        req.add_header("Content-Type", "application/json")
        if self.token:
            req.add_header("Authorization", f"Bearer {self.token}")
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                raw = resp.read()
                return resp.status, json.loads(raw.decode("utf-8")) if raw else {}
        except urllib.error.HTTPError as exc:
            payload = {}
            try:
                raw = exc.read()
                payload = json.loads(raw.decode("utf-8")) if raw else {}
            except Exception:
                pass
            finally:
                exc.close()
            if exc.code == 429 and self.rate_retries < 3:
                # One IP hosting N agents collides with the per-IP request
                # limiter; retry honestly after retry_after, and count it.
                self.rate_retries += 1
                retry_after = float(
                    (payload.get("error") or {}).get("retry_after")
                    or payload.get("retry_after")
                    or 5.0
                )
                time.sleep(min(retry_after, 30.0))
                return self._request(path, body, timeout)
            return exc.code, payload

    def mcp(self, name: str, args: dict, timeout: int = 30) -> dict:
        self._rid += 1
        status, payload = self._request(
            "/mcp",
            {"jsonrpc": "2.0", "method": "tools/call",
             "params": {"name": name, "arguments": args}, "id": self._rid},
            timeout=timeout,
        )
        if status != 200:
            raise RuntimeError(f"{name} HTTP {status}: {payload}")
        result = (payload or {}).get("result") or {}
        if result.get("isError"):
            text = ""
            for c in result.get("content") or []:
                text += c.get("text", "")
            raise RuntimeError(f"{name} isError: {text[:200]}")
        return result.get("structuredContent") or {}


class LocalTarget:
    """In-process cloud service over a real loopback HTTP server."""

    def __init__(self):
        import tempfile
        import shutil
        from http.server import ThreadingHTTPServer

        from weft_cloud.service import WeftCloudService, _CloudHTTPHandler
        from weft_cloud.storage import SqliteWalBackend

        self.tmpdir = tempfile.mkdtemp(prefix="weft-scale-proof-")
        self.db_path = str(Path(self.tmpdir) / "state.db")
        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), _CloudHTTPHandler)
        self.port = self.httpd.server_address[1]
        self.base = f"http://127.0.0.1:{self.port}"
        self.service = WeftCloudService(SqliteWalBackend(self.db_path))
        _CloudHTTPHandler.service = self.service
        self.thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)
        self.thread.start()

    def close(self):
        import shutil
        try:
            self.httpd.shutdown()
            self.httpd.server_close()
        finally:
            try:
                self.service.backend.close()
            except Exception:
                pass
            shutil.rmtree(self.tmpdir, ignore_errors=True)

    def set_plan(self, tenant_id: str, plan_id: str) -> None:
        with self.service.backend.transaction() as tx:
            tx.execute(
                "UPDATE cloud_tenants SET plan_id = ? WHERE tenant_id = ?",
                (plan_id, tenant_id),
            )
            tx.commit()


def signup_local(base: str, email: str, tenant_id: str | None) -> dict:
    body = {"email": email, "password": "password-123"}
    if tenant_id:
        body["tenant_id"] = tenant_id
    data = json.dumps(body).encode("utf-8")
    req = urllib.request.Request(base + "/v1/auth/signup", data=data, method="POST")
    req.add_header("Content-Type", "application/json")
    with urllib.request.urlopen(req, timeout=30) as resp:
        return json.loads(resp.read().decode("utf-8"))


def mint_agent_key(base: str, session_token: str, label: str) -> str:
    body = json.dumps({"label": label}).encode("utf-8")
    req = urllib.request.Request(base + "/v1/agent-keys", data=body, method="POST")
    req.add_header("Content-Type", "application/json")
    req.add_header("Authorization", f"Bearer {session_token}")
    with urllib.request.urlopen(req, timeout=30) as resp:
        return json.loads(resp.read().decode("utf-8"))["agent_key"]


def run_scale(base: str, agents: int, minutes: float,
              pace_signups: bool, identity_mode: str = "accounts",
              local_plan: str | None = None,
              local_plan_setter=None) -> dict:
    t0 = time.monotonic()
    if identity_mode == "keys":
        # One account + N-1 agent keys: per-key identity makes each key a
        # distinct room member (the production connector topology), and it
        # skips the signup auth limiter entirely.
        owner = signup_local(base, f"scale-{int(time.time())}@example.com", None)
        tokens = [owner["session_token"]]
        for i in range(1, agents):
            tokens.append(mint_agent_key(base, owner["session_token"], f"scale-{i}"))
        tenant_id = owner["tenant_id"]
    else:
        # N accounts on ONE tenant. Measured boundary: the 20/IP/900s auth
        # limiter refuses rapid signups (HTTP 429 at ~21), so account mode
        # needs pacing or it fails by design.
        accounts: list[dict] = []
        for i in range(agents):
            tenant = accounts[0]["tenant_id"] if accounts else None
            acct = signup_local(base, f"scale-{int(time.time())}-{i}@example.com", tenant)
            accounts.append(acct)
            if pace_signups:
                time.sleep(REMOTE_SIGNUP_PACE_SECONDS)
        tokens = [a["session_token"] for a in accounts]
        tenant_id = accounts[0]["tenant_id"]

    if local_plan_setter and local_plan:
        local_plan_setter(tenant_id, local_plan)

    clients = [Client(base, t) for t in tokens]
    try:
        room = clients[0].mcp("room_create", {"cap": agents})
    except RuntimeError as exc:
        if "quota_exceeded" in str(exc):
            return {
                "agents": agents,
                "plan_limit_hit": True,
                "detail": str(exc)[:300],
                "pass": False,
            }
        raise

    room_id = room["room_id"]
    for c in clients[1:]:
        c.mcp("room_join", {"room_id": room_id, "link_token": room["link_token"],
                            "consent": True})
    # Roster order == client order (joined_at ordering).
    info = clients[0].mcp("room_info", {"room_id": room_id})
    roster = [m["agent_id"] for m in info["members"]]
    assert len(roster) == agents, f"roster has {len(roster)} members, expected {agents}"

    # --- global send pacer ---------------------------------------------------
    # The plan budget is 60 messages/min PER ROOM on free AND pro (quotas.py).
    # The harness measures correctness, not rate-limit behaviour, so every
    # send (ring + pings) is paced to ~50/min with one shared throttle; the
    # achieved throughput ceiling is reported. rate_limited is retried once
    # after its retry_after and then reported, never swallowed.
    send_lock = threading.Lock()
    last_send = [0.0]
    rate_hits = [0]
    MIN_SEND_GAP = 1.25

    def paced_send(client: Client, args: dict, timeout: int = 30) -> dict:
        with send_lock:
            wait = MIN_SEND_GAP - (time.monotonic() - last_send[0])
            if wait > 0:
                time.sleep(wait)
            last_send[0] = time.monotonic()
        try:
            return client.mcp("room_send", args, timeout=timeout)
        except RuntimeError as exc:
            if "rate_limited" in str(exc):
                rate_hits[0] += 1
                time.sleep(5.0)
                return client.mcp("room_send", args, timeout=timeout)
            raise

    # --- ground truth bookkeeping -------------------------------------------
    deliveries_expected = 0
    losses = 0
    unicast_leaks = 0
    order_violations = 0
    read_failures = 0
    wake_samples: list[float] = []
    # Verification traffic is O(N^2) if exhaustive; one IP cannot sustain that
    # under the request limiter (measured: 429). Receipts remain EXHAUSTIVE
    # ground truth for loss (they ride the send response); read-back and leak
    # checks sample a bounded subset of the other agents per message.
    VERIFY_SAMPLE = min(10, max(2, agents - 1))

    def one_cycle(cycle: int) -> None:
        nonlocal deliveries_expected, losses, unicast_leaks, order_violations, read_failures

        def safe_send(client: Client, args: dict) -> dict | None:
            try:
                return paced_send(client, args)
            except RuntimeError as exc:
                # A send that cannot be made is a delivery failure the harness
                # MUST report — the selftest break lands here and must go RED.
                print(f"    send failed: {str(exc)[:160]}", file=sys.stderr)
                return None

        # Broadcast ring: every agent sends one broadcast.
        for i, c in enumerate(clients):
            sent = safe_send(c, {"room_id": room_id, "target_spec": "*",
                                 "payload": {"cycle": cycle, "agent": i}})
            if sent is None:
                losses += agents - 1
                continue
            seq = sent["seq"]
            expected_recipients = agents - 1  # exclude_sender default True
            deliveries_expected += expected_recipients
            got = {r["agent_id"] for r in sent["receipts"]}
            if len(got) != expected_recipients:
                losses += expected_recipients - len(got)
            # Sampled read-back: a bounded subset of recipients must read it.
            for j in _sample_others(agents, i, VERIFY_SAMPLE):
                events = _poll_until_seq(clients[j], room_id, seq)
                payloads = [e["payload"].get("payload") for e in events
                            if e.get("kind") == "room.message"]
                if not any(p and p.get("cycle") == cycle and p.get("agent") == i
                           for p in payloads):
                    read_failures += 1

        # Unicast leak probes: each agent unicasts a secret to the next agent.
        for i, c in enumerate(clients):
            target = (i + 1) % agents
            secret = f"secret-{cycle}-{i}-{int(time.time() * 1000)}"
            sent = safe_send(c, {"room_id": room_id,
                                 "target_spec": roster[target],
                                 "payload": {"secret": secret}})
            deliveries_expected += 1
            if sent is None or len(sent["receipts"]) != 1:
                losses += 1
                continue
            seq = sent["seq"]
            # The addressee reads it.
            events = _poll_until_seq(clients[target], room_id, seq)
            texts = [json.dumps(e["payload"]) for e in events]
            if not any(secret in t for t in texts):
                read_failures += 1
            # A bounded sample of non-addressees sees a redacted envelope and
            # never the secret text. Only the unicast event at exactly ``seq``
            # is under test — broadcasts in the same window are legitimately
            # readable by everyone.
            for j in _sample_others(agents, i, VERIFY_SAMPLE):
                if j == target:
                    continue
                events = _poll_until_seq(clients[j], room_id, seq)
                for e in events:
                    if e["seq"] != seq:
                        continue
                    pl = e["payload"]
                    raw = json.dumps(pl)
                    if secret in raw:
                        unicast_leaks += 1
                    if isinstance(pl, dict):
                        if pl.get("redacted") is not True:
                            unicast_leaks += 1
                        elif pl.get("reason") != "not_the_addressee":
                            unicast_leaks += 1

        # Ordering: every agent's seen seq list must be increasing and a
        # subsequence of the global order (global seqs are strictly increasing
        # by construction, so local increase alone proves subsequence).
        for c in clients:
            seen = [e["seq"] for e in c.mcp("room_poll",
                                            {"room_id": room_id, "after_seq": 0})["events"]]
            if seen != sorted(seen) or len(set(seen)) != len(seen):
                order_violations += 1

    # Wake latency: a sender thread emits pings at the paced rate; a waiter
    # thread holds room_wait in a tight loop and measures event-commit ->
    # wait-return for every delivered event. That is the number a blocked
    # agent actually feels, measured, never asserted.
    stop_event = threading.Event()
    sender = clients[0]
    waiter = clients[-1]

    def _pinger():
        i = 0
        while not stop_event.is_set():
            paced_send(sender, {"room_id": room_id, "target_spec": "*",
                                "payload": {"ping": i}})
            i += 1
            time.sleep(1.5)

    waiter_seq = [0]

    def _waiter():
        while not stop_event.is_set():
            try:
                polled = waiter.mcp(
                    "room_wait",
                    {"room_id": room_id, "after_seq": waiter_seq[0], "timeout_seconds": 5},
                    timeout=20,
                )
            except RuntimeError:
                time.sleep(1.0)
                continue
            observed = time.time()
            for e in polled.get("events", []):
                committed = _iso_to_epoch(e["created_at"])
                if committed and e.get("kind") == "room.message":
                    wake_samples.append(max(0.0, observed - committed))
                waiter_seq[0] = max(waiter_seq[0], e["seq"])

    ping_thread = threading.Thread(target=_pinger, daemon=True)
    wait_thread = threading.Thread(target=_waiter, daemon=True)
    ping_thread.start()
    wait_thread.start()
    deadline = time.monotonic() + minutes * 60.0
    cycle = 0
    try:
        while time.monotonic() < deadline:
            one_cycle(cycle)
            cycle += 1
    finally:
        stop_event.set()
        ping_thread.join(timeout=10)
        wait_thread.join(timeout=10)

    wake_p50_ms = None
    if wake_samples:
        ordered = sorted(wake_samples)
        wake_p50_ms = round(ordered[len(ordered) // 2] * 1000, 1)

    report = {
        "agents": agents,
        "minutes": minutes,
        "identity_mode": identity_mode,
        "cycles": cycle,
        "deliveries_expected": deliveries_expected,
        "losses": losses,
        "read_failures": read_failures,
        "unicast_leaks": unicast_leaks,
        "order_violations": order_violations,
        "wake_samples": len(wake_samples),
        "wake_p50_ms": wake_p50_ms,
        "rate_limit_retries": rate_hits[0],
        "ip_rate_retries": sum(c.rate_retries for c in clients),
        "send_pace_s": MIN_SEND_GAP,
        "elapsed_s": round(time.monotonic() - t0, 1),
        "pass": (losses == 0 and read_failures == 0 and unicast_leaks == 0
                 and order_violations == 0 and (wake_p50_ms or 0) > 0),
    }
    return report


def _sample_others(agents: int, sender_index: int, limit: int) -> list[int]:
    """A deterministic spread of other-agent indices, bounded by ``limit``."""
    step = max(1, (agents - 1) // limit)
    out = []
    i = (sender_index + 1) % agents
    while len(out) < limit:
        out.append(i)
        i = (i + step) % agents
        if i == sender_index:
            i = (i + 1) % agents
    return [j for j in out if j != sender_index]


def _poll_until_seq(client: Client, room_id: str, seq: int, tries: int = 40) -> list[dict]:
    after = 0
    events: list[dict] = []
    for _ in range(tries):
        try:
            polled = client.mcp("room_poll", {"room_id": room_id, "after_seq": after}, timeout=15)
        except RuntimeError as exc:
            if "429" in str(exc):
                time.sleep(5.0)
                continue
            raise
        events.extend(polled["events"])
        if any(e["seq"] >= seq for e in polled["events"]):
            break
        if polled["events"]:
            after = polled["events"][-1]["seq"]
        time.sleep(0.1)
    return [e for e in events if e["seq"] >= seq] if events else events


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--agents", type=int, default=10)
    parser.add_argument("--minutes", type=float, default=1.0)
    parser.add_argument("--target", choices=["local", "remote"], default="local")
    parser.add_argument("--base", default=None, help="remote base URL")
    parser.add_argument("--token", default=None, help="remote fss_ session token")
    parser.add_argument("--plan", choices=["free", "pro"], default="free",
                        help="tenant plan for the run (local mode sets it directly; "
                             "the free plan caps rooms at 10 members, pro at 50)")
    parser.add_argument("--identity", choices=["accounts", "keys"], default="keys",
                        help="accounts: one signup per agent (hits the 20/IP/900s auth "
                             "limiter at scale); keys: 1 account + N-1 agent keys "
                             "(production connector topology, recommended)")
    parser.add_argument("--selftest", action="store_true",
                        help="inject a deliberate drop; the harness MUST fail")
    args = parser.parse_args()

    if args.target == "remote":
        if not args.base or not args.token:
            print("SCALE PROOF: remote target requires --base and --token")
            sys.exit(2)
        base = args.base.rstrip("/")
        report = run_scale(base, args.agents, args.minutes, pace_signups=True,
                           identity_mode=args.identity)
    else:
        target = LocalTarget()
        try:
            if args.selftest:
                # Deliberate break, real mechanism: re-inject the staleloss
                # shape by dropping the last routed target from every send.
                # A harness that cannot see this guards nothing.
                from weft_cloud import rooms as rooms_module
                _orig_route = rooms_module.CloudRoomService._route_targets

                def _broken_route(self, tx, tenant_id, room_id, target_spec):
                    routed = _orig_route(self, tx, tenant_id, room_id, target_spec)
                    return routed[:-1] if routed else routed

                rooms_module.CloudRoomService._route_targets = _broken_route
            report = run_scale(target.base, args.agents, args.minutes,
                               pace_signups=False, identity_mode=args.identity,
                               local_plan=args.plan, local_plan_setter=target.set_plan)
        finally:
            target.close()

    print(json.dumps(report, indent=2))
    if args.selftest:
        if report["pass"]:
            print("SELFTEST FAILED: injected drop was NOT detected")
            sys.exit(1)
        print("SELFTEST PASS: injected drop was detected (harness went red)")
        sys.exit(0)
    if report.get("plan_limit_hit"):
        print("SCALE PROOF: PLAN LIMIT HIT — the run could not even be constructed: "
              "the claim is unprovable on this plan")
        sys.exit(1)
    if report["pass"]:
        print(f"SCALE PROOF PASS: {args.agents} agents, {report['cycles']} cycles, "
              f"0 losses, 0 leaks, 0 order violations, "
              f"wake p50 {report['wake_p50_ms']}ms")
        sys.exit(0)
    print(f"SCALE PROOF FAIL: {report['losses']} losses, "
          f"{report['read_failures']} read failures, {report['unicast_leaks']} leaks, "
          f"{report['order_violations']} order violations")
    sys.exit(1)


if __name__ == "__main__":
    main()
