"""Every MCP input field must be self-describing.

An MCP host hands these schemas straight to the model, so a field with no
description is a field the caller has to GUESS. Two shipped bare and cost
real time on production:

  * ``target_spec`` was ``{}``. Only ``"*"`` broadcasts; "all", "everyone",
    "room" and "broadcast" are read as member ids and refused. Finding that
    took seven guesses.
  * ``cap`` was ``{"type": "integer"}``. It counts the owning account, so
    cap=15 admits fourteen joiners — discovered only by burning a slot.

These tests pin the PROPERTY (every field explains itself), not any wording,
so the copy stays free to improve.
"""
from __future__ import annotations

import unittest

from weft_cloud.mcp import HOSTED_TOOLS


class ToolSchemaDiscoverabilityTests(unittest.TestCase):
    def test_every_tool_has_a_description(self):
        missing = [t["name"] for t in HOSTED_TOOLS if not t.get("description")]
        self.assertEqual(missing, [], f"tools with no description: {missing}")

    def test_every_input_field_has_a_description(self):
        missing = [
            f"{t['name']}.{field}"
            for t in HOSTED_TOOLS
            for field, schema in t["inputSchema"]["properties"].items()
            if not schema.get("description")
        ]
        self.assertEqual(
            missing, [],
            "these fields are handed to a model with no explanation, so it can "
            f"only guess: {missing}",
        )

    def test_descriptions_are_substantive(self):
        """A one-word description is the same failure wearing a hat."""
        thin = [
            f"{t['name']}.{field}"
            for t in HOSTED_TOOLS
            for field, schema in t["inputSchema"]["properties"].items()
            if len(schema.get("description", "")) < 25
        ]
        self.assertEqual(thin, [], f"these descriptions say too little: {thin}")

    def test_target_spec_still_names_the_broadcast_token(self):
        send = next(t for t in HOSTED_TOOLS if t["name"] == "room_send")
        desc = send["inputSchema"]["properties"]["target_spec"]["description"]
        self.assertIn('"*"', desc, "the only broadcast token must be named")

    def test_consent_states_it_must_be_a_literal_boolean(self):
        join = next(t for t in HOSTED_TOOLS if t["name"] == "room_join")
        desc = join["inputSchema"]["properties"]["consent"]["description"].lower()
        self.assertIn("true", desc)
        self.assertTrue(
            "literal" in desc or "boolean" in desc,
            "consent refuses the string \"true\"; the schema must say so",
        )

    def test_capped_integer_fields_publish_their_cap(self):
        """Silent clamping is a lie unless the schema admits it."""
        wait = next(t for t in HOSTED_TOOLS if t["name"] == "room_wait")
        self.assertIn("30", wait["inputSchema"]["properties"]["timeout_seconds"]["description"])
        poll = next(t for t in HOSTED_TOOLS if t["name"] == "room_poll")
        self.assertIn("200", poll["inputSchema"]["properties"]["limit"]["description"])
