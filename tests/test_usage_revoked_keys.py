"""MPAI-67 regression tests for the Usage page's agent-key metric."""

from __future__ import annotations

import re
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parent.parent
USAGE_TSX = ROOT / "web" / "src" / "components" / "app" / "UsageView.tsx"


class TestUsageRevokedKeysMPAI67(unittest.TestCase):
    """A revoked key must not be reported as active usage."""

    def test_usage_counts_only_unrevoked_agent_keys(self) -> None:
        source = USAGE_TSX.read_text(encoding="utf-8")

        # Usage must consume the canonical typed API so it sees revoked_at,
        # then exclude rows whose revocation timestamp is present.
        self.assertIn("listAgentKeys", source)
        self.assertRegex(source, r"const kd = await listAgentKeys\(\);")
        self.assertRegex(
            source,
            r"setKeys\(\s*\(kd\.keys \?\? \[\]\)\.filter\(\s*\(key\)\s*=>\s*!key\.revoked_at\s*\)\.length\s*\);",
        )
        self.assertNotIn("setKeys((kd.keys ?? []).length)", source)


if __name__ == "__main__":
    unittest.main()
