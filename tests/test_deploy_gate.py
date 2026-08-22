"""Tests for the deploy gate's own trustworthiness.

Covers the locally-testable core of three of the four real incidents in the
ops brief:

- FAILURE 3: silence must never be a verdict (scripts/classify_suite_log.py).
- FAILURE 2: a CRLF that reaches bash on the VM aborts the cutover
  (scripts/normalize_line_endings.py, plus a real bash -n reproduction).
- FAILURE 1: a restart must be PROVEN (MainPID before/after), never assumed
  from a zero exit code (scripts/restart_proof.py).

Plus a general safety net: every shipped deploy script at least parses, and
the line-ending policy that prevents FAILURE 2 from recurring is actually
declared in .gitattributes.

Stdlib only, no network, no VM, no real systemd. The ssh/scp/systemctl
orchestration in the .sh scripts themselves cannot be exercised without a
real VM (out of scope here — see the hard constraint against mutating the
live VM) and is covered by review plus `bash -n`, not by unit tests.
"""

from __future__ import annotations

import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
SCRIPTS_DIR = REPO_ROOT / "scripts"
sys.path.insert(0, str(SCRIPTS_DIR))

from classify_suite_log import classify, INCONCLUSIVE, PASS, REAL_FAILURE  # noqa: E402
from normalize_line_endings import normalize, normalize_file  # noqa: E402
from restart_proof import evaluate_restart  # noqa: E402
from tests._process_cleanup import cleanup_tempdir  # noqa: E402

BASH = shutil.which("bash")


def _bash_script_path(script: Path, cwd: Path) -> str:
    """Return a Bash-safe path relative to the subprocess working directory.

    Windows-hosted Bash implementations can reinterpret a native absolute
    path before Bash sees it (for example, ``C:\\...`` becomes
    ``C:...``).  Passing a relative POSIX path avoids that conversion while
    remaining valid for native Bash on Linux.
    """
    return script.resolve().relative_to(cwd.resolve()).as_posix()


def _run_bash(script: Path, *args: str, cwd: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [BASH, *args, _bash_script_path(script, cwd)],
        cwd=cwd,
        capture_output=True,
        text=True,
    )


class ClassifySuiteLogTests(unittest.TestCase):
    """FAILURE 3 (real incident): the old gate piped the suite through
    `| tail -4` and treated an EMPTY capture as failure. A harness problem
    (python off PATH, an early kill, a lost pipe) was then indistinguishable
    from genuinely broken tests, and it discarded the evidence needed to
    tell them apart."""

    def test_empty_capture_is_inconclusive_not_failure(self):
        # This is the literal shape of the real incident.
        verdict = classify("", exit_code=None)
        self.assertEqual(verdict.label, INCONCLUSIVE)
        self.assertEqual(verdict.process_exit, 2)
        self.assertIsNone(verdict.test_count)

    def test_truncated_output_with_no_ran_line_is_inconclusive(self):
        # e.g. a timeout kills python before it ever prints a summary line.
        log = "Traceback (most recent call last):\n  ...\nKilled\n"
        verdict = classify(log, exit_code=137)
        self.assertEqual(verdict.label, INCONCLUSIVE)
        self.assertEqual(verdict.process_exit, 2)

    def test_pass(self):
        log = "...\n" + "-" * 40 + "\nRan 525 tests in 12.345s\n\nOK\n"
        verdict = classify(log, exit_code=0)
        self.assertEqual(verdict.label, PASS)
        self.assertEqual(verdict.process_exit, 0)
        self.assertEqual(verdict.test_count, 525)

    def test_real_failure(self):
        log = "Ran 525 tests in 12.345s\n\nFAILED (failures=2)\n"
        verdict = classify(log, exit_code=1)
        self.assertEqual(verdict.label, REAL_FAILURE)
        self.assertEqual(verdict.process_exit, 1)
        self.assertEqual(verdict.test_count, 525)

    def test_exit_code_wins_over_misleading_text(self):
        # "Check exit codes, never parse output when an exit code exists" —
        # manufacture text that looks like a pass but a nonzero exit code,
        # and confirm the exit code wins.
        log = "Ran 10 tests in 1.0s\n\nOK (unrelated line also happens to say OK)\n"
        verdict = classify(log, exit_code=1)
        self.assertEqual(verdict.label, REAL_FAILURE)

    def test_zero_tests_collected_is_inconclusive(self):
        # Discovery finding nothing means -s/-p or PYTHONPATH is almost
        # certainly misconfigured — not a legitimate empty-but-passing suite.
        log = "Ran 0 tests in 0.000s\n\nOK\n"
        verdict = classify(log, exit_code=0)
        self.assertEqual(verdict.label, INCONCLUSIVE)
        self.assertEqual(verdict.test_count, 0)

    def test_no_exit_code_falls_back_to_text(self):
        self.assertEqual(classify("Ran 3 tests in 0.01s\n\nOK\n", exit_code=None).label, PASS)
        self.assertEqual(
            classify("Ran 3 tests in 0.01s\n\nFAILED (errors=1)\n", exit_code=None).label,
            REAL_FAILURE,
        )

    def test_cli_end_to_end(self):
        with tempfile.TemporaryDirectory() as tmp:
            ok_log = Path(tmp) / "ok.log"
            ok_log.write_text("Ran 4 tests in 0.02s\n\nOK\n", encoding="utf-8")
            result = subprocess.run(
                [sys.executable, str(SCRIPTS_DIR / "classify_suite_log.py"), str(ok_log), "0"],
                capture_output=True, text=True,
            )
            self.assertEqual(result.returncode, 0)
            self.assertIn("PASS", result.stdout)

            empty_log = Path(tmp) / "empty.log"
            empty_log.write_text("", encoding="utf-8")
            result2 = subprocess.run(
                [sys.executable, str(SCRIPTS_DIR / "classify_suite_log.py"), str(empty_log)],
                capture_output=True, text=True,
            )
            self.assertEqual(result2.returncode, 2)
            self.assertIn("INCONCLUSIVE", result2.stdout)


