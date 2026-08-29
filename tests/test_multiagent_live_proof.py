"""Concurrent multi-agent proof over the hosted MCP HTTP surface.

This is the repeatable local counterpart to the production MPAI-99 transcript:
it runs the real ``WeftCloudService`` on a real HTTP socket with real SQLite
storage, then drives five independent agent-key clients through one room link.
No room internals or SQLite mocks are used.  The test is intentionally one
scenario so its result reads like the product promise:

    one owner-created link -> five concurrent joins -> one broadcast ->
    five polls/acks -> five replay-clean cursors.

The owner account and all five keys are fresh per test.  ``tearDownClass``
removes the temporary database; the test also closes the Room in ``finally``
so a future persistent fixture can use the same proof safely.
"""

from __future__ import annotations

import sys
import threading
import unittest
from concurrent.futures import ThreadPoolExecutor
from http import HTTPStatus
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from test_hosted_mcp import HostedMCPTestBase, _mcp, _post  # noqa: E402


AGENT_COUNT = 5
ROOM_CAP = AGENT_COUNT + 1  # owner account occupies the first seat
MCP_PROTOCOL = "2025-03-26"


def _structured(response: dict | None) -> dict:
    """Return structuredContent or a diagnostic object without hiding errors."""
    result = (response or {}).get("result") or {}
    if result.get("isError"):
        content = result.get("content") or []
        text = content[0].get("text", "") if content else ""
        return {"_error": text}
    value = result.get("structuredContent")
    return value if isinstance(value, dict) else {"_error": "missing structuredContent"}


