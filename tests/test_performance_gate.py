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
            self.assertEqual(GATE.QUALITY_GATE_TEST_TIMEOUT_DEFAULT, 900)
            self.assertEqual(
                int(os.environ.get(
                    "WEFT_GATE_TEST_TIMEOUT",
                    str(GATE.QUALITY_GATE_TEST_TIMEOUT_DEFAULT),
                )),
                900,
            )

    def test_digest_scope_excludes_quality_runner_timeout(self) -> None:
        source = GATE_PATH.read_text(encoding="utf-8")
        changed_timeout = source.replace(
            "QUALITY_GATE_TEST_TIMEOUT_DEFAULT = 900",
            "QUALITY_GATE_TEST_TIMEOUT_DEFAULT = 1200",
        )
        self.assertEqual(GATE._benchmark_digest(source), GATE._benchmark_digest(changed_timeout))

    def test_digest_matches_reference_baseline_metadata(self) -> None:
        baseline_path = ROOT / ".omx" / "goals" / "performance" / "single-node-coordinator-envelope" / "baseline.json"
        baseline = __import__("json").loads(baseline_path.read_text(encoding="utf-8"))
        self.assertEqual(baseline["harness_digest_scope"], GATE.BENCHMARK_DIGEST_SCOPE)
        self.assertEqual(baseline["harness_sha256"], GATE._harness_digest())
        self.assertEqual(
            baseline["legacy_harness_sha256"],
            "bed1c5f70bb4cfaa534a3fc4d3c7b821947715ed94b3852b2ed170fb7a65f4e7",
        )


if __name__ == "__main__":
    unittest.main()