class NormalizeLineEndingsTests(unittest.TestCase):
    """FAILURE 2 (real incident): redeploy-weft.sh reached the VM with CRLF
    line endings, so bash read `set -euo pipefail\\r`, treated `pipefail\\r`
    as an invalid option, and the cutover aborted mid-deploy."""

    def test_crlf_becomes_lf(self):
        data = b"#!/usr/bin/env bash\r\nset -euo pipefail\r\necho hi\r\n"
        normalized, changed = normalize(data)
        self.assertTrue(changed)
        self.assertNotIn(b"\r", normalized)
        self.assertEqual(normalized, b"#!/usr/bin/env bash\nset -euo pipefail\necho hi\n")

    def test_already_lf_is_unchanged(self):
        data = b"#!/usr/bin/env bash\nset -euo pipefail\n"
        normalized, changed = normalize(data)
        self.assertFalse(changed)
        self.assertEqual(normalized, data)

    def test_lone_cr_is_normalized_too(self):
        normalized, _ = normalize(b"a\rb\rc")
        self.assertEqual(normalized, b"a\nb\nc")

    def test_idempotent(self):
        once, _ = normalize(b"a\r\nb\rc\nd")
        twice, changed_again = normalize(once)
        self.assertEqual(once, twice)
        self.assertFalse(changed_again)

    def test_normalize_file_rewrites_in_place(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "script.sh"
            path.write_bytes(b"#!/usr/bin/env bash\r\nset -euo pipefail\r\n")
            changed = normalize_file(path)
            self.assertTrue(changed)
            self.assertNotIn(b"\r", path.read_bytes())

    def test_crlf_bytes_removed_regardless_of_local_bash_tolerance(self):
        # Portability note, found while writing this test: literally
        # reproducing FAILURE 2's "bash: set: pipefail\r: invalid option" in
        # THIS sandbox does not work — this bash (Git Bash / MSYS; see
        # `bash --version`) turns out to transparently strip bare CR bytes
        # before its own tokenizer ever sees them. Confirmed by embedding a
        # raw CR *inside* a quoted string (not at a line ending, so it can't
        # be mistaken for a line terminator) and watching it disappear from
        # `od -c` of the string's expansion. A real Linux bash on the VM has
        # no such translation layer — raw bytes are raw bytes. That is
        # exactly why the fix targets the file's on-disk bytes directly,
        # rather than depending on any particular bash build's tolerance for
        # a stray CR, which is not portable and not something to rely on.
        # This test asserts the one thing that IS portable: the byte
        # sequence that broke production is actually gone afterwards.
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "repro.sh"
            path.write_bytes(b"#!/usr/bin/env bash\r\nset -euo pipefail\r\necho ok\r\n")
            self.assertIn(b"pipefail\r", path.read_bytes(), "fixture must contain the exact byte sequence that broke production")

            normalize_file(path)
            normalized = path.read_bytes()
            self.assertNotIn(b"\r", normalized)
            self.assertIn(b"set -euo pipefail\n", normalized)

    @unittest.skipUnless(BASH, "bash not on PATH")
    def test_normalized_script_parses_and_runs_cleanly(self):
        # Whatever a given bash build tolerates in the un-normalized version
        # (see the portability note above — it varies), the NORMALIZED
        # version must parse and run cleanly everywhere.
        temporary = tempfile.TemporaryDirectory()
        try:
            tmp = temporary.name
            path = Path(tmp) / "repro.sh"
            path.write_bytes(b"#!/usr/bin/env bash\r\nset -euo pipefail\r\necho ok\r\n")
            normalize_file(path)

            syntax = _run_bash(path, "-n", cwd=path.parent)
            self.assertEqual(syntax.returncode, 0, syntax.stderr)

            run = _run_bash(path, cwd=path.parent)
            self.assertEqual(run.returncode, 0, run.stderr)
            self.assertEqual(run.stdout.strip(), "ok")
        finally:
            cleanup_tempdir(temporary)

    def test_cli_check_mode_never_writes(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "script.sh"
            original = b"echo hi\r\n"
            path.write_bytes(original)
            result = subprocess.run(
                [sys.executable, str(SCRIPTS_DIR / "normalize_line_endings.py"), "--check", str(path)],
                capture_output=True, text=True,
            )
            self.assertEqual(result.returncode, 1)
            self.assertEqual(path.read_bytes(), original, "--check must never write")

    def test_cli_rewrites_and_reports_missing_files_distinctly(self):
        with tempfile.TemporaryDirectory() as tmp:
            missing = Path(tmp) / "nope.sh"
            result = subprocess.run(
                [sys.executable, str(SCRIPTS_DIR / "normalize_line_endings.py"), str(missing)],
                capture_output=True, text=True,
            )
            self.assertEqual(result.returncode, 2)
            self.assertIn("missing", result.stderr)


class RestartProofTests(unittest.TestCase):
    """FAILURE 1 (real incident): after a "successful" deploy, weft-cloud
    and weft-web had been running 8h06m, serving OLD CODE FROM MEMORY.
    `systemctl enable --now` returned 0 without restarting anything.
    Verification came back 6/11; a manual restart then gave 11/11."""

    def test_pid_changed_is_proof_of_restart(self):
        failures = evaluate_restart(
            before={"weft-cloud.service": "1234"},
            after={"weft-cloud.service": "5678"},
        )
        self.assertEqual(failures, [])

    def test_unchanged_pid_is_the_real_incident(self):
        failures = evaluate_restart(
            before={"weft-cloud.service": "1234", "weft-web.service": "4321"},
            after={"weft-cloud.service": "1234", "weft-web.service": "9999"},
        )
        self.assertEqual(len(failures), 1)
        self.assertEqual(failures[0].unit, "weft-cloud.service")
        self.assertIn("unchanged", failures[0].reason)

    def test_zero_pid_after_is_not_running(self):
        failures = evaluate_restart(before={"a.service": "1"}, after={"a.service": "0"})
        self.assertEqual(len(failures), 1)
        self.assertIn("not running", failures[0].reason)

    def test_empty_pid_after_is_not_running(self):
        failures = evaluate_restart(before={"a.service": "1"}, after={"a.service": ""})
        self.assertEqual(len(failures), 1)

    def test_first_deploy_zero_before_is_fine(self):
        # A unit that was not running before (first deploy) only needs a
        # non-zero PID after -- nothing to "differ from" yet.
        failures = evaluate_restart(before={"a.service": "0"}, after={"a.service": "42"})
        self.assertEqual(failures, [])

    def test_missing_from_after_snapshot_is_a_failure(self):
        failures = evaluate_restart(before={"a.service": "1"}, after={})
        self.assertTrue(any(f.unit == "a.service" for f in failures))

    def test_multiple_units_independent(self):
        failures = evaluate_restart(
            before={"a.service": "1", "b.service": "2", "c.service": "3"},
            after={"a.service": "9", "b.service": "2", "c.service": "0"},
        )
        self.assertEqual({f.unit for f in failures}, {"b.service", "c.service"})

    def test_cli_end_to_end(self):
        with tempfile.TemporaryDirectory() as tmp:
            before = Path(tmp) / "before.env"
            after_bad = Path(tmp) / "after_bad.env"
            after_good = Path(tmp) / "after_good.env"
            before.write_text("weft-cloud.service=100\nweft-web.service=200\n", encoding="utf-8")
            after_bad.write_text("weft-cloud.service=100\nweft-web.service=999\n", encoding="utf-8")
            after_good.write_text("weft-cloud.service=101\nweft-web.service=999\n", encoding="utf-8")

            bad = subprocess.run(
                [sys.executable, str(SCRIPTS_DIR / "restart_proof.py"), str(before), str(after_bad)],
                capture_output=True, text=True,
            )
            self.assertEqual(bad.returncode, 1)
            self.assertIn("FAIL", bad.stdout)
            self.assertIn("weft-cloud.service", bad.stdout)

            good = subprocess.run(
                [sys.executable, str(SCRIPTS_DIR / "restart_proof.py"), str(before), str(after_good)],
                capture_output=True, text=True,
            )
            self.assertEqual(good.returncode, 0)


class ShippedScriptSanityTests(unittest.TestCase):
    """A cheap, general safety net directly in the spirit of FAILURE 2: every
    .sh script this repo ships must at least parse, every .py helper must at
    least compile, and the line-ending policy that prevents CRLF from
    reaching the VM in the first place must actually be declared."""

    @unittest.skipUnless(BASH, "bash not on PATH")
    def test_every_deploy_sh_script_passes_bash_n(self):
        sh_scripts = sorted(SCRIPTS_DIR.glob("*.sh"))
        self.assertGreater(len(sh_scripts), 0, "expected at least one .sh script in scripts/")
        for script in sh_scripts:
            result = _run_bash(script, "-n", cwd=SCRIPTS_DIR)
            self.assertEqual(result.returncode, 0, f"{script.name}: {result.stderr}")

    def test_every_deploy_py_helper_compiles(self):
        py_scripts = [
            "classify_suite_log.py", "normalize_line_endings.py", "restart_proof.py",
            "backup_cloud_db.py", "restore_drill.py", "healthcheck.py", "mail_error_detail.py",
            "ensure_nginx_routes.py",
        ]
        for name in py_scripts:
            path = SCRIPTS_DIR / name
            self.assertTrue(path.is_file(), f"missing {name}")
            result = subprocess.run([sys.executable, "-m", "py_compile", str(path)], capture_output=True, text=True)
            self.assertEqual(result.returncode, 0, f"{name}: {result.stderr}")

    def test_gitattributes_forces_lf_for_shell_and_python_scripts(self):
        gitattributes = REPO_ROOT / ".gitattributes"
        self.assertTrue(gitattributes.is_file(), ".gitattributes must exist to prevent FAILURE 2 from recurring")
        text = gitattributes.read_text(encoding="utf-8")
        self.assertRegex(text, r"\*\.sh\s+text\s+eol=lf")
        self.assertRegex(text, r"\*\.py\s+text\s+eol=lf")

    def test_no_shipped_sh_script_has_crlf_on_disk_right_now(self):
        # Belt-and-suspenders on the actual working tree: if this ever
        # fails, .gitattributes normalization did not take for some reason
        # and a real script is sitting on disk with CRLF right now — the
        # exact precondition for FAILURE 2.
        for script in sorted(SCRIPTS_DIR.glob("*.sh")):
            self.assertNotIn(b"\r\n", script.read_bytes(), f"{script.name} has CRLF on disk")

    def test_deploy_scripts_present_and_executable_bit_not_required_on_windows(self):
        # The four scripts named explicitly in the ops brief must exist
        # under version control (that is the whole point of this task).
        for name in ("push-code-to-vm.sh", "redeploy-weft.sh", "final-verify.sh", "suite-check.sh"):
            self.assertTrue((SCRIPTS_DIR / name).is_file(), f"missing scripts/{name}")

    def test_push_script_requires_pinned_ssh_identity(self):
        script = (SCRIPTS_DIR / "push-code-to-vm.sh").read_text(encoding="utf-8")
        self.assertIn("WEFT_SSH_KNOWN_HOSTS", script)
        self.assertIn("StrictHostKeyChecking=yes", script)
        self.assertIn('UserKnownHostsFile=$KNOWN_HOSTS', script)
        self.assertIn("BatchMode=yes", script)
        self.assertNotIn("StrictHostKeyChecking=no", script)
        self.assertIn("verified SSH known_hosts file not found", script)

    def test_redeploy_requires_an_explicit_and_matching_nginx_site(self):
        push = (SCRIPTS_DIR / "push-code-to-vm.sh").read_text(encoding="utf-8")
        redeploy = (SCRIPTS_DIR / "redeploy-weft.sh").read_text(encoding="utf-8")
        helper = (SCRIPTS_DIR / "ensure_nginx_routes.py").read_text(encoding="utf-8")
        self.assertIn("WEFT_NGINX_CONF", push)
        self.assertIn("WEFT_NGINX_SERVER_NAME", push)
        self.assertIn("WEFT_NGINX_CONF", redeploy)
        self.assertIn("WEFT_NGINX_SERVER_NAME", redeploy)
        self.assertNotIn("ls /etc/nginx/sites-enabled/", redeploy)
        self.assertIn("NGINX_BACKUP=", redeploy)
        self.assertIn("location = /mcp", helper)
        self.assertIn("ensure_nginx_routes.py", redeploy)
        self.assertIn("restored $CONF from $NGINX_BACKUP", redeploy)


class RedeployBackupSafetyTests(unittest.TestCase):
    """A cutover must fail closed until the database is recoverably backed up."""

    def test_redeploy_requires_backup_and_restore_proof_before_promotion(self):
        script = (SCRIPTS_DIR / "redeploy-weft.sh").read_text(encoding="utf-8")
        backup = script.index('echo "== WAL-safe backup')
        retention = script.index('echo "== release retention')
        promotion = script.index('echo "== promote staged code')
        safety_window = script[backup:retention]

        self.assertLess(backup, retention)
        self.assertLess(retention, promotion)
        self.assertIn("backup_cloud_db.py failed; refusing to promote", safety_window)
        self.assertIn("unavailable; refusing an unsafe raw-copy fallback", safety_window)
        self.assertIn("restore_drill.py is unavailable; refusing promotion", safety_window)
        self.assertIn("FAILED the restore drill; refusing promotion", safety_window)
        self.assertNotIn("deploy below still proceeds", safety_window)
        self.assertNotIn("Falling back to a raw copy", safety_window)

        # Every failure branch in the pre-promotion safety window must terminate
        # the shell process; a warning without an exit is the original defect.
        self.assertGreaterEqual(safety_window.count("exit 1"), 5)

    def test_redeploy_propagates_public_origin_and_join_route(self):
        script = (SCRIPTS_DIR / "redeploy-weft.sh").read_text(encoding="utf-8")
        helper = (SCRIPTS_DIR / "ensure_nginx_routes.py").read_text(encoding="utf-8")
        self.assertIn("Environment=WEFT_PUBLIC_ORIGIN=$PUBLIC_ORIGIN", script)
        self.assertIn("location ^~ /j/", helper,
                      "nginx must forward generated public join links to weft-cloud")
        self.assertIn("location = /mcp", helper,
                      "nginx must continue forwarding hosted MCP to weft-cloud")

    def test_healthcheck_template_probes_edge_separately(self):
        dockerfile = (REPO_ROOT / "Dockerfile").read_text(encoding="utf-8")
        self.assertIn("/readyz", dockerfile)
        self.assertNotIn("/healthz', timeout=3", dockerfile)
        compose = (REPO_ROOT / "compose.yaml").read_text(encoding="utf-8")
        self.assertIn("baked-in HEALTHCHECK probes WEFT_PORT/readyz", compose)
        self.assertNotIn("baked-in HEALTHCHECK probes WEFT_PORT/healthz", compose)

        unit = (SCRIPTS_DIR / "systemd" / "weft-healthcheck.service").read_text(encoding="utf-8")
        self.assertIn("EnvironmentFile=/etc/weft/healthcheck.env", unit)
        self.assertIn("ExecStartPre=/usr/bin/install -d -o azureuser -g azureuser -m 0750 /var/log/weft", unit)
        self.assertIn("ExecStartPre=/usr/bin/test -n ${WEFT_EDGE_URL}", unit)
        self.assertIn("--base-url http://127.0.0.1:18788", unit)
        self.assertIn("--edge-url ${WEFT_EDGE_URL}", unit)
        self.assertIn("--require-https-edge", unit)
        self.assertNotIn("Environment=WEFT_EDGE_URL=http://127.0.0.1", unit)

        backup = (SCRIPTS_DIR / "systemd" / "weft-backup.service").read_text(encoding="utf-8")
        self.assertIn("PermissionsStartOnly=true", backup)
        self.assertIn("ExecStartPre=/usr/bin/install -d -o azureuser -g azureuser -m 0700 /var/backups/weft", backup)

    def test_compose_delivery_profile_shares_store_without_committing_secrets(self):
        compose = (REPO_ROOT / "compose.yaml").read_text(encoding="utf-8")
        cloud_start = compose.index("  weft-cloud:")
        cloud_end = compose.index("\n  # Browser front-end", cloud_start)
        cloud_block = compose[cloud_start:cloud_end]
        self.assertIn(
            'WEFT_PUBLIC_ORIGIN: "${WEFT_PUBLIC_ORIGIN:-http://127.0.0.1:18788}"',
            cloud_block,
        )
        self.assertNotIn("WEFT_WEB_PUBLIC_ORIGIN", cloud_block)
        web_start = compose.index("  weft-web:")
        web_end = compose.index("\n  # Hosted room-delivery worker", web_start)
        web_block = compose[web_start:web_end]
        self.assertIn(
            'WEFT_PUBLIC_ORIGIN: "${WEFT_PUBLIC_ORIGIN:-http://127.0.0.1:18788}"',
            web_block,
        )
        self.assertIn(
            'WEFT_WEB_PUBLIC_ORIGIN: "${WEFT_WEB_PUBLIC_ORIGIN:-${WEFT_PUBLIC_ORIGIN:-http://127.0.0.1:18789}}"',
            web_block,
        )
        delivery_start = compose.index("  weft-delivery:")
        delivery_end = compose.index("\n  # Opt-in SMTP delivery worker", delivery_start)
        delivery_block = compose[delivery_start:delivery_end]
        self.assertIn('profiles: ["cloud-delivery"]', delivery_block)
        self.assertIn(
            'command: ["python", "-B", "-m", "weft_cloud.delivery_worker"]',
            delivery_block,
        )
        self.assertIn('WEFT_DB_PATH: "/data/weft-cloud.db"', delivery_block)
        self.assertIn(
            'WEFT_DELIVERY_SINK: "/data/delivery.jsonl"',
            delivery_block,
        )
        self.assertIn(
            'WEFT_DELIVERY_DRAIN_INTERVAL: "${WEFT_DELIVERY_DRAIN_INTERVAL:-5}"',
            delivery_block,
        )
        self.assertIn("weft-cloud-data:/data", delivery_block)
        self.assertNotIn("weft-web:", delivery_block)
        self.assertNotIn("ports:", delivery_block)
        self.assertIn("condition: service_healthy", delivery_block)
        self.assertIn("restart: on-failure", delivery_block)

        start = compose.index("  weft-outbox:")
        block = compose[start:compose.index("\nvolumes:", start)]
        self.assertIn('profiles: ["delivery"]', block)
        self.assertIn("weft_cloud.identity.outbox_worker", block)
        self.assertIn('WEFT_DB_PATH: "/data/weft-cloud.db"', block)
        self.assertIn("weft-cloud-data:/data", block)
        self.assertIn("WEFT_SMTP_HOST", block)
        self.assertIn("WEFT_SMTP_PASSWORD", block)
        self.assertIn("restart: on-failure", block)
        self.assertIn("condition: service_healthy", block)
        self.assertIn("WEFT_WEB_PUBLIC_ORIGIN", compose)

    def test_outbox_systemd_template_keeps_smtp_secrets_external(self):
        unit = (SCRIPTS_DIR / "systemd" / "weft-outbox.service").read_text(encoding="utf-8")
        self.assertIn("EnvironmentFile=-/etc/weft/weft-email.env", unit)
        self.assertIn("Environment=WEFT_DB_PATH=/var/lib/finalisma/cloud.db", unit)
        self.assertIn("weft_cloud.identity.outbox_worker", unit)
        self.assertIn("Requires=weft-cloud.service weft-web.service", unit)
        self.assertIn("Restart=on-failure", unit)
        self.assertNotIn("WEFT_SMTP_PASSWORD=", unit)

        canonical_db = "/var/lib/finalisma/cloud.db"
        for relative in (
            "redeploy-weft.sh",
            "rollback-weft.sh",
            "systemd/weft-backup.service",
            "systemd/weft-outbox.service",
        ):
            text = (SCRIPTS_DIR / relative).read_text(encoding="utf-8")
            self.assertIn(canonical_db, text, relative)
        self.assertIn(canonical_db, (REPO_ROOT / "docs" / "VM_OPERATIONS.md").read_text(encoding="utf-8"))
        for relative in ("docs/LIVE_DEPLOYMENT.md", "docs/HOSTED_MCP_DESIGN.md"):
            text = (REPO_ROOT / relative).read_text(encoding="utf-8")
            self.assertNotIn("/var/lib/weft/cloud.db", text, relative)


if __name__ == "__main__":
    unittest.main()
