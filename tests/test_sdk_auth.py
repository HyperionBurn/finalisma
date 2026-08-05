"""SDK auth-injection contract tests — TDD RED for Wave SDK-ROOMS.

Spawns a real coordinator with --actor-auth auto over HTTP and asserts the
systematic auth-injection contract every public SDK method must satisfy:

  * join_pairing() MUST inject actor_token (currently RED: token dropped).
  * Every public room method MUST refuse a wrong/expired actor_token.
  * Cross-room isolation: member of A refused on B.
  * Room cap / revoked link refusals.
  * session_* methods MUST work WITHOUT actor_token (session_token is auth).

Run:  python -B -m unittest tests.test_sdk_auth -v
Expected: FAIL (join_pairing drops token; room helpers don't exist yet).
"""

from __future__ import annotations

import atexit
import json
import secrets
import socket
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from finalisma_sdk import FinalismaClient, FinalismaError


def _pick_free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _wait_tcp(host: str, port: int, timeout_s: float = 15.0) -> None:
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        try:
            with socket.create_connection((host, port), timeout=0.5):
                return
        except OSError:
            time.sleep(0.1)
    raise RuntimeError(f"coordinator did not accept TCP on {host}:{port}")


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _spawn_coordinator(port: int, workspace: Path, actor_auth: str = "auto") -> subprocess.Popen:
    """Spawn the coordinator over HTTP with the given actor-auth mode."""
    return subprocess.Popen([
            sys.executable,
            "-B",
            "scripts/finalisma-mcp.py",
            "--transport",
            "http",
            "--host",
            "127.0.0.1",
            "--port",
            str(port),
            "--team-id",
            "demo",
            "--workspace",
            str(workspace),
            "--state",
            str(workspace / ".finalisma" / "state.db"),
            "--actor-auth",
            actor_auth,
        ],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        cwd=str(PROJECT_ROOT),
        encoding="utf-8",
        errors="replace",
    )


def _register(base_url: str, name: str, team: str = "demo") -> tuple[FinalismaClient, str]:
    """Register an agent, return (client bound to that token, token)."""
    bootstrap = FinalismaClient(base_url, name, team)
    reg = bootstrap.register(name=name, role="generalist")
    token = reg["actor_token"]
    bootstrap.close()
    client = FinalismaClient(base_url, name, team, actor_token=token)
    return client, token


# ---------------------------------------------------------------------------
# Test suite
# ---------------------------------------------------------------------------

