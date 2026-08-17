"""Stage-2 email delivery — SMTP mailer, selection, and the outbox drain worker.

RED deliverable: outbound email must actually become deliverable. Today every
flow (signup verification, password reset, org invite) writes a durable row to
``cloud_identity_outbox`` and stops — the Stage-2 drain worker the design
docstring promised was never built, so a forgotten password is a permanent
lockout. This file pins the Stage-2 contract:

  1. No SMTP env → the selected mailer is ``LocalOutboxMailer`` and behaviour
     is byte-for-byte unchanged (existing suite keeps passing untouched).
  2. SMTP env present → ``SmtpMailer`` is selected with the parsed
     host/port/credentials/from-address, and sends over STARTTLS + AUTH.
     Tests use an in-memory SMTP double, never a real connection.
  3. The drain worker sends an undelivered row exactly once; a second run
     sends nothing; concurrent drains cannot double-send (conditional claim
     inside one transaction).
  4. Transient failures retry with backoff; permanent ones reach a terminal
     ``failed`` state and stop.
  5. No log line may contain a reset token or an email body.

Authoritative spec: docs/IDENTITY_DESIGN.md section 5 (Stage 2) and the
sibling design note in this wave's email-delivery doc.
"""

from __future__ import annotations

import contextlib
import io
import logging
import re
import smtplib
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from weft_cloud.identity import outbox_worker
from weft_cloud.identity.mailer import (  # noqa: E402
    LocalOutboxMailer,
    SmtpMailer,
    build_mailer,
    smtp_config_from_env,
)
from weft_cloud.migrations import apply_migrations  # noqa: E402
from weft_cloud.storage import SqliteWalBackend  # noqa: E402

SMTP_ENV = {
    "WEFT_SMTP_HOST": "smtp.example.com",
    "WEFT_SMTP_PORT": "587",
    "WEFT_SMTP_USERNAME": "smtp-user",
    "WEFT_SMTP_PASSWORD": "smtp-pass",
    "WEFT_SMTP_FROM": "no-reply@example.com",
}


class FakeSMTP:
    """In-memory SMTP double. Never touches the network.

    Records the connection args, STARTTLS/AUTH calls, and every message sent.
    ``script`` holds the sequence of exceptions to raise (``None`` = succeed),
    consumed one per ``send_message`` call — so a test can script a transient
    failure followed by success.
    """

    instances: list["FakeSMTP"] = []
    script: list = []

    def __init__(self, host: str, port: int, *, timeout: float = 15.0) -> None:
        self.host = host
        self.port = port
        self.timeout = timeout
        self.starttls_called = False
        self.logged_in: tuple | None = None
        self.sent: list = []
        FakeSMTP.instances.append(self)

    def __enter__(self) -> "FakeSMTP":
        return self

    def __exit__(self, *exc) -> bool:  # noqa: ANN002
        return False

    def starttls(self) -> None:
        self.starttls_called = True

    def login(self, username: str, password: str) -> None:
        self.logged_in = (username, password)

    def send_message(self, message) -> None:
        if FakeSMTP.script:
            exc = FakeSMTP.script.pop(0)
            if exc is not None:
                raise exc
        self.sent.append(message)

    @classmethod
    def reset(cls) -> None:
        cls.instances = []
        cls.script = []

    @classmethod
    def total_sent(cls) -> int:
        return sum(len(inst.sent) for inst in cls.instances)


def make_backend() -> SqliteWalBackend:
    tmp = tempfile.TemporaryDirectory(prefix="email-delivery-")
    db_path = Path(tmp.name) / "identity.db"
    backend = SqliteWalBackend(db_path)
    backend.initialize()
    apply_migrations(backend)
    backend._tmpdir = tmp  # type: ignore[attr-defined]
    return backend


