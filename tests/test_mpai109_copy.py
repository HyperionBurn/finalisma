from __future__ import annotations

from pathlib import Path
import unittest


ROOT = Path(__file__).resolve().parents[1]


class MPAI109CopyTests(unittest.TestCase):
    def test_connect_page_is_honest_about_one_time_keys(self) -> None:
        source = (ROOT / "web/src/components/app/ConnectPicker.tsx").read_text(
            encoding="utf-8"
        )
        self.assertIn("Existing agent keys cannot be retrieved", source)
        self.assertIn('href="/app/keys"', source)
        self.assertNotIn("agk_… (create a key below)", source)

    def test_signup_copy_uses_the_nav_label(self) -> None:
        auth_form = (ROOT / "web/src/components/app/AuthForm.tsx").read_text(
            encoding="utf-8"
        )
        signup_page = (ROOT / "web/src/pages/signup.astro").read_text(
            encoding="utf-8"
        )
        expected = 'Already have an account? <a href="/login">Log in</a>'
        self.assertIn(expected, auth_form)
        self.assertIn(expected, signup_page)
        self.assertIn(
            "Sign in to continue to where you were headed.",
            auth_form,
        )
        self.assertIn(
            "Your session expired. Sign in again to return to where you were.",
            auth_form,
        )
        self.assertIn(
            "Sign in to continue to the room you were invited to.",
            auth_form,
        )

    def test_bare_next_login_copy_makes_no_session_claim(self) -> None:
        auth_form = (ROOT / "web/src/components/app/AuthForm.tsx").read_text(
            encoding="utf-8"
        )
        self.assertNotIn(
            "Your session ended. Sign in to return to where you left off.",
            auth_form,
        )
        self.assertIn("Sign in to continue to where you were headed.", auth_form)

    def test_join_page_uses_weft_brand_and_keeps_link_in_technical_section(self) -> None:
        source = (ROOT / "src/weft_cloud/service.py").read_text(encoding="utf-8")
        self.assertIn(">Weft</span>", source)
        self.assertNotIn("Multiplayer AI Room", source)
        self.assertNotIn(
            "This link opens a Weft room. Give it to the agent you want to",
            source,
        )


if __name__ == "__main__":
    unittest.main()
