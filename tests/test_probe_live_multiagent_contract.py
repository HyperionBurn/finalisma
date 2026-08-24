from pathlib import Path
import unittest


SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "probe_live_multiagent_roundtrip.cjs"


class TestLiveMultiagentProbeContract(unittest.TestCase):
    def test_probe_exercises_real_cross_tenant_roundtrip_and_cleanup(self):
        text = SCRIPT.read_text(encoding="utf-8")
        for marker in (
            "weft-live-multiagent-roundtrip-v1",
            "--api-origin",
            "/rooms/create",
            "/rooms/join",
            "/rooms/send",
            "/rooms/poll",
            "/rooms/event_log",
            "cross_tenant_join",
            "targeted-roundtrip",
            "broadcast-roundtrip",
            "non_member_send_refused",
            "browser_cleanup_deleted_all_organizations",
        ):
            self.assertIn(marker, text)

    def test_probe_does_not_print_disposable_credentials(self):
        text = SCRIPT.read_text(encoding="utf-8")
        self.assertNotIn("console.log(email", text)
        self.assertNotIn("console.log(password", text)
        self.assertNotIn("console.log(token", text)


if __name__ == "__main__":
    unittest.main()