def smtp_mailer(**overrides) -> SmtpMailer:
    kwargs = dict(host=SMTP_ENV["WEFT_SMTP_HOST"], port=587,
                  username="smtp-user", password="smtp-pass",
                  from_addr="no-reply@example.com", smtp_factory=FakeSMTP)
    kwargs.update(overrides)
    return SmtpMailer(**kwargs)


class EmailDeliveryTests(unittest.TestCase):
    """SMTP mailer + selection contract (points 1-2)."""

    def setUp(self) -> None:
        FakeSMTP.reset()
        self.backend = make_backend()
        self.tenant_id = "tenant_email_delivery"

    def tearDown(self) -> None:
        try:
            self.backend.close()
        finally:
            tmp = getattr(self.backend, "_tmpdir", None)
            if tmp is not None:
                tmp.cleanup()

    # -- 1. selection defaults to LocalOutboxMailer with no SMTP env --

    def test_no_smtp_env_selects_local_outbox_mailer(self) -> None:
        mailer = build_mailer(self.backend, environ={})
        self.assertIsInstance(mailer, LocalOutboxMailer)

    def test_no_smtp_env_keeps_outbox_byte_for_byte(self) -> None:
        mailer = build_mailer(self.backend, environ={})
        mailer.send(self.tenant_id, "to@example.com", "Verify your email", "Click: fvt_abc123")
        with self.backend.transaction() as tx:
            row = tx.execute(
                "SELECT tenant_id, to_email, subject, body, status, attempts FROM cloud_identity_outbox "
                "WHERE to_email = ?",
                ("to@example.com",),
            ).fetchone()
        self.assertIsNotNone(row)
        self.assertEqual(row["tenant_id"], self.tenant_id)
        self.assertEqual(row["to_email"], "to@example.com")
        self.assertEqual(row["subject"], "Verify your email")
        self.assertEqual(row["body"], "Click: fvt_abc123")
        self.assertEqual(row["status"], "queued")
        self.assertEqual(row["attempts"], 0)

    # -- 2. SMTP env selects SmtpMailer with parsed settings --

    def test_smtp_env_selects_smtp_mailer_with_parsed_settings(self) -> None:
        mailer = build_mailer(self.backend, environ=SMTP_ENV)
        self.assertIsInstance(mailer, SmtpMailer)
        self.assertEqual(mailer.host, "smtp.example.com")
        self.assertEqual(mailer.port, 587)
        self.assertEqual(mailer.username, "smtp-user")
        self.assertEqual(mailer.password, "smtp-pass")
        self.assertEqual(mailer.from_addr, "no-reply@example.com")

    def test_smtp_config_from_env_returns_none_without_host(self) -> None:
        self.assertIsNone(smtp_config_from_env(environ={}))
        self.assertIsNone(smtp_config_from_env(environ={"WEFT_SMTP_PORT": "587"}))

    def test_partial_smtp_env_fails_loudly_naming_variable(self) -> None:
        with self.assertRaises(ValueError) as ctx:
            smtp_config_from_env(environ={"WEFT_SMTP_HOST": "smtp.example.com"})
        self.assertIn("WEFT_SMTP_USERNAME", str(ctx.exception))

    def test_invalid_smtp_port_fails_loudly(self) -> None:
        env = dict(SMTP_ENV, WEFT_SMTP_PORT="not-a-port")
        with self.assertRaises(ValueError) as ctx:
            smtp_config_from_env(environ=env)
        self.assertIn("WEFT_SMTP_PORT", str(ctx.exception))

    # -- 2b. SmtpMailer sends via STARTTLS + AUTH through the double --

    def test_smtp_mailer_sends_via_starttls_and_auth(self) -> None:
        mailer = smtp_mailer()
        mailer.send(self.tenant_id, "to@example.com", "Reset your password", "Click: frt_secret")
        inst = FakeSMTP.instances[-1]
        self.assertEqual(inst.host, "smtp.example.com")
        self.assertEqual(inst.port, 587)
        self.assertTrue(inst.starttls_called)
        self.assertEqual(inst.logged_in, ("smtp-user", "smtp-pass"))
        self.assertEqual(len(inst.sent), 1)
        msg = inst.sent[0]
        self.assertEqual(msg["From"], "no-reply@example.com")
        self.assertEqual(msg["To"], "to@example.com")
        self.assertEqual(msg["Subject"], "Reset your password")
        self.assertIn("frt_secret", msg.get_content())


