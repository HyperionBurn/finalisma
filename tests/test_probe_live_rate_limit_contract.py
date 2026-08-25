from pathlib import Path
import unittest


SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "probe_live_rate_limit.cjs"


class TestLiveRateLimitProbeContract(unittest.TestCase):
    def test_probe_covers_bounded_structured_rate_limit_workflow(self):
        text = SCRIPT.read_text(encoding="utf-8")
        for marker in (
            "weft-live-rate-limit-v1",
            "--requests",
            "--concurrency",
            "/rooms/send",
            "burst_completed_without_transport_reset",
            "rate_limit_error_code_is_structured",
            "retry_after_is_present_and_numeric",
            "browser_cleanup_deleted_organization",
        ):
            self.assertIn(marker, text)

    def test_probe_does_not_print_credentials(self):
        text = SCRIPT.read_text(encoding="utf-8")
        self.assertNotIn("console.log(email", text)
        self.assertNotIn("console.log(password", text)
        self.assertNotIn("console.log(sessionToken", text)


if __name__ == "__main__":
    unittest.main()
