"""Finalisma bridge-adapter interop validation driver.

Spawns the REAL coordinator over stdio (MCP's primary transport), registers
an agent through its MCP surface, then exercises the REAL bridge adapters
(WebhookBridge, PollingBridge, ClipboardBridge) from src/weft_mcp/bridge.py
against that live coordinator's database. This is the "hosts with no MCP"
path: the bridge adapters are how non-MCP AI products reach the coordinator.

Happy paths + deliberate negative refusals for each adapter. Verbatim
transcript captured. Teardown in finally.

Run:  timeout 180 python -B scripts/interop-validate-bridge.py
"""

from __future__ import annotations

import json
import subprocess
import sys
import tempfile
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from weft_mcp.core import WeftStore, WeftError
from weft_mcp.bridge import (
    WebhookBridge,
    PollingBridge,
    ClipboardBridge,
    BridgeAuthError,
)

TEAM_ID = "demo"
REQUEST_TIMEOUT_S = 60


class InteropError(RuntimeError):
    pass


# ---------------------------------------------------------------------------
# Tiny webhook receiver (runs inside the driver on an ephemeral port)
# ---------------------------------------------------------------------------

class WebhookReceiver:
    """Minimal HTTP server that records the last request it received.

    Lives only for the duration of one webhook delivery test; torn down in
    the driver's finally block.
    """

    def __init__(self) -> None:
        self._handler = _make_handler_class()
        self._server = ThreadingHTTPServer(("127.0.0.1", 0), self._handler)
        self._port = self._server.server_address[1]
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)

    def start(self) -> None:
        self._thread.start()

    @property
    def port(self) -> int:
        return self._port

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self._port}/hook"

    def last_request(self) -> dict | None:
        return self._handler.last_request

    def wait_for_request(self, timeout: float = 5.0) -> dict | None:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if self._handler.last_request is not None:
                return self._handler.last_request
            time.sleep(0.05)
        return self._handler.last_request

    def stop(self) -> None:
        self._server.shutdown()
        self._server.server_close()


def _make_handler_class():
    class _Handler(BaseHTTPRequestHandler):
        last_request: dict | None = None

        def log_message(self, *args):  # silence stderr
            pass

        def do_POST(self):
            length = int(self.headers.get("Content-Length", "0"))
            raw = self.rfile.read(length) if length else b""
            WebhookReceiver  # reference to keep linter calm
            sig = self.headers.get("X-Finalisma-Signature", "")
            ts = self.headers.get("X-Finalisma-Timestamp", "")
            try:
                body = json.loads(raw.decode("utf-8")) if raw else None
            except json.JSONDecodeError:
                body = raw.decode("utf-8", errors="replace")
            _Handler.last_request = {
                "path": self.path,
                "signature": sig,
                "timestamp": ts,
                "body": body,
            }
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(b'{"ok":true}')

    return _Handler


# ---------------------------------------------------------------------------
# Driver
# ---------------------------------------------------------------------------

