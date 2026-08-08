"""Runtime-config resolution for the cloud service launcher.

Containers configure through the environment (``WEFT_HOST``,
``WEFT_PORT``, ``WEFT_DB_PATH``); the original positional argv form
(``service.py <port> <db-path>``) must keep working so nothing that already
depends on it changes behaviour. Precedence is argv > env > default, and a
bad port fails loudly instead of silently defaulting.
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from weft_cloud.service import runtime_config


class TestRuntimeConfig(unittest.TestCase):
    def test_defaults_with_no_input(self) -> None:
        cfg = runtime_config(argv=[], environ={})
        self.assertEqual(cfg, {
            "host": "127.0.0.1",
            "port": 18788,
            "db_path": "./data/weft-cloud.db",
            "origin": "http://127.0.0.1:18788",
        })

    def test_env_overrides_defaults(self) -> None:
        cfg = runtime_config(argv=[], environ={
            "WEFT_HOST": "0.0.0.0",
            "WEFT_PORT": "19001",
            "WEFT_DB_PATH": "/data/cloud.db",
            "WEFT_PUBLIC_ORIGIN": "https://connect.example",
        })
        self.assertEqual(cfg, {
            "host": "0.0.0.0",
            "port": 19001,
            "db_path": "/data/cloud.db",
            "origin": "https://connect.example",
        })

    def test_argv_overrides_env(self) -> None:
        cfg = runtime_config(argv=["19999", "/tmp/argv.db"], environ={
            "WEFT_HOST": "0.0.0.0",
            "WEFT_PORT": "19001",
            "WEFT_DB_PATH": "/data/cloud.db",
        })
        self.assertEqual(cfg, {
            "host": "0.0.0.0",
            "port": 19999,
            "db_path": "/tmp/argv.db",
            "origin": "http://127.0.0.1:18788",
        })

    def test_argv_port_only_keeps_env_db_path(self) -> None:
        cfg = runtime_config(argv=["12345"], environ={"WEFT_DB_PATH": "/data/x.db"})
        self.assertEqual(cfg["port"], 12345)
        self.assertEqual(cfg["db_path"], "/data/x.db")
        self.assertEqual(cfg["host"], "127.0.0.1")
        self.assertEqual(cfg["origin"], "http://127.0.0.1:18788")

    def test_invalid_env_port_raises(self) -> None:
        with self.assertRaises(ValueError):
            runtime_config(argv=[], environ={"WEFT_PORT": "not-a-number"})

    def test_out_of_range_port_raises(self) -> None:
        with self.assertRaises(ValueError):
            runtime_config(argv=["70000"], environ={})


if __name__ == "__main__":
    unittest.main()
