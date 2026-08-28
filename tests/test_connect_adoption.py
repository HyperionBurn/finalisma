from __future__ import annotations

from pathlib import Path
import sys
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from weft_cloud.web.copy import connect_page_body


class ConnectAdoptionCopyTests(unittest.TestCase):
    def setUp(self) -> None:
        self.html = connect_page_body("room_123", "rm_" + "a" * 43)

    def test_human_join_page_teaches_zero_install_bridge_download(self) -> None:
        self.assertIn(
            "curl -fsSL -o weft-mcp-bridge.py https://&lt;origin&gt;/downloads/weft-mcp-bridge.py",
            self.html,
        )
        self.assertIn("/downloads/weft-mcp-bridge.py", self.html)
        self.assertNotIn("Install the <code>weft-mcp</code> package", self.html)
        self.assertNotIn("python -m weft_mcp", self.html)

    def test_human_join_page_has_single_line_curl_join(self) -> None:
        self.assertIn(
            "curl -fsS -X POST https://&lt;origin&gt;/v1/rooms/join",
            self.html,
        )
        self.assertIn('Authorization: Bearer $WEFT_TOKEN', self.html)
        self.assertIn('"room_id":"room_123"', self.html)
        self.assertIn('"consent":true', self.html)


if __name__ == "__main__":
    unittest.main()
