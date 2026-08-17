"""Public-origin validation for bearer-bearing customer URLs."""

from __future__ import annotations

import os
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from weft_cloud.origin import configured_origin, normalize_origin  # noqa: E402


class PublicOriginTests(unittest.TestCase):
    def test_normalizes_valid_http_and_https_origins(self) -> None:
        self.assertEqual(
            normalize_origin("https://rooms.example.test/", default="http://fallback"),
            "https://rooms.example.test",
        )
        self.assertEqual(
            normalize_origin("http://127.0.0.1:18789", default="http://fallback"),
            "http://127.0.0.1:18789",
        )

    def test_rejects_non_origin_values(self) -> None:
        for value in (
            "javascript:alert(1)",
            "data:text/plain,leak",
            "https://user:pass@example.test",
            "https://example.test/path",
            "https://example.test?next=evil",
            "https://example.test#fragment",
            "https://example.test:bad",
            "https://example.test\nX-Leak: yes",
        ):
            with self.subTest(value=value):
                with self.assertRaises(ValueError):
                    normalize_origin(value, default="http://fallback")

    def test_configured_origin_uses_first_nonblank_environment_value(self) -> None:
        previous = dict(os.environ)
        try:
            os.environ["WEFT_WEB_PUBLIC_ORIGIN"] = "  "
            os.environ["WEFT_PUBLIC_ORIGIN"] = "https://fallback.example.test/"
            self.assertEqual(
                configured_origin(
                    env_names=("WEFT_WEB_PUBLIC_ORIGIN", "WEFT_PUBLIC_ORIGIN"),
                    default="http://fallback",
                ),
                "https://fallback.example.test",
            )
        finally:
            os.environ.clear()
            os.environ.update(previous)


if __name__ == "__main__":
    unittest.main()
