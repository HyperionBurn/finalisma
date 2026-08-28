"""Accessibility regressions for the public landing page (MPAI-72)."""

from __future__ import annotations

import re
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parent.parent
INDEX_ASTRO = ROOT / "web" / "src" / "pages" / "index.astro"
ROOM_TSX = ROOT / "web" / "src" / "components" / "land" / "Room.tsx"
LAND_CSS = ROOT / "web" / "src" / "styles" / "land.css"
VERIFY_PRESERVATION = ROOT / "web" / "scripts" / "verify-preservation.cjs"


class TestLandingAccessibility(unittest.TestCase):
    """Keep the landing page's dynamic showcase usable without visual motion."""

    def test_marquee_hides_visual_clones_and_exposes_hosts_once(self) -> None:
        source = INDEX_ASTRO.read_text(encoding="utf-8")
        self.assertRegex(
            source,
            r'<div class="mq__row"[^>]*aria-hidden="true"',
            "the animated marquee row must be decorative for assistive technology",
        )
        self.assertIn(
            '<span class="sr">Claude Code, Codex, Cursor, Windsurf, OpenCode, Zed, Cline, and VS Code.</span>',
            source,
            "the host compatibility claim needs one stable, non-duplicated accessible copy",
        )

    def test_room_has_static_transcript_and_no_js_preview_notice(self) -> None:
        index = INDEX_ASTRO.read_text(encoding="utf-8")
        room = ROOM_TSX.read_text(encoding="utf-8")
        self.assertRegex(
            index,
            r'<noscript>\s*<p class="room__nojs">[^<]*static preview without JavaScript',
            "scriptless visitors need to know the room is a preview and where its transcript is",
        )
        self.assertIn(
            '<ol className="room__transcript" aria-label="Room demonstration transcript">',
            room,
        )
        self.assertRegex(
            room,
            r'<ol className="room__transcript"[^>]*>\s*\{BEATS\.map\(\(b, n\) =>',
            "the transcript must be generated from the same beats as the visual walkthrough",
        )
        self.assertRegex(
            room,
            r'<p key=\{i\} role="status" aria-live="polite" aria-atomic="true">',
            "the currently selected beat should be announced as the reader scrubs",
        )

    def test_caption_animation_is_gated_by_motion_preference(self) -> None:
        css = LAND_CSS.read_text(encoding="utf-8")
        marker = "@media (prefers-reduced-motion: no-preference)"
        self.assertIn(marker, css)
        before_motion_media, after_motion_media = css.split(marker, 1)
        self.assertNotIn(
            "animation:capIn",
            before_motion_media,
            "the caption must not animate in the default/reduced-motion styles",
        )
        self.assertRegex(
            after_motion_media,
            r"\.room__caption p\{[^}]*animation:capIn",
            "the caption animation belongs inside the no-preference media query",
        )

    def test_skip_link_becomes_visible_when_focused(self) -> None:
        css = LAND_CSS.read_text(encoding="utf-8")
        match = re.search(r"\.sr:focus-visible\s*\{(?P<body>[^}]*)\}", css)
        self.assertIsNotNone(match, "the clipped skip link needs a focused presentation")
        body = match.group("body")
        for declaration in ("position:fixed", "width:auto", "height:auto", "overflow:visible", "clip:auto"):
            self.assertIn(declaration, body)

    def test_primary_nav_offers_returning_users_a_direct_login_route(self) -> None:
        source = INDEX_ASTRO.read_text(encoding="utf-8")
        nav = re.search(r'<nav class="links" aria-label="Primary">(?P<body>.*?)</nav>', source, re.S)
        self.assertIsNotNone(nav, "the landing page needs its primary navigation")
        self.assertIn("APP_LOGIN_URL", source)
        self.assertRegex(nav.group("body"), r'<a href=\{APP_LOGIN_URL\}>Log in</a>')

    def test_signup_ctas_name_the_account_creation_action(self) -> None:
        source = INDEX_ASTRO.read_text(encoding="utf-8")
        ctas = re.findall(r'<a\b[^>]*href=\{APP_SIGNUP_URL\}[^>]*>(.*?)</a>', source, re.S)
        self.assertEqual(len(ctas), 4, "all four signup destinations should remain available")
        for cta in ctas:
            visible_text = re.sub(r"<[^>]+>", "", cta)
            self.assertIn("Create a free account", visible_text)
            self.assertNotIn("Open a room", visible_text)

        guard = VERIFY_PRESERVATION.read_text(encoding="utf-8")
        self.assertIn("'Create a free account'", guard)
        self.assertNotIn("'Open a room'", guard)

    def test_hero_video_waits_for_engagement_after_poster_paint(self) -> None:
        source = INDEX_ASTRO.read_text(encoding="utf-8")
        video = re.search(r'<video class="plate-video"(?P<body>.*?)</video>', source, re.S)
        self.assertIsNotNone(video, "the landing hero needs its poster-backed video element")
        body = video.group("body")
        self.assertIn('preload="none"', body)
        self.assertIn('poster={VIDEO_POSTER}', body)
        self.assertIn('data-video-src={VIDEO}', body)
        self.assertNotIn('<source src={VIDEO}', body)
        self.assertIn("pointerdown", source)
        self.assertIn("scroll", source)
        self.assertIn("v.load()", source)

    def test_hero_poster_preload_replaces_dead_video_hint(self) -> None:
        source = INDEX_ASTRO.read_text(encoding="utf-8")
        self.assertIn(
            '<link rel="preload" as="image" href={VIDEO_POSTER} />',
            source,
            "the poster is the first-view LCP candidate and needs an explicit preload",
        )
        self.assertNotIn(
            '<link rel="preload" as="video">',
            source,
            "an empty video preload hint is dead markup and risks restoring eager video fetches",
        )


if __name__ == "__main__":
    unittest.main()