class OutboxDrainerTests(unittest.TestCase):
    """Drain worker contract (points 3-5)."""

    def setUp(self) -> None:
        FakeSMTP.reset()
        self.backend = make_backend()
        self.tenant_id = "tenant_email_delivery"
        self.backend.create_tenant(self.tenant_id, "Email Delivery")

    def tearDown(self) -> None:
        try:
            self.backend.close()
        finally:
            tmp = getattr(self.backend, "_tmpdir", None)
            if tmp is not None:
                tmp.cleanup()

    def _seed(self, to: str = "to@example.com", body: str = "Click: frt_secret") -> None:
        LocalOutboxMailer(self.backend).send(
            self.tenant_id, to, "Reset your password", body
        )

    def _row(self, entry_id: str | None = None) -> dict:
        with self.backend.transaction() as tx:
            if entry_id is None:
                row = tx.execute("SELECT * FROM cloud_identity_outbox").fetchone()
            else:
                row = tx.execute(
                    "SELECT * FROM cloud_identity_outbox WHERE entry_id = ?", (entry_id,)
                ).fetchone()
        return dict(row)

    def _drainer(self, **kwargs) -> outbox_worker.OutboxDrainer:
        return outbox_worker.OutboxDrainer(
            self.backend, smtp_mailer(), **kwargs
        )

    # -- 3. exactly-once --

    def test_drain_sends_undelivered_row_exactly_once(self) -> None:
        self._seed()
        drainer = self._drainer()
        first = drainer.drain_once()
        self.assertEqual(first["sent"], 1)
        self.assertEqual(first["claimed"], 1)
        row = self._row()
        self.assertEqual(row["status"], "sent")
        self.assertIsNotNone(row["dispatched_at"])

        self.assertEqual(FakeSMTP.total_sent(), 1)
        delivered = FakeSMTP.instances[-1].sent[0]
        self.assertEqual(delivered["To"], "to@example.com")
        self.assertEqual(delivered["Subject"], "Reset your password")
        self.assertIn("frt_secret", delivered.get_content())

        second = drainer.drain_once()
        self.assertEqual(second["claimed"], 0)
        self.assertEqual(second["sent"], 0)
        self.assertEqual(FakeSMTP.total_sent(), 1)

    def test_worker_delivers_with_weft_smtp_env(self) -> None:
        config = smtp_config_from_env(environ=SMTP_ENV)
        self.assertIsNotNone(config)
        mailer = SmtpMailer(**config, smtp_factory=FakeSMTP)
        self._seed()
        drainer = outbox_worker.OutboxDrainer(self.backend, mailer)
        result = drainer.drain_once()
        self.assertEqual(result["sent"], 1)
        delivered = FakeSMTP.instances[-1].sent[0]
        self.assertEqual(delivered["To"], "to@example.com")
        self.assertEqual(delivered["Subject"], "Reset your password")
        self.assertIn("frt_secret", delivered.get_content())
        self.assertEqual(self._row()["status"], "sent")

    def test_second_run_sends_nothing(self) -> None:
        self._seed()
        drainer = self._drainer()
        drainer.drain_once()
        FakeSMTP.reset()
        drainer.drain_once()
        self.assertEqual(FakeSMTP.total_sent(), 0)

    # -- 3b. concurrency: two drainers cannot double-send --

    def test_concurrent_drains_do_not_double_send(self) -> None:
        self._seed()
        drainer_a = self._drainer()
        drainer_b = self._drainer()
        barrier = threading.Barrier(2)

        def run(drainer: outbox_worker.OutboxDrainer) -> dict:
            barrier.wait()
            return drainer.drain_once()

        threads = [threading.Thread(target=run, args=(d,)) for d in (drainer_a, drainer_b)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=30)
        self.assertEqual(FakeSMTP.total_sent(), 1)
        self.assertEqual(self._row()["status"], "sent")

    def test_reclaimed_worker_cannot_finalize_newer_claim(self) -> None:
        self._seed()
        drainer_a = self._drainer(lease_seconds=60)
        drainer_b = self._drainer(lease_seconds=60)
        drainer_a.worker_id = "worker-a"
        drainer_b.worker_id = "worker-b"
        test_case = self
        # This test uses a deterministic synthetic clock to exercise stale
        # finalization; bypass the live heartbeat so worker B can reclaim at
        # the fixed timestamp below. A real-time renewal race is covered by
        # the separate slow-send scenario.
        drainer_a.renew_lease = lambda _entry_id: True

        class ReclaimingMailer:
            def send(self, *_args) -> None:
                test_case.assertEqual(len(drainer_b.claim_due(now=1061)), 1)

        drainer_a.mailer = ReclaimingMailer()
        result = drainer_a.drain_once(now=1000)
        self.assertEqual(result["claimed"], 1)
        self.assertEqual(result["sent"], 0)
        self.assertEqual(result["lost"], 1)

        row = self._row()
        entry_id = row["entry_id"]
        self.assertEqual(row["status"], "claimed")
        self.assertEqual(row["claimed_by"], "worker-b")
        self.assertFalse(drainer_a.mark_sent(entry_id, now=1062))
        self.assertFalse(drainer_a.mark_retry(entry_id, 1, 1122, "stale"))
        self.assertFalse(drainer_a.mark_failed(entry_id, 1, "stale"))
        self.assertTrue(drainer_b.mark_sent(entry_id, now=1063))
        self.assertEqual(self._row()["status"], "sent")

        # A live but slow SMTP send must renew its claim instead of being
        # mistaken for a dead worker and sent a second time.
        slow_entry_id = self._seed(to="slow@example.com")
        started = threading.Event()
        release = threading.Event()
        calls: list[str] = []

        class SlowMailer:
            def send(self, _tenant_id: str, _to: str, _subject: str, body: str) -> None:
                calls.append(body)
                started.set()
                if not release.wait(5):
                    raise RuntimeError("slow send test timed out")

        live = self._drainer(lease_seconds=0.3)
        live.mailer = SlowMailer()
        observer = self._drainer(lease_seconds=0.3)
        result_holder: list[dict] = []
        thread = threading.Thread(target=lambda: result_holder.append(live.drain_once()))
        thread.start()
        self.assertTrue(started.wait(5), "slow send did not start")
        time.sleep(0.8)
        self.assertEqual(observer.claim_due(now=time.time()), [])
        release.set()
        thread.join(timeout=5)
        self.assertFalse(thread.is_alive(), "slow send worker did not finish")
        self.assertEqual(result_holder[0]["sent"], 1)
        self.assertEqual(self._row(slow_entry_id)["status"], "sent")
        self.assertEqual(len(calls), 1)

    # -- 4. retry / terminal semantics --

    def test_transient_failure_retried_with_backoff_then_succeeds(self) -> None:
        FakeSMTP.script = [smtplib.SMTPServerDisconnected("temporary")]
        self._seed()
        drainer = self._drainer(backoff_seconds=60)
        now = time.time()
        first = drainer.drain_once(now=now)
        self.assertEqual(first["retried"], 1)
        self.assertEqual(first["sent"], 0)
        row = self._row()
        self.assertEqual(row["status"], "queued")
        self.assertEqual(row["attempts"], 1)
        self.assertGreater(row["next_attempt_at"], now)
        self.assertIsNone(row["claimed_by"])

        retry = drainer.drain_once(now=row["next_attempt_at"] + 1)
        self.assertEqual(retry["sent"], 1)
        self.assertEqual(self._row()["status"], "sent")

    def test_permanent_failure_reaches_terminal_state_and_stops(self) -> None:
        FakeSMTP.script = [smtplib.SMTPRecipientsRefused({})]
        self._seed()
        drainer = self._drainer()
        first = drainer.drain_once()
        self.assertEqual(first["failed"], 1)
        row = self._row()
        self.assertEqual(row["status"], "failed")
        self.assertGreaterEqual(row["attempts"], 1)

        drainer.drain_once()
        self.assertEqual(FakeSMTP.total_sent(), 0)

    def test_max_attempts_on_transient_failures_reaches_terminal_state(self) -> None:
        FakeSMTP.script = [smtplib.SMTPServerDisconnected("t")] * 3
        self._seed()
        drainer = self._drainer(backoff_seconds=0, max_attempts=2)
        now = time.time()
        first = drainer.drain_once(now=now)
        self.assertEqual(first["retried"], 1)
        row = self._row()
        self.assertEqual(row["attempts"], 1)
        second = drainer.drain_once(now=row["next_attempt_at"] + 1)
        self.assertEqual(second["failed"], 1)
        self.assertEqual(self._row()["status"], "failed")

    # -- 5. no token or body ever in logs --

    def test_no_log_line_contains_reset_token_or_body(self) -> None:
        secret = "frt_secret_reset_token_xyz"
        body = "Reset your password: " + secret
        self._seed(body=body)
        drainer = self._drainer()
        with self.assertLogs(level=logging.DEBUG) as logs:
            drainer.drain_once()
        combined = "\n".join(logs.output)
        self.assertNotIn(secret, combined)
        self.assertNotIn(body, combined)
        self.assertNotIn("Reset your password", combined)


