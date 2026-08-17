from __future__ import annotations

from pathlib import Path
import sys
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from weft_cloud.web.copy import connect_page_body
CONNECT_TIERS = ROOT / "web" / "src" / "components" / "ConnectTiers.astro"
AGENT_KEYS = ROOT / "docs" / "AGENT_KEYS.md"


def _read(path: Path) -> str:
    return path.read_text(encoding="utf-8")


class OnboardingConsistencyTests(unittest.TestCase):
    def test_connect_tiers_prefers_agent_keys_for_hosted_copy(self) -> None:
        copy = _read(CONNECT_TIERS)

        self.assertIn('<agk_ agent key>', copy)
        self.assertNotIn('<fss_ session token>', copy)
        self.assertIn('via an MCP stdio\n        bridge configured for remote mode', copy)


    def test_connect_tiers_sdk_room_join_uses_public_wrapper(self) -> None:
        copy = _read(CONNECT_TIERS)

        self.assertIn('result = client.join_room(', copy)
        self.assertIn('link_token="rm_..."', copy)
        self.assertIn('consent=True', copy)
        self.assertNotIn('client._call("room_join", {', copy)


    def test_cloud_connect_page_keeps_hosted_and_self_hosted_boundaries_clear(self) -> None:
        html = connect_page_body('room_<unsafe>', 'rm_<unsafe>')

        self.assertIn('agk_...', html)
        self.assertIn('session_token_or_agent_key', html)
        self.assertIn('This is not a hosted stdio server', html)
        self.assertIn('stdio remains a local process boundary', html)
        self.assertIn('weft_sdk.WeftClient', html)
        self.assertIn('there is no hosted-cloud SDK client', html)
        self.assertIn('a session joins as your account, and an agent key joins as its own distinct agent identity', html)
        self.assertIn('room_&lt;unsafe&gt;', html)
        self.assertIn('rm_&lt;unsafe&gt;', html)


    def test_agent_keys_doc_has_one_identity_contract(self) -> None:
        doc = _read(AGENT_KEYS)

        self.assertIn('Multiple keys for one', doc)
        self.assertIn('account therefore create separate hosted agent identities', doc)
        self.assertIn('Hosted MCP is the Streamable HTTP endpoint `POST /mcp`', doc)
        self.assertIn('not a hosted stdio endpoint', doc)
        self.assertIn('legacy self-hosted actor/pairing credentials are not interchangeable', doc)
        self.assertIn('distinct,\nstable room identity', doc)
        self.assertIn('agk_` key authenticates the account that owns it', doc)
