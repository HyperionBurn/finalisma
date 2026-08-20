"""Repository hygiene checks for credential-shaped fixtures and transcripts."""

from __future__ import annotations

import re
import sys
import unittest
from pathlib import Path
from collections.abc import Iterator

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

from _transcript_safety import redact_text


_LIVE_WEBHOOK_RE = re.compile(r"\bwhsec_(?:live|prod)_[A-Za-z0-9_-]+\b")
_EXPOSED_ACTOR_RE = re.compile(r"\bfst_actor_[A-Za-z0-9_-]{20,}\b")
_EXPOSED_PAIRING_RE = re.compile(r"\bfst_pair_[A-Za-z0-9_-]{20,}\b")


def _text_files(*roots: Path) -> Iterator[Path]:
    for root in roots:
        if root.is_file():
            yield root
            continue
        for path in root.rglob("*"):
            if path.is_file() and path.suffix.lower() in {
                ".md",
                ".py",
                ".yml",
                ".yaml",
                ".json",
                ".toml",
                ".txt",
            }:
                yield path


class SecretHygieneTests(unittest.TestCase):
    def test_no_live_looking_webhook_fixture_is_tracked(self):
        findings = []
        for path in _text_files(
            ROOT / "docs",
            ROOT / "scripts",
            ROOT / "tests",
            ROOT / ".github",
            ROOT / "README.md",
        ):
            text = path.read_text(encoding="utf-8", errors="replace")
            if _LIVE_WEBHOOK_RE.search(text):
                findings.append(str(path.relative_to(ROOT)))
        self.assertEqual(findings, [])

    def test_historical_transcript_has_no_full_bearer_tokens(self):
        transcript = (ROOT / "docs" / "INTEROP_BRIDGE_2026-08-05.md").read_text(encoding="utf-8")
        self.assertIsNone(_EXPOSED_ACTOR_RE.search(transcript))
        self.assertIsNone(_EXPOSED_PAIRING_RE.search(transcript))

    def test_transcript_redactor_covers_webhook_secrets(self):
        secret = "whsec_" + "live_" + "synthetic_secret_value"
        self.assertEqual(redact_text(f"secret={secret}"), "secret=***")