class OutboxWorkerConfigTests(unittest.TestCase):
    """Drain-worker runtime config + runnable guard."""

    def setUp(self) -> None:
        FakeSMTP.reset()

    def test_runtime_config_defaults(self) -> None:
        cfg = outbox_worker.runtime_config(argv=[], environ={})
        # The drain worker must default to the SAME file the service writes to,
        # not a stale finalisma-named path that is never opened by anything.
        self.assertEqual(cfg["db_path"], "./data/weft-cloud.db")
        self.assertEqual(cfg["interval"], 5)
        self.assertEqual(cfg["batch_size"], 50)
        self.assertEqual(cfg["max_attempts"], 5)

    def test_runtime_config_weft_db_path_wins(self) -> None:
        cfg = outbox_worker.runtime_config(argv=[], environ={"WEFT_DB_PATH": "/data/cloud.db"})
        self.assertEqual(cfg["db_path"], "/data/cloud.db")

    def test_worker_and_service_resolve_same_db_path(self) -> None:
        from weft_cloud.service import runtime_config as service_runtime_config
        for env in ({}, {"WEFT_DB_PATH": "/data/cloud.db"}):
            worker = outbox_worker.runtime_config(argv=[], environ=dict(env))
            service = service_runtime_config(argv=[], environ=dict(env))
            self.assertEqual(worker["db_path"], service["db_path"], env)

    def test_runtime_config_argv_db_path_overrides_env(self) -> None:
        cfg = outbox_worker.runtime_config(argv=["/tmp/argv.db"], environ={
            "WEFT_DB_PATH": "/data/cloud.db",
        })
        self.assertEqual(cfg["db_path"], "/tmp/argv.db")

    def test_runtime_config_env_overrides(self) -> None:
        cfg = outbox_worker.runtime_config(argv=[], environ={
            "WEFT_DB_PATH": "/data/cloud.db",
            "WEFT_DRAIN_INTERVAL": "1",
            "WEFT_DRAIN_BATCH": "10",
            "WEFT_DRAIN_MAX_ATTEMPTS": "3",
            "WEFT_DRAIN_BACKOFF": "30",
        })
        self.assertEqual(cfg["db_path"], "/data/cloud.db")
        self.assertEqual(cfg["interval"], 1)
        self.assertEqual(cfg["batch_size"], 10)
        self.assertEqual(cfg["max_attempts"], 3)
        self.assertEqual(cfg["backoff_seconds"], 30)

    def test_runtime_config_invalid_interval_raises(self) -> None:
        with self.assertRaises(ValueError) as ctx:
            outbox_worker.runtime_config(argv=[], environ={"WEFT_DRAIN_INTERVAL": "abc"})
        self.assertIn("WEFT_DRAIN_INTERVAL", str(ctx.exception))

    def test_worker_without_smtp_config_exits_cleanly_and_sends_nothing(self) -> None:
        tmp = tempfile.TemporaryDirectory(prefix="email-worker-")
        db_path = str(Path(tmp.name) / "cloud.db")
        try:
            backend = SqliteWalBackend(db_path)
            backend.initialize()
            apply_migrations(backend)
            LocalOutboxMailer(backend).send(
                "tenant_x", "to@example.com", "Reset your password", "Click: frt_secret"
            )
            backend.close()

            stderr = io.StringIO()
            with contextlib.redirect_stderr(stderr):
                code = outbox_worker.main(
                    argv=["--once"],
                    environ={"WEFT_DB_PATH": db_path},
                )
            self.assertEqual(code, 0)
            self.assertIn("WEFT_SMTP_HOST", stderr.getvalue())

            backend = SqliteWalBackend(db_path)
            backend.initialize()
            with backend.transaction() as tx:
                row = tx.execute(
                    "SELECT status, attempts FROM cloud_identity_outbox"
                ).fetchone()
            self.assertEqual(row["status"], "queued")
            self.assertEqual(row["attempts"], 0)
            backend.close()
        finally:
            tmp.cleanup()
        self.assertEqual(FakeSMTP.total_sent(), 0)

    def test_migration_adds_outbox_delivery_columns(self) -> None:
        backend = make_backend()
        try:
            with backend.transaction() as tx:
                names = {
                    r["name"] for r in
                    tx.execute("PRAGMA table_info(cloud_identity_outbox)").fetchall()
                }
            for column in ("status", "attempts", "next_attempt_at",
                           "claimed_at", "claimed_by", "last_error"):
                self.assertIn(column, names)
            apply_migrations(backend)
            apply_migrations(backend)
        finally:
            backend.close()
            tmp = getattr(backend, "_tmpdir", None)
            if tmp is not None:
                tmp.cleanup()


