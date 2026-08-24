from pathlib import Path
import unittest


SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "probe_live_public_bridge.cjs"


class TestLivePublicBridgeProbeContract(unittest.TestCase):
    def test_probe_covers_downloaded_bridge_roundtrip_and_cleanup(self):
        text = SCRIPT.read_text(encoding="utf-8")
        for marker in (
            "weft-live-public-bridge-v1",
            "downloads/weft-mcp-bridge.py",
            "--token-env",
            "room_create",
            "room_join",
            "room_send",
            "room_poll",
            "room_ack",
            "browser_cleanup_deleted_organization",
        ):
            self.assertIn(marker, text)

    def test_probe_does_not_print_credentials(self):
        text = SCRIPT.read_text(encoding="utf-8")
        self.assertNotIn("console.log(agentKey", text)
        self.assertNotIn("console.log(sessionToken", text)
        self.assertNotIn("console.log(password", text)


if __name__ == "__main__":
    unittest.main()
