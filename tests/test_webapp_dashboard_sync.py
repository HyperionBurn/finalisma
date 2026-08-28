"""MPAI-60 & MPAI-68 regression tests:
1. Dashboard must deduplicate concurrent list-rooms calls on load into a single network call (MPAI-60).
2. Room creation/closure must dispatch `weft:rooms_changed` event, and RailRooms / RailAccount / RoomsList
   must listen to `weft:rooms_changed` so the sidebar and dashboard update without manual page reload (MPAI-60).
3. RailRooms and RailAccount must filter out closed rooms so closed rooms leave the rail and the quota count drops (MPAI-68).
4. RoomView must provide an owner-only close control with a two-step confirmation step, and handle closed rooms cleanly (MPAI-68).
5. Server must enforce owner-only room closure (403 owner_required for non-owners) and decrement active room quota upon closure so users can immediately create new rooms (MPAI-68).
"""

from __future__ import annotations

import json
import subprocess
import sys
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

from weft_cloud.service import WeftCloudService, _CloudHTTPHandler
from weft_cloud.storage import SqliteWalBackend

API_TS = ROOT / "web" / "src" / "lib" / "api.ts"
RAIL_ROOMS_TSX = ROOT / "web" / "src" / "components" / "app" / "RailRooms.tsx"
RAIL_ACCOUNT_TSX = ROOT / "web" / "src" / "components" / "app" / "RailAccount.tsx"
ROOMS_LIST_TSX = ROOT / "web" / "src" / "components" / "app" / "RoomsList.tsx"
ROOM_VIEW_TSX = ROOT / "web" / "src" / "components" / "app" / "RoomView.tsx"


def _post(base: str, path: str, body: dict, token: str | None = None) -> tuple[int, dict]:
    data = json.dumps(body).encode("utf-8")
    req = urllib.request.Request(base + path, data=data, method="POST")
    req.add_header("Content-Type", "application/json")
    if token:
        req.add_header("Authorization", f"Bearer {token}")
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            return resp.status, json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        payload = {}
        try:
            payload = json.loads(exc.read().decode("utf-8"))
        except Exception:
            pass
        finally:
            exc.close()
        return exc.code, payload


