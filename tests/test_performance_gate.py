from __future__ import annotations

import importlib.util
import os
from pathlib import Path
import unittest
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
GATE_PATH = ROOT / "scripts" / "weft_performance_gate.py"
SPEC = importlib.util.spec_from_file_location("weft_performance_gate", GATE_PATH)
assert SPEC is not None and SPEC.loader is not None
GATE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(GATE)


class PerformanceGateConfigTests(unittest.TestCase):
    def test_default_quality_gate_timeout_covers_measured_full_suite(self) -> None:
        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop("WEFT_GATE_TEST_TIMEOUT", None)
            self.assertEqual(GATE.QUALITY_GATE_TEST_TIMEOUT_DEFAULT, 420)
            self.assertEqual(
                int(os.environ.get(
                    "WEFT_GATE_TEST_TIMEOUT",
                    str(GATE.QUALITY_GATE_TEST_TIMEOUT_DEFAULT),
                )),
                420,
            )


if __name__ == "__main__":
    unittest.main()
