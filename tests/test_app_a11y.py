"""Accessibility regressions for the authenticated app routes (MPAI-69/70)."""

from __future__ import annotations

import re
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parent.parent
ROOMS_LIST_TSX = ROOT / "web" / "src" / "components" / "app" / "RoomsList.tsx"
USAGE_TSX = ROOT / "web" / "src" / "components" / "app" / "UsageView.tsx"
ROOM_VIEW_TSX = ROOT / "web" / "src" / "components" / "app" / "RoomView.tsx"


class TestAuthenticatedAppAccessibility(unittest.TestCase):
    """Keep the app's link collections and scroll regions operable by keyboard."""

    def test_room_link_collections_use_native_list_semantics(self) -> None:
        for path, label in ((ROOMS_LIST_TSX, "Your rooms"), (USAGE_TSX, "Rooms by size")):
            source = path.read_text(encoding="utf-8")
            self.assertNotIn('role="table"', source, path.name)
            self.assertRegex(
                source,
                rf'<ul aria-label="{re.escape(label)}">',
                path.name,
            )
            self.assertRegex(
                source,
                r'<li[^>]*>\s*<a className="row row--room"',
                path.name,
            )

    def test_room_scroll_regions_are_focusable_and_named(self) -> None:
        source = ROOM_VIEW_TSX.read_text(encoding="utf-8")
        self.assertRegex(
            source,
            r'<div\s+className="feed__scroll"[^>]*tabIndex=\{0\}[^>]*role="region"[^>]*aria-label="Room messages, scrollable"',
        )
        self.assertRegex(
            source,
            r'<aside className="insp" aria-label="Room details"[^>]*tabIndex=\{0\}[^>]*role="region"',
        )
        self.assertEqual(source.count('aria-label="Room details"'), 1)


if __name__ == "__main__":
    unittest.main()
