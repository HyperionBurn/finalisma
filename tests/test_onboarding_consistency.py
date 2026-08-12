from __future__ import annotations

from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from weft_cloud.web.copy import connect_page_body
CONNECT_TIERS = ROOT / "web" / "src" / "components" / "ConnectTiers.astro"
AGENT_KEYS = ROOT / "docs" / "AGENT_KEYS.md"


def _read(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def test_connect_tiers_prefers_agent_keys_for_hosted_copy() -> None:
    copy = _read(CONNECT_TIERS)

    assert '<agk_ agent key>' in copy
    assert '<fss_ session token>' not in copy
    assert 'via an MCP stdio\n        bridge configured for remote mode' in copy


def test_connect_tiers_sdk_room_join_uses_public_wrapper() -> None:
    copy = _read(CONNECT_TIERS)

    assert 'result = client.join_room(' in copy
    assert 'link_token="rm_..."' in copy
    assert 'consent=True' in copy
    assert 'client._call("room_join", {' not in copy


def test_cloud_connect_page_keeps_hosted_and_self_hosted_boundaries_clear() -> None:
    html = connect_page_body('room_<unsafe>', 'rm_<unsafe>')

    assert 'agk_...' in html
    assert 'session_token_or_agent_key' in html
    assert 'This is not a hosted stdio server' in html
    assert 'stdio remains a local process boundary' in html
    assert 'weft_sdk.WeftClient' in html
    assert 'there is no hosted-cloud SDK client' in html
    assert 'a session joins as your account, and an agent key joins as its own distinct agent identity' in html
    assert 'room_&lt;unsafe&gt;' in html
    assert 'rm_&lt;unsafe&gt;' in html


def test_agent_keys_doc_has_one_identity_contract() -> None:
    doc = _read(AGENT_KEYS)

    assert 'Multiple keys for one' in doc
    assert 'account therefore create separate hosted agent identities' in doc
    assert 'Hosted MCP is the Streamable HTTP endpoint `POST /mcp`' in doc
    assert 'not a hosted stdio endpoint' in doc
    assert 'legacy self-hosted actor/pairing credentials are not interchangeable' in doc
    assert 'distinct,\nstable room identity' in doc
    assert 'agk_` key authenticates the account that owns it' in doc
