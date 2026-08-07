"""The web app has a runnable entry point that boots and serves.

``python -m weft_cloud.web`` is the deployable launcher — the missing
piece that took the web app from "green but not startable" to deployable. This
file is its regression guard: it starts the REAL module as a subprocess (the
same way a container would), asserts the public pages serve 200, then shuts it
down. Without this guard the launcher can regress to "imports fine, never
binds" and nothing notices.

Also covers ``runtime_config`` for the web launcher: same argv > env > default
precedence and fail-loud-on-bad-port discipline as the cloud service
(``weft_cloud.service.runtime_config``), so the two entry points stay
consistent.
"""

from __future__ import annotations

import http.client
import os
import socket
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from weft_cloud.web.__main__ import runtime_config  # noqa: E402

SITE_DIR = str(ROOT / "site")


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _get(port: int, path: str, timeout: float = 10) -> int:
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=timeout)
    try:
        conn.request("GET", path)
        resp = conn.getresponse()
        resp.read()
        return resp.status
    finally:
        conn.close()


class TestWebRuntimeConfig(unittest.TestCase):
    def test_defaults_with_no_input(self) -> None:
        cfg = runtime_config(argv=[], environ={})
        self.assertEqual(cfg, {
            "host": "127.0.0.1",
            "port": 18789,
            "db_path": "./data/finalisma-web.db",
            "static_dir": "./site",
            "state_dir": "./data",
        })

    def test_env_overrides_defaults(self) -> None:
        cfg = runtime_config(argv=[], environ={
            "FINALISMA_WEB_HOST": "0.0.0.0",
            "FINALISMA_WEB_PORT": "19010",
            "FINALISMA_WEB_DB_PATH": "/data/web.db",
            "FINALISMA_WEB_STATIC_DIR": "/app/site",
            "FINALISMA_WEB_STATE_DIR": "/data/state",
        })
        self.assertEqual(cfg, {
            "host": "0.0.0.0",
            "port": 19010,
            "db_path": "/data/web.db",
            "static_dir": "/app/site",
            "state_dir": "/data/state",
        })

    def test_argv_overrides_env(self) -> None:
        cfg = runtime_config(argv=["19020", "/tmp/argv.db"], environ={
            "FINALISMA_WEB_PORT": "19010",
            "FINALISMA_WEB_DB_PATH": "/data/web.db",
        })
        self.assertEqual(cfg["port"], 19020)
        self.assertEqual(cfg["db_path"], "/tmp/argv.db")
        self.assertEqual(cfg["host"], "127.0.0.1")

    def test_invalid_env_port_raises(self) -> None:
        with self.assertRaises(ValueError):
            runtime_config(argv=[], environ={"FINALISMA_WEB_PORT": "not-a-number"})

    def test_out_of_range_port_raises(self) -> None:
        with self.assertRaises(ValueError):
            runtime_config(argv=["70000"], environ={})


class TestWebEntryPointBootsAndServes(unittest.TestCase):
    def test_module_starts_prints_url_and_serves_signup_login(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            db_path = str(Path(tmp) / "web.db")
            state_dir = str(Path(tmp) / "state")
            port = _free_port()
            stderr_path = Path(tmp) / "stderr.log"
            env = dict(os.environ)
            env.update({
                "PYTHONPATH": str(ROOT / "src"),
                "FINALISMA_WEB_HOST": "127.0.0.1",
                "FINALISMA_WEB_PORT": str(port),
                "FINALISMA_WEB_DB_PATH": db_path,
                "FINALISMA_WEB_STATIC_DIR": SITE_DIR,
                "FINALISMA_WEB_STATE_DIR": state_dir,
            })
            with open(stderr_path, "wb") as stderr_fh:
                proc = subprocess.Popen(
                    [sys.executable, "-B", "-m", "weft_cloud.web"],
                    cwd=str(ROOT),
                    env=env,
                    stdout=subprocess.DEVNULL,
                    stderr=stderr_fh,
                    text=False,
                )
            try:
                self._wait_until_serving(port)
                self.assertEqual(_get(port, "/signup"), 200, "GET /signup")
                self.assertEqual(_get(port, "/login"), 200, "GET /login")
            finally:
                proc.terminate()
                try:
                    proc.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    proc.kill()
                    proc.wait(timeout=10)
            bound = stderr_path.read_text(encoding="utf-8", errors="replace")
            self.assertIn(
                f"finalisma-web listening on http://127.0.0.1:{port}",
                bound,
                "launcher must print its bound URL to stderr",
            )

    def _wait_until_serving(self, port: int, timeout: float = 20) -> None:
        deadline = time.monotonic() + timeout
        last_error: Exception | None = None
        while time.monotonic() < deadline:
            try:
                if _get(port, "/signup") == 200:
                    return
            except (ConnectionRefusedError, OSError, http.client.HTTPException) as exc:
                last_error = exc
            time.sleep(0.25)
        self.fail(f"web app did not start serving on port {port}: {last_error}")


if __name__ == "__main__":
    unittest.main()