def main() -> int:
    scratch = tempfile.TemporaryDirectory(prefix="weft-interop-bridge-")
    workspace = Path(scratch.name)
    state_path = workspace / ".weft" / "state.db"
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
            str(state_path),
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
        line_out = json.dumps(payload, separators=(",", ":"))
        transcript.append(f"> {line_out}")
        assert proc.stdin is not None and proc.stdout is not None
        proc.stdin.write(line_out + "\n")
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

    # The bridge adapters need a store handle to the SAME database the
    # coordinator uses. WAL mode allows a second process to open it
    # concurrently; the coordinator owns writes, the driver's store is the
    # bridge adapters' view of state.
    bridge_store: WeftStore | None = None
    webhook_receiver: WebhookReceiver | None = None

    try:
        # ---- initialize handshake ---------------------------------------
        init = rpc(1, "initialize", {"protocolVersion": "2025-03-26", "capabilities": {}})
        protocol_version = init.get("protocolVersion")
        server_info = init.get("serverInfo")
        transcript.append(f"# initialize: protocol={protocol_version} server={server_info}")
        if not protocol_version or not server_info:
            raise InteropError("initialize returned no protocolVersion/serverInfo")

        tools = rpc(2, "tools/list").get("tools", [])
        tool_names = [t["name"] for t in tools]
        transcript.append(f"# tools/list: {len(tools)} tools")
        if "register_agent" not in tool_names:
            raise InteropError("tools/list missing register_agent")

        # ---- 1. register agent over MCP --------------------------------
        reg = call_tool(
            3,
            "register_agent",
            {"team_id": TEAM_ID, "agent_id": "bridge-agent", "role": "generalist", "name": "Bridge Agent"},
        )
        actor_token = reg["actor_token"]
        transcript.append(f"# register_agent: agent_id=bridge-agent token={actor_token[:8]}...")

        # ---- open bridge store against the coordinator's DB -----------
        bridge_store = WeftStore(
            str(state_path),
            str(workspace),
            require_actor_auth=True,
        )
        webhook = WebhookBridge(bridge_store)
        polling = PollingBridge(bridge_store)
        clipboard = ClipboardBridge(bridge_store)

        # ---- 2. ClipboardBridge: generate + parse (one-shot) -----------
        # Need a pairing_id + join_token. Create a pairing through MCP so the
        # bootstrap snippet references a real pairing.
        pairing = call_tool(
            4,
            "create_pairing",
            {
                "initiator_id": "bridge-agent",
                "team_id": TEAM_ID,
                "capabilities_offered": ["read"],
                "actor_token": actor_token,
            },
        )
        join_token = pairing["join_token"]
        pairing_id = pairing["pairing_id"]

        bootstrap = clipboard.generate_bootstrap(
            team_id=TEAM_ID,
            agent_id="bridge-agent",
            endpoint="http://127.0.0.1:8787",
            pairing_id=pairing_id,
            join_token=join_token,
            actor_token=actor_token,
        )
        transcript.append(f"# clipboard.generate_bootstrap: pairing_id={pairing_id} nonce={bootstrap['_nonce'][:8]}...")
        bootstrap_raw = json.dumps(bootstrap)

        # First parse succeeds (nonce consumed)
        parsed_ok = False
        try:
            parsed = clipboard.parse_bootstrap(bootstrap_raw)
            if parsed.get("team_id") == TEAM_ID and "_nonce" in parsed:
                parsed_ok = True
                transcript.append("# clipboard.parse_bootstrap (1st): accepted, nonce consumed")
            else:
                transcript.append("# clipboard.parse_bootstrap (1st): unexpected payload")
        except WeftError as exc:
            transcript.append(f"# clipboard.parse_bootstrap (1st) FAILED unexpectedly: {exc}")

        # Second parse refused (one-shot enforcement)
        clipboard_one_shot_refused = False
        clipboard_refusal_text = ""
        try:
            clipboard.parse_bootstrap(bootstrap_raw)
            transcript.append("# clipboard.parse_bootstrap (2nd): NOT refused (unexpected)")
        except WeftError as exc:
            clipboard_one_shot_refused = True
            clipboard_refusal_text = str(exc)
            transcript.append(f"# clipboard.parse_bootstrap (2nd) refused: {exc}")

        # ---- 3. PollingBridge: enqueue / get_pending / ack / no-repeat --
        enqueue_result = polling.enqueue(
            team_id=TEAM_ID,
            agent_id="bridge-agent",
            event={"kind": "test.message", "payload": {"text": "hello from polling bridge"}},
            actor_token=actor_token,
        )
        event_id = enqueue_result["event_id"]
        event_seq = enqueue_result["seq"]
        transcript.append(f"# polling.enqueue: event_id={event_id} seq={event_seq}")

        # get_pending returns it with a cursor
        pending = polling.get_pending(
            team_id=TEAM_ID,
            agent_id="bridge-agent",
            cursor=0,
            actor_token=actor_token,
        )
        got_events = pending.get("events", [])
        next_cursor = pending.get("next_cursor", 0)
        polling_got_event = len(got_events) == 1 and got_events[0].get("event_id") == event_id
        transcript.append(
            f"# polling.get_pending (cursor=0): returned {len(got_events)} event(s), next_cursor={next_cursor}"
        )

        # ack it
        ack_result = polling.ack(
            team_id=TEAM_ID,
            agent_id="bridge-agent",
            event_ids=[event_id],
            actor_token=actor_token,
        )
        transcript.append(f"# polling.ack: acked_count={ack_result.get('acked_count')} cursor={ack_result.get('cursor')}")

        # get_pending again returns nothing (at-most-once)
        pending2 = polling.get_pending(
            team_id=TEAM_ID,
            agent_id="bridge-agent",
            cursor=next_cursor,
            actor_token=actor_token,
        )
        polling_atmostonce = len(pending2.get("events", [])) == 0
        transcript.append(
            f"# polling.get_pending (cursor={next_cursor}): returned {len(pending2.get('events', []))} event(s) "
            f"{'(at-most-once OK)' if polling_atmostonce else '(REPEATED — bad)'}"
        )

        # NEGATIVE: wrong token poll is refused (actor binding)
        polling_wrong_token_refused = False
        polling_wrong_token_text = ""
        try:
            polling.get_pending(
                team_id=TEAM_ID,
                agent_id="bridge-agent",
                cursor=0,
                actor_token="wrong-token-not-valid",
            )
            transcript.append("# polling.get_pending (wrong token): NOT refused (unexpected)")
        except (WeftError, BridgeAuthError) as exc:
            polling_wrong_token_refused = True
            polling_wrong_token_text = str(exc)
            transcript.append(f"# polling.get_pending (wrong token) refused: {exc}")

        # NEGATIVE: non-member poll is refused
        polling_nonmember_refused = False
        polling_nonmember_text = ""
        try:
            polling.get_pending(
                team_id=TEAM_ID,
                agent_id="agent-does-not-exist",
                cursor=0,
                actor_token=actor_token,
            )
            transcript.append("# polling.get_pending (non-member): NOT refused (unexpected)")
        except (WeftError, BridgeAuthError) as exc:
            polling_nonmember_refused = True
            polling_nonmember_text = str(exc)
            transcript.append(f"# polling.get_pending (non-member) refused: {exc}")

        # ---- 4. WebhookBridge: register / deliver / verify_signature ---
        webhook_receiver = WebhookReceiver()
        webhook_receiver.start()
        webhook_signing_secret = "whsec_live_signing_secret_value_12345"

        wh_result = webhook.register_webhook(
            team_id=TEAM_ID,
            agent_id="bridge-agent",
            url=webhook_receiver.url,
            secret_ref=webhook_signing_secret,
            actor_token=actor_token,
        )
        webhook_id = wh_result["webhook_id"]
        transcript.append(f"# webhook.register_webhook: webhook_id={webhook_id} url={webhook_receiver.url}")

        deliver_result = webhook.deliver(
            webhook_id=webhook_id,
            event={"kind": "test.alert", "payload": {"msg": "signed webhook delivery"}},
            signing_secret=webhook_signing_secret,
        )
        transcript.append(f"# webhook.deliver: delivered={deliver_result.get('delivered')} status={deliver_result.get('status')}")

        # The receiver got an HMAC-signed POST
        received = webhook_receiver.wait_for_request(timeout=5.0)
        webhook_received = received is not None
        webhook_signature = received.get("signature", "") if received else ""
        webhook_timestamp = received.get("timestamp", "") if received else ""
        webhook_body = received.get("body") if received else None
        transcript.append(
            f"# webhook receiver: got_request={webhook_received} signature={webhook_signature[:28]}... timestamp={webhook_timestamp}"
        )

        # verify_signature passes with the real secret
        webhook_verify_real = False
        if received:
            webhook_verify_real = WebhookBridge.verify_signature(
                secret=webhook_signing_secret,
                signature=webhook_signature,
                timestamp=webhook_timestamp,
                body=webhook_body,
            )
            transcript.append(f"# webhook.verify_signature (real secret): {webhook_verify_real}")

        # verify_signature FAILS with a wrong secret
        webhook_verify_wrong = True  # assume fails; set False if it wrongly passes
        if received:
            webhook_verify_wrong = WebhookBridge.verify_signature(
                secret="wrong_secret_value",
                signature=webhook_signature,
                timestamp=webhook_timestamp,
                body=webhook_body,
            )
            transcript.append(
                f"# webhook.verify_signature (wrong secret): {webhook_verify_wrong} "
                f"{'(correctly rejected)' if not webhook_verify_wrong else '(WRONGLY accepted — bad)'}"
            )
        webhook_wrong_secret_refused = not webhook_verify_wrong

        # NEGATIVE: deliver WITHOUT signing_secret is refused (HIGH-1 fail-closed)
        webhook_no_secret_refused = False
        webhook_no_secret_text = ""
        try:
            webhook.deliver(
                webhook_id=webhook_id,
                event={"kind": "test"},
                signing_secret=None,
            )
            transcript.append("# webhook.deliver (no signing_secret): NOT refused (unexpected — HIGH-1 regression)")
        except WeftError as exc:
            webhook_no_secret_refused = True
            webhook_no_secret_text = str(exc)
            transcript.append(f"# webhook.deliver (no signing_secret) refused: {exc}")

        # ---- timing + result -------------------------------------------
        elapsed = time.monotonic() - started
        all_ok = all([
            parsed_ok,
            clipboard_one_shot_refused,
            polling_got_event,
            polling_atmostonce,
            polling_wrong_token_refused,
            polling_nonmember_refused,
            webhook_received,
            webhook_verify_real,
            webhook_wrong_secret_refused,
            webhook_no_secret_refused,
        ])

        result = {
            "status": "ok" if all_ok else "partial",
            "transport": "stdio",
            "protocol_version": protocol_version,
            "server_info": server_info,
            "tool_count": len(tool_names),
            "clipboard_one_shot": {
                "first_parse_ok": parsed_ok,
                "second_parse_refused": clipboard_one_shot_refused,
                "refusal": clipboard_refusal_text,
            },
            "polling_atmostonce": {
                "got_event": polling_got_event,
                "no_repeat": polling_atmostonce,
                "wrong_token_refused": polling_wrong_token_refused,
                "wrong_token_refusal": polling_wrong_token_text,
                "nonmember_refused": polling_nonmember_refused,
                "nonmember_refusal": polling_nonmember_text,
            },
            "webhook_signed": {
                "delivered": webhook_received,
                "signature_verified_real_secret": webhook_verify_real,
                "wrong_secret_refused": webhook_wrong_secret_refused,
                "no_signing_secret_refused": webhook_no_secret_refused,
                "no_signing_secret_refusal": webhook_no_secret_text,
            },
            "time_s": round(elapsed, 3),
            "transcript": transcript,
        }
        print(json.dumps(result, indent=2))
        return 0 if all_ok else 2
    except InteropError as exc:
        print(json.dumps({"status": "failed", "error": str(exc), "transcript": transcript}, indent=2))
        return 1
    finally:
        if webhook_receiver is not None:
            try:
                webhook_receiver.stop()
            except Exception:
                pass
        if bridge_store is not None:
            try:
                bridge_store.close()
            except Exception:
                pass
        proc.terminate()
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait(timeout=10)
        scratch.cleanup()


if __name__ == "__main__":
    raise SystemExit(main())
