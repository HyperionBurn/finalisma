"""Regression coverage for the root favicon served by the VM site."""

from __future__ import annotations

import struct
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


class FaviconSourceTests(unittest.TestCase):
    def test_root_favicon_is_valid_and_shared_layouts_reference_it(self) -> None:
        icon = ROOT / "web" / "public" / "favicon.ico"
        self.assertTrue(icon.is_file(), "the Astro public source must contain /favicon.ico")
        data = icon.read_bytes()
        self.assertGreaterEqual(len(data), 6, "favicon.ico is too short to contain an ICO header")
        reserved, image_type, image_count = struct.unpack_from("<HHH", data)
        self.assertEqual(reserved, 0)
        self.assertEqual(image_type, 1, "root favicon must be an ICO, not a cursor")
        self.assertGreaterEqual(image_count, 1)
        self.assertGreaterEqual(len(data), 6 + 16 * image_count)
        for index in range(image_count):
            _width, _height, _colors, _reserved, _planes, _bits, size, offset = struct.unpack_from(
                "<BBBBHHII", data, 6 + 16 * index
            )
            self.assertGreater(size, 0, f"ICO image {index} has no payload")
            self.assertLessEqual(offset + size, len(data), f"ICO image {index} points outside the file")

        for layout in ("web/src/layouts/AuthShell.astro", "web/src/layouts/AppShell.astro"):
            source = (ROOT / layout).read_text(encoding="utf-8")
            self.assertIn(
                '<link rel="icon" href="/favicon.ico" type="image/x-icon"',
                source,
                f"{layout} must reference the VM-root favicon",
            )
