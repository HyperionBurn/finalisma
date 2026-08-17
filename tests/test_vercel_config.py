from __future__ import annotations

import json
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from weft_cloud.web.security_headers import security_headers


class VercelConfigTests(unittest.TestCase):
    def test_static_deploy_headers_match_coordinator_contract(self) -> None:
        root = Path(__file__).resolve().parents[1]
        config = json.loads((root / "vercel.json").read_text(encoding="utf-8"))
        rules = config.get("headers", [])
        wildcard = next((rule for rule in rules if rule.get("source") == "/(.*)"), None)
        self.assertIsNotNone(wildcard, "vercel.json must protect every static route")
        configured = {
            item["key"]: item["value"] for item in wildcard["headers"]
        }
        expected = dict(security_headers(html=True))
        self.assertEqual(configured, expected)


if __name__ == "__main__":
    unittest.main()
