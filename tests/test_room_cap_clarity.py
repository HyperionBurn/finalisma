"""Guard the room `cap` contract against the off-by-one every customer hits.

Measured on production 2026-08-23: a room created with cap=15 admitted the
owning account plus FOURTEEN joiners, not fifteen. The owning account
auto-joins on creation and occupies one slot. The tool description at the
time said "one multi-use link admits up to cap agents", which is false by
one, and the `cap` field carried a bare {"type": "integer"} schema with no
description at all — so neither a human nor an agent could learn the real
semantics without burning a slot to discover it.

These tests pin the EXPLANATION, not the arithmetic. The behaviour (cap ==
total members including the owner) is deliberate and unchanged; what must
never regress is a caller's ability to size the room correctly on the first
try.
"""
from __future__ import annotations

import unittest

from weft_cloud.mcp import HOSTED_TOOLS


class RoomCapSchemaClarityTests(unittest.TestCase):
    def setUp(self):
        self.tool = next(t for t in HOSTED_TOOLS if t["name"] == "room_create")
        self.cap = self.tool["inputSchema"]["properties"]["cap"]

    def test_cap_field_is_not_a_bare_integer(self):
        self.assertTrue(
            self.cap.get("description"),
            "cap needs a description: a bare {'type': 'integer'} tells a caller "
            "nothing about whether the owning account counts toward it",
        )

    def test_cap_description_states_the_owner_is_counted(self):
        d = self.cap["description"].lower()
        self.assertIn("total", d)
        self.assertTrue(
            any(w in d for w in ("including", "includes", "counting", "counts")),
            "cap description must say the owning account is counted in the total",
        )

    def test_cap_description_gives_the_arithmetic(self):
        self.assertIn(
            "cap-1", self.cap["description"],
            "state the consequence explicitly: the link admits cap-1 further agents",
        )

    def test_cap_has_examples(self):
        self.assertTrue(self.cap.get("examples"), "cap should carry concrete examples")

    def test_room_create_description_does_not_repeat_the_false_claim(self):
        # The exact wording that was wrong by one.
        self.assertNotIn(
            "link admits up to cap agents", self.tool["description"],
            "this phrasing is false: the link admits cap-1 agents, not cap",
        )

    def test_room_create_warns_that_an_agent_key_must_join_first(self):
        d = self.tool["description"]
        self.assertIn("room_join", d)
        self.assertIn(
            "room_not_found", d,
            "an agk_ key that skips room_join gets room_not_found for a room it "
            "just created; that surprise must be documented where it is read",
        )