class HostedMCPMultiAgentProofTests(HostedMCPTestBase):
    """Five independent authenticated clients enter through one link."""

    def _join_one(
        self,
        index: int,
        token: str,
        room_id: str,
        link_token: str,
        start: threading.Barrier,
    ) -> dict:
        """Run a complete initialize/list/join sequence for one agent key."""
        # Make the five room_join requests overlap instead of merely running
        # one after another in a pool.  A timeout turns a broken worker into a
        # useful test failure rather than a permanently hung suite.
        start.wait(timeout=15)
        base_id = 100 + index * 10
        status, initialized = _mcp(
            self.base,
            "initialize",
            {"protocolVersion": MCP_PROTOCOL, "capabilities": {}},
            token=token,
            request_id=base_id,
            timeout=15,
        )
        if status != HTTPStatus.OK:
            return {"index": index, "stage": "initialize", "status": status, "body": initialized}

        notification_status, notification = _mcp(
            self.base,
            "notifications/initialized",
            {},
            token=token,
            request_id=None,
            notification=True,
            timeout=15,
        )
        if notification_status != HTTPStatus.ACCEPTED or notification is not None:
            return {
                "index": index,
                "stage": "notifications/initialized",
                "status": notification_status,
                "body": notification,
            }

        list_status, listing = _mcp(
            self.base,
            "tools/list",
            None,
            token=token,
            request_id=base_id + 1,
            timeout=15,
        )
        if list_status != HTTPStatus.OK:
            return {"index": index, "stage": "tools/list", "status": list_status, "body": listing}
        tools = ((listing or {}).get("result") or {}).get("tools") or []
        if len(tools) != 15:
            return {
                "index": index,
                "stage": "tools/list",
                "status": list_status,
                "tool_count": len(tools),
            }

        join_status, joined = _mcp(
            self.base,
            "tools/call",
            {
                "name": "room_join",
                "arguments": {
                    "room_id": room_id,
                    "link_token": link_token,
                    "consent": True,
                },
            },
            token=token,
            request_id=base_id + 2,
            timeout=15,
        )
        return {
            "index": index,
            "stage": "room_join",
            "status": join_status,
            "initialized": _structured(initialized),
            "joined": _structured(joined),
        }

    def _poll_one(self, index: int, token: str, room_id: str) -> dict:
        status, response = _mcp(
            self.base,
            "tools/call",
            {
                "name": "room_poll",
                "arguments": {"room_id": room_id, "after_seq": 0, "limit": 100},
            },
            token=token,
            request_id=700 + index,
            timeout=15,
        )
        return {"index": index, "status": status, "poll": _structured(response)}

    def _ack_one(self, index: int, token: str, room_id: str, seq: int) -> dict:
        status, response = _mcp(
            self.base,
            "tools/call",
            {
                "name": "room_ack",
                "arguments": {"room_id": room_id, "seq": seq},
            },
            token=token,
            request_id=800 + index,
            timeout=15,
        )
        return {"index": index, "status": status, "ack": _structured(response)}

    def _replay_one(self, index: int, token: str, room_id: str) -> dict:
        status, response = _mcp(
            self.base,
            "tools/call",
            {
                "name": "room_poll",
                "arguments": {"room_id": room_id},
            },
            token=token,
            request_id=900 + index,
            timeout=15,
        )
        return {"index": index, "status": status, "poll": _structured(response)}

    def test_one_link_admits_five_concurrent_agents_and_replays_broadcast(self) -> None:
        account = self._signup("multiagent-live-proof@example.com")
        owner_session = account["session_token"]
        room_id: str | None = None
        try:
            # Mint five independent keys under one account.  The room owner is
            # the account identity; each key becomes a separate room member.
            agent_tokens: list[str] = []
            for index in range(AGENT_COUNT):
                status, key = _post(
                    self.base,
                    "/v1/agent-keys",
                    {"label": f"multiagent-proof-{index}"},
                    token=owner_session,
                )
                self.assertEqual(status, HTTPStatus.CREATED, f"key {index} create failed: {key}")
                self.assertTrue(isinstance(key.get("agent_key"), str) and key["agent_key"].startswith("agk_"))
                agent_tokens.append(key["agent_key"])

            create_status, created = _post(
                self.base,
                "/v1/rooms/create",
                {
                    "cap": ROOM_CAP,
                    "name": "multiagent-live-proof",
                    "ttl_seconds": 300,
                },
                token=owner_session,
            )
            self.assertEqual(create_status, HTTPStatus.CREATED, f"room create failed: {created}")
            room_id = created["room_id"]
            link_token = created["link_token"]
            self.assertTrue(room_id.startswith("room_"))
            self.assertTrue(link_token.startswith("rm_"))
            self.assertTrue(created["shareable_link"].endswith(f"/j/{link_token}"))

            # Every client uses the exact same link token, concurrently.
            start = threading.Barrier(AGENT_COUNT)
            with ThreadPoolExecutor(max_workers=AGENT_COUNT, thread_name_prefix="weft-agent") as pool:
                joins = list(
                    pool.map(
                        lambda item: self._join_one(item[0], item[1], room_id, link_token, start),
                        enumerate(agent_tokens),
                    )
                )

            for result in joins:
                self.assertEqual(result["status"], HTTPStatus.OK, result)
                if "tool_count" in result:
                    self.fail(
                        f"Agent {result.get('index')} failed at stage {result.get('stage')!r}: "
                        f"expected 15 tools, got {result.get('tool_count')}"
                    )
                if "joined" not in result:
                    self.fail(
                        f"Agent {result.get('index')} failed at stage {result.get('stage')!r}: {result}"
                    )
                self.assertNotIn("_error", result["joined"], result)
                self.assertEqual(result["joined"].get("status"), "active", result)
                self.assertTrue(result["joined"].get("agent_id", "").startswith("key_"), result)

            # The owner sees itself plus all five identities, proving that the
            # same link admitted five distinct members rather than five retries
            # of one identity.
            info_status, info_response = _mcp(
                self.base,
                "tools/call",
                {"name": "room_info", "arguments": {"room_id": room_id}},
                token=owner_session,
                request_id=500,
                timeout=15,
            )
            info = _structured(info_response)
            self.assertEqual(info_status, HTTPStatus.OK)
            self.assertEqual(info.get("state"), "active")
            self.assertEqual(info.get("member_count"), ROOM_CAP)
            members = info.get("members") or []
            member_ids = {member.get("agent_id") for member in members}
            self.assertEqual(len(member_ids), ROOM_CAP)
            self.assertTrue(all(result["joined"]["agent_id"] in member_ids for result in joins))

            # A single owner broadcast must fan out to every joined key.
            marker = "five-agents-one-link"
            send_status, send_response = _mcp(
                self.base,
                "tools/call",
                {
                    "name": "room_send",
                    "arguments": {
                        "room_id": room_id,
                        "target_spec": "*",
                        "payload": {"text": marker},
                    },
                },
                token=owner_session,
                request_id=600,
                timeout=15,
            )
            sent = _structured(send_response)
            self.assertEqual(send_status, HTTPStatus.OK)
            self.assertNotIn("_error", sent)
            self.assertEqual(sent.get("recipient_count"), AGENT_COUNT)
            message_seq = sent.get("seq")
            self.assertIsInstance(message_seq, int)

            with ThreadPoolExecutor(max_workers=AGENT_COUNT, thread_name_prefix="weft-poll") as pool:
                polls = list(pool.map(lambda item: self._poll_one(item[0], item[1], room_id), enumerate(agent_tokens)))
            for result in polls:
                self.assertEqual(result["status"], HTTPStatus.OK, result)
                events = result["poll"].get("events") or []
                matches = [
                    event
                    for event in events
                    if isinstance(event.get("payload"), dict)
                    and isinstance(event["payload"].get("payload"), dict)
                    and event["payload"]["payload"].get("text") == marker
                ]
                self.assertEqual(len(matches), 1, result)
                self.assertEqual(matches[0].get("seq"), message_seq, result)

            # Each recipient acknowledges independently; this is the durable
            # read receipt path rather than a sender-side HTTP 200 assertion.
            with ThreadPoolExecutor(max_workers=AGENT_COUNT, thread_name_prefix="weft-ack") as pool:
                acks = list(pool.map(lambda item: self._ack_one(item[0], item[1], room_id, message_seq), enumerate(agent_tokens)))
            for result in acks:
                self.assertEqual(result["status"], HTTPStatus.OK, result)
                self.assertEqual(result["ack"].get("last_ack_seq"), message_seq, result)
                self.assertEqual(result["ack"].get("receipts_read"), 1, result)

            # A subsequent default-cursor poll must not replay the acknowledged
            # message for any of the five independent members.
            with ThreadPoolExecutor(max_workers=AGENT_COUNT, thread_name_prefix="weft-replay") as pool:
                replays = list(pool.map(lambda item: self._replay_one(item[0], item[1], room_id), enumerate(agent_tokens)))
            for result in replays:
                self.assertEqual(result["status"], HTTPStatus.OK, result)
                self.assertEqual(result["poll"].get("events"), [], result)
        finally:
            # Keep cleanup on the owner path even if a concurrent worker fails.
            if room_id is not None:
                _mcp(
                    self.base,
                    "tools/call",
                    {"name": "room_close", "arguments": {"room_id": room_id}},
                    token=owner_session,
                    request_id=999,
                    timeout=15,
                )


if __name__ == "__main__":
    unittest.main()
