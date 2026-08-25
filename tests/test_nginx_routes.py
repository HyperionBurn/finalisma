"""Fixture tests for the deterministic nginx route editor."""

from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "scripts"))

from ensure_nginx_routes import ensure_routes  # noqa: E402


BASE_CONFIG = """\
server {
    listen 443 ssl;
    server_name weft.example.com;
    location / {
        proxy_pass http://127.0.0.1:18789;
    }
}
"""

STATIC_SITE_CONFIG = """\
server {
    listen 443 ssl;
    server_name weft.example.com;
    location / {
        root /opt/weft/site;
        index index.html;
        try_files $uri $uri/ $uri/index.html =404;
    }
}
server {
    listen 80;
    server_name weft.example.com;
    location / {
        root /opt/weft/site;
        try_files $uri $uri/ $uri/index.html =404;
    }
}
"""


class NginxRouteEditorTests(unittest.TestCase):
    def _write(self, directory: str, content: str = BASE_CONFIG) -> Path:
        path = Path(directory) / "weft.conf"
        path.write_text(content, encoding="utf-8")
        return path

    def test_inserts_both_routes_into_the_selected_site(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = self._write(tmp)
            self.assertTrue(ensure_routes(path, "weft.example.com"))
            updated = path.read_text(encoding="utf-8")
            self.assertIn("location ^~ /j/ {", updated)
            self.assertIn("location = /mcp {", updated)
            self.assertLess(updated.index("location ^~ /j/ {"), updated.index("location / {"))
            self.assertLess(updated.index("location = /mcp {"), updated.index("location / {"))
            self.assertFalse(ensure_routes(path, "weft.example.com"))

            legacy = self._write(
                tmp,
                BASE_CONFIG.replace(
                    "    location / {\n",
                    "    location /j/ {\n        proxy_pass http://127.0.0.1:18788;\n    }\n"
                    "    location / {\n",
                ),
            )
            self.assertTrue(ensure_routes(legacy, "weft.example.com"))
            legacy_updated = legacy.read_text(encoding="utf-8")
            self.assertEqual(legacy_updated.count("location /j/ {"), 1)
            self.assertNotIn("location ^~ /j/ {", legacy_updated)

    def test_upgrades_legacy_mcp_prefix_and_adds_missing_join_route(self):
        content = BASE_CONFIG.replace(
            "    location / {\n",
            "    location /mcp {\n        proxy_pass http://127.0.0.1:18788;\n    }\n"
            "    location / {\n",
        )
        with tempfile.TemporaryDirectory() as tmp:
            path = self._write(tmp, content)
            self.assertTrue(ensure_routes(path, "weft.example.com"))
            updated = path.read_text(encoding="utf-8")
            self.assertIn("location = /mcp {", updated)
            self.assertNotIn("location /mcp {", updated)
            self.assertIn("location ^~ /j/ {", updated)

    def test_replaces_static_site_catch_all_with_web_app_proxy(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = self._write(tmp, STATIC_SITE_CONFIG)
            self.assertTrue(ensure_routes(path, "weft.example.com"))
            updated = path.read_text(encoding="utf-8")
            self.assertEqual(updated.count("proxy_pass http://127.0.0.1:18789;"), 2)
            self.assertNotIn("root /opt/weft/site", updated)
            self.assertIn("location ^~ /j/ {", updated)
            self.assertIn("location = /mcp {", updated)
            self.assertFalse(ensure_routes(path, "weft.example.com"))

    def test_rejects_an_unknown_catch_all_without_editing(self):
        content = BASE_CONFIG.replace(
            "proxy_pass http://127.0.0.1:18789;",
            "return 404;",
        )
        with tempfile.TemporaryDirectory() as tmp:
            path = self._write(tmp, content)
            original = path.read_text(encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "does not proxy"):
                ensure_routes(path, "weft.example.com")
            self.assertEqual(path.read_text(encoding="utf-8"), original)

    def test_rejects_a_site_with_the_wrong_server_name_without_editing(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = self._write(tmp)
            original = path.read_text(encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "not declared"):
                ensure_routes(path, "other.example.com")
            self.assertEqual(path.read_text(encoding="utf-8"), original)

    def test_ignores_commented_server_name(self):
        content = BASE_CONFIG.replace(
            "    server_name weft.example.com;",
            "    # server_name weft.example.com;",
        )
        with tempfile.TemporaryDirectory() as tmp:
            path = self._write(tmp, content)
            with self.assertRaisesRegex(ValueError, "not declared"):
                ensure_routes(path, "weft.example.com")

    def test_fails_without_a_safe_catch_all_insertion_point_without_editing(self):
        content = BASE_CONFIG.replace(
            "    location / {\n        proxy_pass http://127.0.0.1:18789;\n    }\n",
            "    location ^~ /other/ {\n        proxy_pass http://127.0.0.1:18789;\n    }\n",
        )
        with tempfile.TemporaryDirectory() as tmp:
            path = self._write(tmp, content)
            original = path.read_text(encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "safe location"):
                ensure_routes(path, "weft.example.com")
            self.assertEqual(path.read_text(encoding="utf-8"), original)


if __name__ == "__main__":
    unittest.main()
