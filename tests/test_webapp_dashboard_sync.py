"""MPAI-60 regression tests:
1. Dashboard must deduplicate concurrent list-rooms calls on load into a single network call.
2. Room creation/closure must dispatch `weft:rooms_changed` event, and RailRooms / RailAccount / RoomsList
   must listen to `weft:rooms_changed` so the sidebar and dashboard update without manual page reload.
"""

from __future__ import annotations

import json
import subprocess
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
API_TS = ROOT / "web" / "src" / "lib" / "api.ts"
RAIL_ROOMS_TSX = ROOT / "web" / "src" / "components" / "app" / "RailRooms.tsx"
RAIL_ACCOUNT_TSX = ROOT / "web" / "src" / "components" / "app" / "RailAccount.tsx"
ROOMS_LIST_TSX = ROOT / "web" / "src" / "components" / "app" / "RoomsList.tsx"


class TestDashboardPapercutsMPAI60(unittest.TestCase):
    """Regression test suite for Kevin's papercuts (MPAI-60)."""

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


if __name__ == "__main__":
    unittest.main()
