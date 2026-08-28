from __future__ import annotations

from pathlib import Path
import sys
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from weft_cloud.web.copy import connect_page_body
CONNECT_PICKER = ROOT / "web" / "src" / "components" / "app" / "ConnectPicker.tsx"
KEYS_MANAGER = ROOT / "web" / "src" / "components" / "app" / "KeysManager.tsx"
AGENT_KEYS = ROOT / "docs" / "AGENT_KEYS.md"
SDK = ROOT / "docs" / "SDK.md"


def _read(path: Path) -> str:
    return path.read_text(encoding="utf-8")


class OnboardingConsistencyTests(unittest.TestCase):
    def test_connect_picker_uses_downloaded_bridge_and_utf8_environment(self) -> None:
        copy = _read(CONNECT_PICKER)
        keys = _read(KEYS_MANAGER)

        self.assertIn("PATH = '<path-to-the-file-you-downloaded>'", copy)
        self.assertIn('weft-mcp-bridge.py', copy)
        self.assertIn("'--token-env', 'WEFT_TOKEN'", copy)
        self.assertIn("PYTHONUTF8 = \"1\"", copy)
        self.assertNotIn('actor_token=', copy)
        self.assertIn("sessionStorage.setItem", keys)
        self.assertIn("sessionStorage.getItem", copy)
        self.assertIn("sessionStorage.removeItem", copy)
        self.assertNotIn("?key=", keys)
        self.assertIn("Headless or CI Codex runs must permit MCP tool calls", copy)


    def test_connect_picker_keeps_host_origin_outside_the_downloaded_path(self) -> None:
        copy = _read(CONNECT_PICKER)

        self.assertIn('APP_ORIGIN_EXAMPLE', copy)
        self.assertIn('useState(APP_ORIGIN_EXAMPLE)', copy)
        self.assertIn("'--remote', origin", copy)
        self.assertIn('const [origin, setOrigin]', copy)


    def test_cloud_connect_page_keeps_hosted_and_self_hosted_boundaries_clear(self) -> None:
        html = connect_page_body('room_<unsafe>', 'rm_<unsafe>')

        self.assertIn('agk_...', html)
        self.assertIn('session_token_or_agent_key', html)
        self.assertIn('This is not a hosted stdio server', html)
        self.assertIn('stdio remains a local process boundary', html)
        self.assertIn('weft_sdk.WeftClient', html)
        self.assertIn('coordinator_url="https://&lt;origin&gt;/mcp"', html)
        self.assertIn('bearer_token=os.environ["WEFT_TOKEN"]', html)
        self.assertIn('Hosted mode derives identity from the bearer credential', html)
        self.assertIn('Creator-key note', html)
        self.assertIn('must redeem this returned link', html)
        self.assertIn('before that key can call', html)
        self.assertNotIn('there is no hosted-cloud SDK client', html)
        self.assertIn('a session joins as your account, and an agent key joins as its own distinct agent identity', html)
        self.assertIn('Treat this URL like a password', html)
        self.assertIn('revoke the link if it is exposed', html)
        self.assertIn('room_&lt;unsafe&gt;', html)
        self.assertIn('rm_&lt;unsafe&gt;', html)

    def test_hosted_sdk_docs_repeat_creator_key_redemption_contract(self) -> None:
        doc = _read(SDK)

        self.assertIn('the key is a distinct room identity', doc)
        self.assertIn('before that key calls', doc)
        self.assertIn('room_poll()', doc)
        self.assertIn('send()', doc)


    def test_agent_keys_doc_has_one_identity_contract(self) -> None:
        doc = _read(AGENT_KEYS)

        self.assertIn('Multiple keys for one', doc)
        self.assertIn('account therefore create separate hosted agent identities', doc)
        self.assertIn('Hosted MCP is the Streamable HTTP endpoint `POST /mcp`', doc)
        self.assertIn('not a hosted stdio endpoint', doc)
        self.assertIn('legacy self-hosted actor/pairing credentials are not interchangeable', doc)
        self.assertIn('distinct,\nstable room identity', doc)
        self.assertIn('agk_` key authenticates the account that owns it', doc)
