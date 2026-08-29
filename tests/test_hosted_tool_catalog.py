"""Keep the published hosted MCP catalog aligned with runtime registration."""

from __future__ import annotations

import re
import sys
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from weft_cloud.mcp import HOSTED_TOOLS, HostedMCPDispatcher  # noqa: E402


_ROOM_CODE = re.compile(r"(?:<code>|`)(room_[a-z_]+)(?:</code>|`)")
_REGISTERED_NAMES = [tool["name"] for tool in HOSTED_TOOLS]


def _room_names(match: re.Match[str], group: int = 1) -> list[str]:
    return _ROOM_CODE.findall(match.group(group))


class HostedToolCatalogDocumentationTests(unittest.TestCase):
    def test_published_hosted_catalog_matches_registered_tools(self) -> None:
        """A new registered tool must make every published list fail loudly."""
        self.assertEqual(
            len(_REGISTERED_NAMES),
            len(set(_REGISTERED_NAMES)),
            "runtime hosted tool registration contains duplicate names",
        )
        missing_handlers = [
            name
            for name in _REGISTERED_NAMES
            if not hasattr(HostedMCPDispatcher, f"_tool_{name}")
        ]
        self.assertEqual(
            missing_handlers,
            [],
            "runtime catalog names without a dispatcher handler",
        )

        quickstart = (ROOT / "site" / "docs" / "quickstart.html").read_text(encoding="utf-8")
        quickstart_match = re.search(
            r"<code>tools/list</code>\s+returns\s+<strong>(\d+) tools</strong>:\s*(.*?)\.\s*Use\s+<code>room_list</code>",
            quickstart,
            re.DOTALL,
        )
        self.assertIsNotNone(quickstart_match, "quickstart hosted tools/list catalog is missing")
        assert quickstart_match is not None
        self.assertEqual(int(quickstart_match.group(1)), len(_REGISTERED_NAMES))

        protocol = (ROOT / "site" / "docs" / "protocol.html").read_text(encoding="utf-8")
        protocol_match = re.search(
            r"endpoint exposes the room set \((.*?)\)\.\s*Agent-key",
            protocol,
            re.DOTALL,
        )
        self.assertIsNotNone(protocol_match, "protocol hosted room tool list is missing")
        assert protocol_match is not None

        design = (ROOT / "docs" / "HOSTED_MCP_DESIGN.md").read_text(encoding="utf-8")
        design_match = re.search(
            r"`CloudRoomService` method[^\n]*:\s*\n\n(.*?)\n\nThe member lifecycle",
            design,
            re.DOTALL,
        )
        self.assertIsNotNone(design_match, "hosted MCP design tool list is missing")
        assert design_match is not None

        expected = sorted(_REGISTERED_NAMES)
        for label, match, group in (
            ("site/docs/quickstart.html", quickstart_match, 2),
            ("site/docs/protocol.html", protocol_match, 1),
            ("docs/HOSTED_MCP_DESIGN.md", design_match, 1),
        ):
            with self.subTest(surface=label):
                self.assertEqual(sorted(_room_names(match, group)), expected)