class EnvNameGuardTests(unittest.TestCase):
    """Regression guard: the ``FINALISMA_`` rename must never reappear.

    The email subsystem shipped under the pre-rebrand ``FINALISMA_*`` env-var
    names while every other component used ``WEFT_*``. That divergence is what
    made a documented, correctly-configured deploy silently deliver nothing
    (the worker defaulted to a different database file and reported success).
    Any ``FINALISMA_``-prefixed env var name anywhere in ``src/`` is that same
    defect returning — fail loudly instead of letting it ship again.
    """

    def test_no_finalisma_env_var_names_in_src(self) -> None:
        src = Path(__file__).resolve().parents[1] / "src"
        offenders: list[str] = []
        for path in sorted(src.rglob("*")):
            if not path.is_file():
                continue
            try:
                text = path.read_text(encoding="utf-8")
            except (OSError, UnicodeDecodeError):
                continue
            for match in re.finditer(r"FINALISMA_[A-Z0-9_]+", text):
                offenders.append(
                    f"{path.relative_to(src.parent)}:{match.start()}:{match.group(0)}"
                )
        self.assertEqual(
            offenders,
            [],
            "stale FINALISMA_ env var name(s) reappeared in src/: " + "; ".join(offenders),
        )


if __name__ == "__main__":
    unittest.main()