class SdkAuthInjectionContract(unittest.TestCase):
    """The systematic auth-injection contract. Every test here is RED until
    Wave SDK-ROOMS implements the room helpers + fixes join_pairing."""

    proc: subprocess.Popen | None = None
    scratch: tempfile.TemporaryDirectory | None = None
    workspace: Path | None = None
    base_url: str = ""
    port: int = 0

    # shared fixtures
    owner: FinalismaClient | None = None
    owner_token: str = ""
    room_id: str = ""
    link_token: str = ""
    members: dict[str, FinalismaClient] = {}
    member_tokens: dict[str, str] = {}

    @classmethod
    def setUpClass(cls) -> None:
        cls.scratch = tempfile.TemporaryDirectory(prefix="finalisma-sdk-auth-")
        cls.workspace = Path(cls.scratch.name)
        cls.port = _pick_free_port()
        cls.base_url = f"http://127.0.0.1:{cls.port}/mcp"
        cls.proc = _spawn_coordinator(cls.port, cls.workspace, actor_auth="auto")
        _wait_tcp("127.0.0.1", cls.port)

        # Owner registers + creates a room (cap 4).
        cls.owner, cls.owner_token = _register(cls.base_url, "owner")
        room = cls.owner._call(
            "finalisma_room_create",
            owner_agent_id="owner", cap=4, name="auth-contract",
        )
        cls.room_id = room["room_id"]
        cls.link_token = room["link_token"]

        # Two members join via the low-level call (which injects actor_token).
        for name in ["m1", "m2"]:
            c, t = _register(cls.base_url, name)
            cls.members[name] = c
            cls.member_tokens[name] = t
            c._call(
                "finalisma_room_join",
                room_id=cls.room_id,
                link_token=cls.link_token,
                agent_id=name,
                consent=True,
                capabilities=["read"],
            )

    @classmethod
    def tearDownClass(cls) -> None:
        for c in list(cls.members.values()) + [cls.owner]:
            if c:
                c.close()
        if cls.proc is not None:
            cls.proc.terminate()
            try:
                cls.proc.wait(timeout=10)
            except subprocess.TimeoutExpired:
                cls.proc.kill()
                cls.proc.wait(timeout=10)
        if cls.scratch is not None:
            cls.scratch.cleanup()

    # ---- 1. join_pairing injects actor_token (the original bug) -----------

    def test_01_join_pairing_injects_actor_token(self) -> None:
        """join_pairing() must inject the client's actor_token so the join
        succeeds on a coordinator with --actor-auth auto."""
        # Fresh agent registers, gets a token, builds a client with it.
        joiner, joiner_token = _register(self.base_url, "joiner")
        try:
            # Owner creates a pairing link.
            pairing = self.owner.create_pairing_link(capabilities=["read"])
            self.assertIsNotNone(pairing.join_token)

            # The PUBLIC helper must succeed — it must inject actor_token.
            jr = joiner.join_pairing(pairing.join_url, consent=True)
            self.assertIn(jr.state, ("active", "open"))
            self.assertIn("joiner", jr.members)
        finally:
            joiner.close()

    # ---- 2. Systematic negative: wrong actor_token refused ----------------

    def _assert_refused(self, callable_, expected_codes: set[str]) -> None:
        try:
            callable_()
        except FinalismaError as e:
            self.assertIn(
                e.code, expected_codes,
                f"expected one of {expected_codes}, got {e.code}: {e.message}",
            )
        else:
            self.fail(f"expected refusal in {expected_codes}, call succeeded")

    def test_02_create_room_wrong_token_refused(self) -> None:
        bogus = FinalismaClient(
            self.base_url, "owner", "demo",
            actor_token="rm_" + secrets.token_urlsafe(32),
        )
        try:
            self._assert_refused(
                lambda: bogus.create_room(cap=4, name="x"),
                {"actor_auth_invalid", "actor_auth_required"},
            )
        finally:
            bogus.close()

    def test_03_join_room_wrong_token_refused(self) -> None:
        bogus = FinalismaClient(
            self.base_url, "m1", "demo",
            actor_token="rm_" + secrets.token_urlsafe(32),
        )
        try:
            self._assert_refused(
                lambda: bogus.join_room(
                    room_id=self.room_id, link_token=self.link_token,
                    agent_id="m1", consent=True,
                ),
                {"actor_auth_invalid", "actor_auth_required"},
            )
        finally:
            bogus.close()

    def test_04_room_info_wrong_token_refused(self) -> None:
        bogus = FinalismaClient(
            self.base_url, "m1", "demo",
            actor_token="rm_" + secrets.token_urlsafe(32),
        )
        try:
            self._assert_refused(
                lambda: bogus.room_info(room_id=self.room_id, agent_id="m1"),
                {"actor_auth_invalid", "actor_auth_required"},
            )
        finally:
            bogus.close()

    def test_05_room_poll_wrong_token_refused(self) -> None:
        bogus = FinalismaClient(
            self.base_url, "m1", "demo",
            actor_token="rm_" + secrets.token_urlsafe(32),
        )
        try:
            self._assert_refused(
                lambda: bogus.room_poll(room_id=self.room_id, agent_id="m1"),
                {"actor_auth_invalid", "actor_auth_required"},
            )
        finally:
            bogus.close()

    def test_06_room_ack_wrong_token_refused(self) -> None:
        bogus = FinalismaClient(
            self.base_url, "m1", "demo",
            actor_token="rm_" + secrets.token_urlsafe(32),
        )
        try:
            self._assert_refused(
                lambda: bogus.room_ack(room_id=self.room_id, agent_id="m1", seq=0),
                {"actor_auth_invalid", "actor_auth_required"},
            )
        finally:
            bogus.close()

    def test_07_room_send_wrong_token_refused(self) -> None:
        bogus = FinalismaClient(
            self.base_url, "m1", "demo",
            actor_token="rm_" + secrets.token_urlsafe(32),
        )
        try:
            self._assert_refused(
                lambda: bogus.send(
                    room_id=self.room_id, sender_agent_id="m1",
                    target_spec="m2", payload={"text": "x"},
                ),
                {"actor_auth_invalid", "actor_auth_required"},
            )
        finally:
            bogus.close()

    def test_08_room_heartbeat_wrong_token_refused(self) -> None:
        bogus = FinalismaClient(
            self.base_url, "m1", "demo",
            actor_token="rm_" + secrets.token_urlsafe(32),
        )
        try:
            self._assert_refused(
                lambda: bogus.room_heartbeat(room_id=self.room_id, agent_id="m1"),
                {"actor_auth_invalid", "actor_auth_required"},
            )
        finally:
            bogus.close()

    def test_09_group_members_wrong_token_refused(self) -> None:
        bogus = FinalismaClient(
            self.base_url, "m1", "demo",
            actor_token="rm_" + secrets.token_urlsafe(32),
        )
        try:
            self._assert_refused(
                lambda: bogus.group_members(
                    room_id=self.room_id, agent_id="m1",
                    group_name="g", action="list",
                ),
                {"actor_auth_invalid", "actor_auth_required"},
            )
        finally:
            bogus.close()

    def test_10_add_to_group_wrong_token_refused(self) -> None:
        bogus = FinalismaClient(
            self.base_url, "owner", "demo",
            actor_token="rm_" + secrets.token_urlsafe(32),
        )
        try:
            self._assert_refused(
                lambda: bogus.add_to_group(
                    room_id=self.room_id, agent_id="owner",
                    group_name="g", members=["m1"],
                ),
                {"actor_auth_invalid", "actor_auth_required"},
            )
        finally:
            bogus.close()

    def test_11_room_receipts_wrong_token_refused(self) -> None:
        bogus = FinalismaClient(
            self.base_url, "m1", "demo",
            actor_token="rm_" + secrets.token_urlsafe(32),
        )
        try:
            self._assert_refused(
                lambda: bogus.room_receipts(
                    room_id=self.room_id, agent_id="m1", entry_ids=[],
                ),
                {"actor_auth_invalid", "actor_auth_required"},
            )
        finally:
            bogus.close()

    def test_12_leave_room_wrong_token_refused(self) -> None:
        bogus = FinalismaClient(
            self.base_url, "m1", "demo",
            actor_token="rm_" + secrets.token_urlsafe(32),
        )
        try:
            self._assert_refused(
                lambda: bogus.leave_room(room_id=self.room_id, agent_id="m1"),
                {"actor_auth_invalid", "actor_auth_required"},
            )
        finally:
            bogus.close()

    def test_13_close_room_wrong_token_refused(self) -> None:
        bogus = FinalismaClient(
            self.base_url, "owner", "demo",
            actor_token="rm_" + secrets.token_urlsafe(32),
        )
        try:
            self._assert_refused(
                lambda: bogus.close_room(room_id=self.room_id, owner_agent_id="owner"),
                {"actor_auth_invalid", "actor_auth_required"},
            )
        finally:
            bogus.close()

    def test_14_revoke_link_wrong_token_refused(self) -> None:
        bogus = FinalismaClient(
            self.base_url, "owner", "demo",
            actor_token="rm_" + secrets.token_urlsafe(32),
        )
        try:
            self._assert_refused(
                lambda: bogus.revoke_link(
                    room_id=self.room_id, owner_agent_id="owner", link_id="x",
                ),
                {"actor_auth_invalid", "actor_auth_required"},
            )
        finally:
            bogus.close()

    # ---- 2b. Rotated (expired) token refused --------------------------------

    def test_15_rotated_token_refused(self) -> None:
        """After credential rotation, the OLD token must be refused."""
        victim, old_token = _register(self.base_url, "victim")
        try:
            # Join the room with the good token via low-level call.
            victim._call(
                "finalisma_room_join",
                room_id=self.room_id, link_token=self.link_token,
                agent_id="victim", consent=True,
            )
            # Rotate — SDK updates victim._actor_token internally.
            rotated = victim.rotate_credential()
            self.assertTrue(rotated.actor_token)
            self.assertNotEqual(rotated.actor_token, old_token)

            # A client bound to the OLD token must be refused.
            stale = FinalismaClient(
                self.base_url, "victim", "demo", actor_token=old_token,
            )
            try:
                self._assert_refused(
                    lambda: stale.room_info(room_id=self.room_id, agent_id="victim"),
                    {"actor_auth_invalid", "actor_auth_required"},
                )
            finally:
                stale.close()
        finally:
            victim.close()

    # ---- 3. Cross-room isolation -------------------------------------------

    def test_16_cross_room_isolation(self) -> None:
        """A member of room A calling room_info/poll/send on room B's id
        is refused (room_not_found or member_required)."""
        other = self.owner._call(
            "finalisma_room_create",
            owner_agent_id="owner", cap=4, name="other",
        )
        other_id = other["room_id"]
        # m1 is in self.room_id but NOT in other_id.
        self._assert_refused(
            lambda: self.members["m1"].room_info(room_id=other_id, agent_id="m1"),
            {"room_not_found", "member_required"},
        )
        self._assert_refused(
            lambda: self.members["m1"].room_poll(room_id=other_id, agent_id="m1"),
            {"room_not_found", "member_required"},
        )
        self._assert_refused(
            lambda: self.members["m1"].send(
                room_id=other_id, sender_agent_id="m1",
                target_spec="*", payload={"text": "x"},
            ),
            {"room_not_found", "member_required"},
        )

    # ---- 4. Room cap -------------------------------------------------------

    def test_17_room_cap_enforced(self) -> None:
        """Joining past the cap is refused (room_full)."""
        # Cap must be >= 2 per coordinator validation. Use cap=2: owner
        # auto-joins (1), m1 joins (2) → full. The 3rd join attempt is refused.
        tiny = self.owner._call(
            "finalisma_room_create",
            owner_agent_id="owner", cap=2, name="tiny",
        )
        tiny_id = tiny["room_id"]
        tiny_link = tiny["link_token"]

        # Fill the room: m1 joins (2nd member → cap reached).
        self.members["m1"]._call(
            "finalisma_room_join",
            room_id=tiny_id, link_token=tiny_link,
            agent_id="m1", consent=True,
        )

        capper, _ = _register(self.base_url, "capper")
        try:
            self._assert_refused(
                lambda: capper.join_room(
                    room_id=tiny_id, link_token=tiny_link,
                    agent_id="capper", consent=True,
                ),
                {"room_full"},
            )
        finally:
            capper.close()

    # ---- 5. Revoked link ----------------------------------------------------

    def test_18_revoked_link_cannot_join(self) -> None:
        """A revoked link is refused (link_revoked)."""
        # Create a room, revoke its link, then attempt to join.
        rl = self.owner._call(
            "finalisma_room_create",
            owner_agent_id="owner", cap=4, name="rl",
        )
        rl_id = rl["room_id"]
        # Revoke the room's PRIMARY link (a distinct link_* id returned by create).
        self.owner._call(
            "finalisma_room_revoke_link",
            room_id=rl_id, owner_agent_id="owner", link_id=rl["link_id"],
        )
        joiner, _ = _register(self.base_url, "rl-joiner")
        try:
            self._assert_refused(
                lambda: joiner.join_room(
                    room_id=rl_id, link_token=rl["link_token"],
                    agent_id="rl-joiner", consent=True,
                ),
                {"link_revoked", "pairing_not_found", "pairing_unavailable"},
            )
        finally:
            joiner.close()

    # ---- 6. session_* work WITHOUT actor_token ------------------------------

    def test_19_session_methods_work_without_actor_token(self) -> None:
        """session_send/poll/ack authenticate via session_token, NOT actor_token.
        A client with NO actor_token must still operate on its session. This
        guards against an over-eager 'fix' that injects actor_token into the
        session path (the coordinator's session schemas have no such field)."""
        # Build a pairing + join to get a session_token.
        a, a_token = _register(self.base_url, "sess-a")
        b, b_token = _register(self.base_url, "sess-b")
        try:
            pairing = a.create_pairing_link(capabilities=["read"])
            jr = b.join_pairing(pairing.join_url, consent=True)
            session_token = jr.session_token
            self.assertTrue(session_token)

            # Client with NO actor_token — must still send/poll/ack.
            bare = FinalismaClient(self.base_url, "sess-b", "demo")
            try:
                evt = bare.session_send(
                    session_token=session_token, kind="test",
                    payload={"hello": "world"},
                )
                self.assertEqual(evt.type, "test")

                events = bare.session_poll(
                    session_token=session_token, after_seq=0,
                )
                self.assertTrue(any(e.event_id == evt.event_id for e in events))

                acked = bare.session_ack(
                    session_token=session_token, seq=evt.seq,
                )
                self.assertGreaterEqual(acked, evt.seq)
            finally:
                bare.close()
        finally:
            a.close()
            b.close()


def _kill_orphaned_coordinator() -> None:
    """atexit safety: if setUpClass raised before tearDownClass, terminate the
    spawned coordinator so it is never orphaned (it would hold the tempdir
    and its port indefinitely)."""
    if SdkAuthInjectionContract.proc is not None and SdkAuthInjectionContract.proc.poll() is None:
        try:
            SdkAuthInjectionContract.proc.terminate()
            SdkAuthInjectionContract.proc.wait(timeout=5)
        except Exception:
            try:
                SdkAuthInjectionContract.proc.kill()
            except Exception:
                pass
        SdkAuthInjectionContract.proc = None


atexit.register(_kill_orphaned_coordinator)


if __name__ == "__main__":
    unittest.main()
