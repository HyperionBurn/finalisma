"""Live-proof driver: agent keys carry distinct room identities.

Boots the REAL WeftCloudService over REAL HTTP on an ephemeral port, then
proves by request that two keys from ONE account:
  - join the same room as TWO distinct members,
  - carry DISTINCT origin_agent values,
  - exchange a unicast that a third sibling key (same account) cannot read.

Run: python -B scripts/verify-key-identity.py
"""

import json
import sys
import tempfile
import threading
import urllib.error
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from weft_cloud.service import WeftCloudService, _CloudHTTPHandler
from weft_cloud.storage import SqliteWalBackend


def _req(base, method, path, body=None, token=None):
    data = json.dumps(body).encode("utf-8") if body is not None else b""
    req = urllib.request.Request(base + path, data=data, method=method)
    req.add_header("Content-Type", "application/json")
    if token:
        req.add_header("Authorization", f"Bearer {token}")
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
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
        return exc.code, payload


def main() -> int:
    tmpdir = tempfile.mkdtemp(prefix="weft-keyidentity-live-")
    db_path = str(Path(tmpdir) / "live.db")
    from http.server import ThreadingHTTPServer

    httpd = ThreadingHTTPServer(("127.0.0.1", 0), _CloudHTTPHandler)
    base = f"http://127.0.0.1:{httpd.server_address[1]}"
    service = WeftCloudService(SqliteWalBackend(db_path))
    _CloudHTTPHandler.service = service
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()

    failures = []

    def check(cond, msg):
        print(("  PASS  " if cond else "  FAIL  ") + msg)
        if not cond:
            failures.append(msg)

    try:
        s, acct = _req(base, "POST", "/v1/auth/signup",
                       {"email": "live-proof@example.com", "password": "password-123"})
        assert s == 201, acct
        session = acct["session_token"]
        print(f"account   = {acct['account_id']}")

        keys = []
        for label in ("alpha", "bravo", "charlie"):
            s, key = _req(base, "POST", "/v1/agent-keys", {"label": label}, token=session)
            assert s == 201, key
            keys.append(key)
            print(f"key       = {label:8s} {key['key_id']}")
        key_a, key_b, key_c = keys

        s, room = _req(base, "POST", "/v1/rooms/create",
                       {"name": "live-proof", "cap": 5}, token=key_a["agent_key"])
        assert s == 201, room
        print(f"room      = {room['room_id']}  (owner: {room['owner_agent_id']})")
        check(room["owner_agent_id"] == key_a["key_id"],
              "room owner is the creating KEY's identity, not the account")

        for key in (key_b, key_c):
            s, joined = _req(base, "POST", "/v1/rooms/join",
                             {"room_id": room["room_id"], "link_token": room["link_token"],
                              "consent": True}, token=key["agent_key"])
            assert s == 200, joined
            print(f"join      = {key['label']:8s} -> agent_id {joined['agent_id']}")
            check(joined["agent_id"] == key["key_id"],
                  f"{key['label']} joined under its own key identity")

        s, info = _req(base, "GET", f"/v1/rooms/info?room_id={room['room_id']}",
                       token=key_a["agent_key"])
        print(f"room_info = member_count={info['member_count']} "
              f"members={[m['agent_id'] for m in info['members']]}")
        check(info["member_count"] == 3,
              "two keys + owner = 3 distinct members (not 1 collapsed account)")
        member_ids = {m["agent_id"] for m in info["members"]}
        check(member_ids == {key_a["key_id"], key_b["key_id"], key_c["key_id"]},
              "every member identity is a distinct key id, never the account id")

        s, sent = _req(base, "POST", "/v1/rooms/send",
                       {"room_id": room["room_id"], "target_spec": key_b["key_id"],
                        "payload": {"text": "pssst-bravo-only"}}, token=key_a["agent_key"])
        assert s == 200, sent
        check([r["agent_id"] for r in sent["receipts"]] == [key_b["key_id"]],
              "unicast alpha -> bravo is addressed to bravo's key identity only")

        s, poll_b = _req(base, "POST", "/v1/rooms/poll",
                         {"room_id": room["room_id"], "after_seq": 0}, token=key_b["agent_key"])
        msg_b = [e for e in poll_b["events"] if e["kind"] == "room.message"][-1]
        check(msg_b["payload"]["payload"]["text"] == "pssst-bravo-only",
              "bravo (the addressee) reads the unicast body")

        s, poll_c = _req(base, "POST", "/v1/rooms/poll",
                         {"room_id": room["room_id"], "after_seq": 0}, token=key_c["agent_key"])
        msg_c = [e for e in poll_c["events"] if e["kind"] == "room.message"][-1]
        check(msg_c["payload"] == {"redacted": True, "reason": "not_the_addressee"},
              "charlie (sibling key, SAME account) sees only the redacted envelope")

        s, poll_a = _req(base, "POST", "/v1/rooms/poll",
                         {"room_id": room["room_id"], "after_seq": 0}, token=key_a["agent_key"])
        origins = {e["origin_agent"] for e in poll_a["events"] if e["kind"] == "room.message"}
        print(f"origins   = {sorted(origins)}")
        check(len(origins) == 1 and key_a["key_id"] in origins,
              "the unicast's origin_agent is the sender KEY's identity")

        s, me_a = _req(base, "GET", "/v1/me", token=key_a["agent_key"])
        s, me_b = _req(base, "GET", "/v1/me", token=key_b["agent_key"])
        check(me_a["agent_id"] == key_a["key_id"] and me_b["agent_id"] == key_b["key_id"]
              and me_a["agent_id"] != me_b["agent_id"],
              "each key resolves a distinct /v1/me agent_id")

        print("\nRESULT:", "ALL CHECKS PASSED" if not failures else f"{len(failures)} FAILURES")
        return 0 if not failures else 1
    finally:
        httpd.shutdown()
        httpd.server_close()
        try:
            service.backend.close()
        except Exception:
            pass
        import shutil
        shutil.rmtree(tmpdir, ignore_errors=True)


if __name__ == "__main__":
    sys.exit(main())