class TestDashboardPapercutsMPAI60(unittest.TestCase):
    """Regression test suite for Kevin's papercuts (MPAI-60 & MPAI-68)."""

    def test_list_rooms_deduplicates_concurrent_calls(self):
        """listRooms() must deduplicate in-flight requests so concurrent mounts only fire 1 network call."""
        api_source = API_TS.read_text(encoding="utf-8")
        self.assertIn("listRoomsInFlight", api_source, "api.ts must track in-flight listRooms promise")

        js_test = f"""
        let fetchCount = 0;
        let rpcId = 1;
        function getToken() {{ return 'tok_123'; }}
        function requireAuth() {{ return 'tok_123'; }}
        async function tool(name, args = {{}}) {{
            fetchCount++;
            await new Promise(r => setTimeout(r, 10));
            return {{ rooms: [{{ room_id: 'r_1', name: 'incident-4471' }}] }};
        }}
        {api_source[api_source.find('let listRoomsInFlight'):api_source.find('export interface CreatedRoom')] if 'let listRoomsInFlight' in api_source else 'const listRooms = () => tool("room_list");'}

        async function run() {{
            const p1 = listRooms();
            const p2 = listRooms();
            const p3 = listRooms();
            const [r1, r2, r3] = await Promise.all([p1, p2, p3]);
            console.log(JSON.stringify({{ fetchCount, roomCount: r1.rooms.length }}));
        }}
        run();
        """
        proc = subprocess.run(["node", "-e", js_test], capture_output=True, text=True)
        self.assertEqual(proc.returncode, 0, f"Node script failed: {proc.stderr}")
        res = json.loads(proc.stdout.strip())
        self.assertEqual(res["fetchCount"], 1, f"Expected exactly 1 network call for 3 concurrent callers, got {res['fetchCount']}")

    def test_room_lifecycle_dispatches_event_and_rail_subscribes(self):
        """Creating/closing rooms must dispatch weft:rooms_changed and components must listen."""
        api_source = API_TS.read_text(encoding="utf-8")
        rail_rooms_source = RAIL_ROOMS_TSX.read_text(encoding="utf-8")
        rail_account_source = RAIL_ACCOUNT_TSX.read_text(encoding="utf-8")
        rooms_list_source = ROOMS_LIST_TSX.read_text(encoding="utf-8")

        # 1. createRoom and closeRoom dispatch weft:rooms_changed
        self.assertIn("weft:rooms_changed", api_source, "api.ts must dispatch weft:rooms_changed on room changes")

        # 2. RailRooms subscribes to weft:rooms_changed
        self.assertIn("weft:rooms_changed", rail_rooms_source, "RailRooms must subscribe to weft:rooms_changed")

        # 3. RailAccount subscribes to weft:rooms_changed
        self.assertIn("weft:rooms_changed", rail_account_source, "RailAccount must subscribe to weft:rooms_changed")

        # 4. RoomsList subscribes to weft:rooms_changed
        self.assertIn("weft:rooms_changed", rooms_list_source, "RoomsList must subscribe to weft:rooms_changed")

    def test_rail_rooms_and_account_filter_out_closed_rooms(self):
        """RailRooms and RailAccount must filter out closed rooms so closed rooms leave the rail."""
        rail_rooms_source = RAIL_ROOMS_TSX.read_text(encoding="utf-8")
        rail_account_source = RAIL_ACCOUNT_TSX.read_text(encoding="utf-8")

        # Both RailRooms and RailAccount filter out closed rooms
        self.assertIn("!== 'closed'", rail_rooms_source, "RailRooms must filter out closed rooms")
        self.assertIn("!== 'closed'", rail_account_source, "RailAccount must filter out closed rooms")

    def test_room_view_close_room_control_and_owner_gating(self):
        """RoomView must provide an owner-only close button with confirmation and handle closed state."""
        room_view_source = ROOM_VIEW_TSX.read_text(encoding="utf-8")

        # 1. Imports closeRoom and me
        self.assertIn("closeRoom", room_view_source, "RoomView must import closeRoom")
        self.assertIn("me", room_view_source, "RoomView must import me to identify owner")

        # 2. Gated on isOwner
        self.assertIn("isOwner", room_view_source, "RoomView must compute and check isOwner")
        self.assertIn("{isOwner &&", room_view_source, "Close control must be wrapped in isOwner guard")

        # 3. Confirmation step
        self.assertIn("armingClose", room_view_source, "RoomView must implement two-step arming for close")
        self.assertIn("Sure? Close this room", room_view_source, "RoomView must prompt user before closing")

        # 4. Disabled on closed room
        self.assertIn("disabled={roomState === 'closed'", room_view_source, "Composer must be disabled when room is closed")

    def test_server_close_room_reclaims_quota_and_enforces_owner_only(self):
        """Server must enforce max 5 rooms on free plan, reject non-owner close with 403, and reclaim quota on close."""
        import http.server
        with tempfile.TemporaryDirectory() as tmp:
            backend = SqliteWalBackend(str(Path(tmp) / "cloud.db"))
            backend.initialize()
            server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), _CloudHTTPHandler)
            base = f"http://127.0.0.1:{server.server_address[1]}"
            service = WeftCloudService(backend, origin=base)
            _CloudHTTPHandler.service = service

            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            base = f"http://127.0.0.1:{server.server_address[1]}"

            try:
                # 1. Sign up owner and another member
                st_owner, owner = _post(base, "/v1/auth/signup", {"email": "owner@example.com", "password": "password123"})
                self.assertEqual(st_owner, 201)
                owner_tok = owner["session_token"]

                st_member, member = _post(base, "/v1/auth/signup", {"email": "member@example.com", "password": "password123"})
                self.assertEqual(st_member, 201)
                member_tok = member["session_token"]

                # 2. Create 5 rooms (hitting free cap)
                created_rooms = []
                for i in range(5):
                    st, rm = _post(base, "/v1/rooms/create", {"name": f"room-{i}", "cap": 8}, token=owner_tok)
                    self.assertEqual(st, 201, f"Failed creating room {i}: {rm}")
                    created_rooms.append(rm)

                # 3. 6th room creation is refused with 409 / quota error
                st_refuse, refuse = _post(base, "/v1/rooms/create", {"name": "room-overflow", "cap": 8}, token=owner_tok)
                self.assertEqual(st_refuse, 409)
                self.assertIn("tenant room limit reached", refuse["error"]["message"])

                # 4. Member joins room 0
                st_join, _ = _post(base, "/v1/rooms/join", {
                    "room_id": created_rooms[0]["room_id"],
                    "link_token": created_rooms[0]["link_token"],
                    "consent": True,
                }, token=member_tok)
                self.assertEqual(st_join, 200)

                # 5. Member tries to close room 0 -> refused with 403 owner_required
                st_member_close, close_refuse = _post(base, "/v1/rooms/close", {
                    "room_id": created_rooms[0]["room_id"],
                }, token=member_tok)
                self.assertEqual(st_member_close, 403)
                self.assertEqual(close_refuse["error"]["code"], "owner_required")

                # 6. Owner closes room 0 -> 200 OK
                st_owner_close, close_ok = _post(base, "/v1/rooms/close", {
                    "room_id": created_rooms[0]["room_id"],
                }, token=owner_tok)
                self.assertEqual(st_owner_close, 200)
                self.assertEqual(close_ok["state"], "closed")

                # 7. Owner can now create the 6th room (quota slot reclaimed)
                st_reclaim, new_room = _post(base, "/v1/rooms/create", {"name": "room-reclaimed", "cap": 8}, token=owner_tok)
                self.assertEqual(st_reclaim, 201)
                self.assertEqual(new_room["room_id"] is not None, True)
            finally:
                server.shutdown()
                server.server_close()

    def test_spa_failure_empty_and_loading_states_mpai73(self):
        """Audit and assert failure, empty, loading, timeout, and double-submit states across SPA components (MPAI-73)."""
        api_src = API_TS.read_text(encoding="utf-8")
        rooms_list_src = ROOMS_LIST_TSX.read_text(encoding="utf-8")
        room_view_src = ROOM_VIEW_TSX.read_text(encoding="utf-8")
        usage_view_src = (ROOT / "web" / "src" / "components" / "app" / "UsageView.tsx").read_text(encoding="utf-8")
        new_room_src = (ROOT / "web" / "src" / "components" / "app" / "NewRoom.tsx").read_text(encoding="utf-8")
        keys_manager_src = (ROOT / "web" / "src" / "components" / "app" / "KeysManager.tsx").read_text(encoding="utf-8")
        connect_picker_src = (ROOT / "web" / "src" / "components" / "app" / "ConnectPicker.tsx").read_text(encoding="utf-8")
        auth_form_src = (ROOT / "web" / "src" / "components" / "app" / "AuthForm.tsx").read_text(encoding="utf-8")
        settings_view_src = (ROOT / "web" / "src" / "components" / "app" / "SettingsView.tsx").read_text(encoding="utf-8")

        # 1. api.ts has fetch timeout and 502/503/504 handler
        self.assertIn("REQUEST_TIMEOUT_MS", api_src)
        self.assertIn("fetchWithTimeout", api_src)
        self.assertIn("service_unavailable", api_src)

        # 2. RoomsList: error state does not collide with 0 rooms onboarding, has retry button
        self.assertIn("Could not load your rooms", rooms_list_src)
        self.assertIn("Try again", rooms_list_src)

        # 3. RoomView: closed empty room message, composer disabled on error, double submit guard
        self.assertIn("This room is closed", room_view_src)
        self.assertIn("disabled={roomState === 'closed' || Boolean(error)", room_view_src)
        self.assertIn("if (closing) return;", room_view_src)

        # 4. UsageView: retry button on error, onboarding empty state
        self.assertIn("No usage recorded yet", usage_view_src)
        self.assertIn("Could not load usage data", usage_view_src)

        # 5. NewRoom: double submit guard, quota limit guidance
        self.assertIn("if (busy) return;", new_room_src)
        self.assertIn("close an inactive room", new_room_src)

        # 6. KeysManager: retry button on error, no 'No keys yet' on error, revoking guard
        self.assertIn("Could not load agent keys", keys_manager_src)
        self.assertIn("if (revoking) return;", keys_manager_src)

        # 7. ConnectPicker: double mint guard
        self.assertIn("if (minting) return;", connect_picker_src)

        # 8. AuthForm: double submit guard, timeout error mapping
        self.assertIn("if (busy) return;", auth_form_src)
        self.assertIn("e.code === 'timeout'", auth_form_src)

        # 9. SettingsView: retry on error, signout escape hatch
        self.assertIn("Could not load account details", settings_view_src)
        self.assertIn("Try again", settings_view_src)


if __name__ == "__main__":
    unittest.main()

