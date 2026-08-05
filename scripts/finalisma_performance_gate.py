"""Reproducible evaluator for Finalisma's single-node coordination envelope.

The benchmark uses only public ``FinalismaStore`` operations and the Python
standard library.  A baseline is captured before optimization and then treated
as immutable evaluator evidence.
"""

from __future__ import annotations

import argparse
import collections
import gc
import hashlib
import json
import os
import platform
import re
import sqlite3
import statistics
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from typing import Any, Callable


PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from finalisma_mcp.core import FinalismaStore  # noqa: E402


HARNESS_VERSION = 1
EXPECTED_EXISTING_TESTS = 53
TARGET_IMPROVEMENT_PERCENT = 20.0
MAX_P95_REGRESSION_PERCENT = 5.0
SCENARIO_WEIGHTS = {
    "routing_fanout": 0.40,
    "session_relay": 0.35,
    "authenticated_core": 0.25,
}
RAW_CREDENTIAL_PATTERN = re.compile(r"fst_(?:actor|pair|session)_[A-Za-z0-9_-]+")


def _digest(value: Any) -> str:
    payload = json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _harness_digest() -> str:
    return hashlib.sha256(Path(__file__).read_bytes()).hexdigest()


def _percent_change(reference: float, current: float) -> float:
    if reference == 0:
        return 0.0 if current == 0 else float("inf")
    return ((current - reference) / reference) * 100.0


def _p95(samples: list[float]) -> float:
    if len(samples) == 1:
        return samples[0]
    return statistics.quantiles(samples, n=20, method="inclusive")[18]


def _store(root: Path) -> FinalismaStore:
    return FinalismaStore(
        root / "state.db",
        root / "workspace",
        require_actor_auth=True,
    )


def _register_agents(store: FinalismaStore, count: int) -> dict[str, str]:
    profiles = (
        ("architect", "planning", "qwencloud/qwen3.8-max-preview"),
        ("security", "security", "opencode-go/mimo-v2.5"),
        ("reviewer", "testing", "longcat/LongCat-2.0"),
        ("coder", "python", "gpt-5.6-luna"),
    )
    tokens: dict[str, str] = {}
    for index in range(count):
        role, capability, model = profiles[index % len(profiles)]
        agent_id = f"agent-{index:02d}"
        result = store.register_agent(
            "perf-team",
            agent_id,
            name=f"Agent {index:02d}",
            role=role,
            model=model,
            capabilities=[capability, "coordination", f"lane-{index % 8}"],
        )
        tokens[agent_id] = result["actor_token"]
    return tokens


def _routing_fanout(root: Path) -> tuple[float, dict[str, Any], list[str]]:
    store = _store(root)
    tokens = _register_agents(store, 32)

    # Seed realistic load outside the timed region so routing has to account
    # for active leases instead of selecting from an empty team.
    for index in range(8):
        agent_id = f"agent-{index:02d}"
        task_result = store.create_task(
            "perf-team",
            "agent-00",
            f"Seed load lane {index} token-{index * 7919}",
            f"Hold work for deterministic route load lane {index}.",
            scope=[f"seed/lane-{index}.txt"],
            preferred_agent=agent_id,
            idempotency_key=f"perf-seed-{index:04d}",
            actor_token=tokens["agent-00"],
        )
        store.claim_task(
            "perf-team",
            agent_id,
            task_result["task"]["task_id"],
            actor_token=tokens[agent_id],
        )

    prompts = (
        ("Plan a migration", "Create an architecture and planning sequence."),
        ("Audit the boundary", "Review security controls and threat handling."),
        ("Verify the release", "Run testing and reviewer checks."),
        ("Implement the patch", "Write Python code for the coordinator."),
    )
    selections: collections.Counter[str] = collections.Counter()
    active_task_total = 0
    model_selections: collections.Counter[str] = collections.Counter()

    started = time.perf_counter_ns()
    for index in range(320):
        title, description = prompts[index % len(prompts)]
        result = store.route_task(
            "perf-team",
            f"{title} iteration {index}",
            description,
            agent_id="agent-00",
            actor_token=tokens["agent-00"],
        )
        selection = result["selection"]
        assert selection is not None
        selections[selection["agent_id"]] += 1
        model_selections[selection["model"]] += 1
        active_task_total += int(selection["active_tasks"])
    elapsed_ms = (time.perf_counter_ns() - started) / 1_000_000

    semantics = {
        "calls": 320,
        "selections": dict(sorted(selections.items())),
        "models": dict(sorted(model_selections.items())),
        "active_task_total": active_task_total,
    }
    return elapsed_ms, semantics, list(tokens.values())


def _session_relay(root: Path) -> tuple[float, dict[str, Any], list[str]]:
    store = _store(root)
    registered = store.register_agent(
        "perf-team",
        "agent-a",
        "Planner",
        "architect",
        "gpt-5.6-sol",
        ["planning", "coordination"],
    )
    actor_a = registered["actor_token"]
    pairing = store.create_pairing(
        "agent-a",
        "perf-team",
        capabilities_offered=["task", "message", "evidence"],
        actor_token=actor_a,
    )
    joined = store.join_pairing(
        pairing["join_token"],
        "agent-b",
        name="Builder",
        role="coder",
        model="opencode-go/mimo-v2.5",
        capabilities=["python", "testing"],
        consent=True,
    )
    actor_b = joined["actor_token"]
    session_a = pairing["initiator_session_token"]
    session_b = joined["session_token"]
    cursor_a = 0
    cursor_b = 0
    received_a = 0
    received_b = 0

    started = time.perf_counter_ns()
    for index in range(80):
        store.session_send(
            session_a,
            "agent-a",
            "task.delta",
            {"iteration": index, "action": "build", "files": [f"src/lane-{index % 8}.py"]},
            f"relay-a-{index:04d}",
            trace_id=f"trace-{index:04d}",
        )
        polled_b = store.session_poll(session_b, "agent-b", after_seq=cursor_b, limit=8)
        received_b += len(polled_b["events"])
        cursor_b = polled_b["next_seq"]
        store.session_ack(session_b, "agent-b", cursor_b)

        store.session_send(
            session_b,
            "agent-b",
            "review.delta",
            {"iteration": index, "status": "passed", "finding_count": index % 3},
            f"relay-b-{index:04d}",
            trace_id=f"trace-{index:04d}",
        )
        polled_a = store.session_poll(session_a, "agent-a", after_seq=cursor_a, limit=8)
        received_a += len(polled_a["events"])
        cursor_a = polled_a["next_seq"]
        store.session_ack(session_a, "agent-a", cursor_a)
    elapsed_ms = (time.perf_counter_ns() - started) / 1_000_000

    final_a = store.session_status(session_a, "agent-a")
    final_b = store.session_status(session_b, "agent-b")
    semantics = {
        "sent": 160,
        "received_a": received_a,
        "received_b": received_b,
        "cursor_head": final_a["cursor_head"],
        "cursor_a": next(item["last_ack_seq"] for item in final_a["cursors"] if item["agent_id"] == "agent-a"),
        "cursor_b": next(item["last_ack_seq"] for item in final_b["cursors"] if item["agent_id"] == "agent-b"),
        "state": final_a["state"],
    }
    secrets = [actor_a, actor_b, pairing["join_token"], session_a, session_b]
    return elapsed_ms, semantics, secrets


def _authenticated_core(root: Path) -> tuple[float, dict[str, Any], list[str]]:
    store = _store(root)
    tokens = _register_agents(store, 3)
    workspace = root / "workspace"
    for index in range(24):
        evidence_path = workspace / "artifacts" / f"result-{index:02d}.txt"
        evidence_path.parent.mkdir(parents=True, exist_ok=True)
        evidence_path.write_text(f"verified result {index}\n", encoding="utf-8")

    done = 0
    evidence_passed = 0
    messages_read = 0
    started = time.perf_counter_ns()
    for index in range(24):
        created = store.create_task(
            "perf-team",
            "agent-00",
            f"Deliver isolated result {index} token-{index * 104729}",
            f"Produce and verify artifact number {index}.",
            scope=[f"artifacts/result-{index:02d}.txt"],
            preferred_agent="agent-01",
            idempotency_key=f"perf-task-{index:04d}",
            actor_token=tokens["agent-00"],
        )
        task = created["task"]
        claimed = store.claim_task(
            "perf-team",
            "agent-01",
            task["task_id"],
            actor_token=tokens["agent-01"],
        )
        store.update_task(
            "perf-team",
            "agent-01",
            task["task_id"],
            progress=80,
            note="Implementation ready for verification",
            fencing_token=claimed["fencing_token"],
            actor_token=tokens["agent-01"],
        )
        store.send_message(
            "perf-team",
            "agent-00",
            "task.context",
            {"iteration": index, "artifact": f"result-{index:02d}.txt"},
            recipient_id="agent-01",
            task_id=task["task_id"],
            idempotency_key=f"perf-message-{index:04d}",
            actor_token=tokens["agent-00"],
        )
        inbox = store.read_inbox(
            "perf-team",
            "agent-01",
            limit=4,
            actor_token=tokens["agent-01"],
        )
        messages_read += inbox["count"]
        verified = store.verify_task(
            "perf-team",
            "agent-01",
            task["task_id"],
            claimed["fencing_token"],
            [f"artifacts/result-{index:02d}.txt"],
            [{"name": "artifact-check", "status": "passed", "evidence": "deterministic fixture"}],
            reviewer_id="agent-02",
            require_review=True,
            actor_token=tokens["agent-01"],
        )
        evidence_passed += int(verified["passed"])
        completed = store.complete_task(
            "perf-team",
            "agent-01",
            task["task_id"],
            claimed["fencing_token"],
            "Verified benchmark handoff",
            actor_token=tokens["agent-01"],
        )
        done += int(completed["status"] == "done")
    elapsed_ms = (time.perf_counter_ns() - started) / 1_000_000

    status = store.team_status(
        "perf-team",
        agent_id="agent-00",
        actor_token=tokens["agent-00"],
    )
    semantics = {
        "tasks_done": done,
        "evidence_passed": evidence_passed,
        "messages_read": messages_read,
        "team_task_statuses": collections.Counter(task["status"] for task in status["tasks"]),
        "agent_count": len(status["agents"]),
    }
    semantics["team_task_statuses"] = dict(sorted(semantics["team_task_statuses"].items()))
    return elapsed_ms, semantics, list(tokens.values())


Scenario = Callable[[Path], tuple[float, dict[str, Any], list[str]]]
SCENARIOS: dict[str, Scenario] = {
    "routing_fanout": _routing_fanout,
    "session_relay": _session_relay,
    "authenticated_core": _authenticated_core,
}


def _run_trial(base: Path, trial_index: int) -> tuple[dict[str, float], dict[str, str], list[str]]:
    names = list(SCENARIOS)
    offset = trial_index % len(names)
    names = names[offset:] + names[:offset]
    timings: dict[str, float] = {}
    digests: dict[str, str] = {}
    secrets: list[str] = []
    for name in names:
        scenario_root = base / f"trial-{trial_index:02d}" / name
        scenario_root.mkdir(parents=True, exist_ok=True)
        gc.collect()
        elapsed_ms, semantics, scenario_secrets = SCENARIOS[name](scenario_root)
        timings[name] = elapsed_ms
        digests[name] = _digest(semantics)
        secrets.extend(scenario_secrets)
    return timings, digests, secrets


def _benchmark(runs: int) -> tuple[dict[str, Any], list[str]]:
    root = PROJECT_ROOT / ".tmp"
    root_existed = root.exists()
    root.mkdir(parents=True, exist_ok=True)
    secrets: list[str] = []
    try:
        with tempfile.TemporaryDirectory(prefix="finalisma-perf-", dir=root) as temporary:
            base = Path(temporary)
            _run_trial(base, -1)  # warm-up; intentionally excluded
            timing_runs: list[dict[str, float]] = []
            reference_digests: dict[str, str] | None = None
            for trial_index in range(runs):
                timings, digests, trial_secrets = _run_trial(base, trial_index)
                if reference_digests is None:
                    reference_digests = digests
                elif digests != reference_digests:
                    raise RuntimeError(f"semantic digest changed between trials: {digests!r}")
                timing_runs.append(timings)
                secrets.extend(trial_secrets)
    finally:
        if not root_existed:
            try:
                root.rmdir()
            except OSError:
                pass

    assert reference_digests is not None
    scenario_summary: dict[str, dict[str, Any]] = {}
    for name, weight in SCENARIO_WEIGHTS.items():
        samples = [trial[name] for trial in timing_runs]
        scenario_summary[name] = {
            "weight": weight,
            "median_ms": round(statistics.median(samples), 3),
            "p95_ms": round(_p95(samples), 3),
            "samples_ms": [round(sample, 3) for sample in samples],
            "semantic_digest": reference_digests[name],
        }
    composite_samples = [
        sum(SCENARIO_WEIGHTS[name] * trial[name] for name in SCENARIO_WEIGHTS)
        for trial in timing_runs
    ]
    return (
        {
            "runs": runs,
            "weighted_median_ms": round(statistics.median(composite_samples), 3),
            "weighted_p95_ms": round(_p95(composite_samples), 3),
            "composite_samples_ms": [round(sample, 3) for sample in composite_samples],
            "scenarios": scenario_summary,
        },
        secrets,
    )


def _environment() -> dict[str, str]:
    return {
        "python": platform.python_version(),
        "sqlite": sqlite3.sqlite_version,
        "platform": platform.platform(),
    }


def _run_quality_gates() -> dict[str, Any]:
    environment = os.environ.copy()
    environment["PYTHONDONTWRITEBYTECODE"] = "1"
    tests = subprocess.run(
        [sys.executable, "-B", "-m", "unittest", "discover", "-s", "tests", "-v"],
        cwd=PROJECT_ROOT,
        env=environment,
        capture_output=True,
        text=True,
        timeout=180,
        check=False,
    )
    test_output = f"{tests.stdout}\n{tests.stderr}"
    count_match = re.search(r"Ran (\d+) tests?", test_output)
    test_count = int(count_match.group(1)) if count_match else 0

    smoke = subprocess.run(
        [sys.executable, "-B", "scripts/finalisma-smoke.py"],
        cwd=PROJECT_ROOT,
        env=environment,
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )
    smoke_output = f"{smoke.stdout}\n{smoke.stderr}"
    try:
        smoke_json = json.loads(smoke.stdout)
    except json.JSONDecodeError:
        smoke_json = {}

    leaked = bool(RAW_CREDENTIAL_PATTERN.search(test_output) or RAW_CREDENTIAL_PATTERN.search(smoke_output))
    return {
        "tests": {
            "passed": tests.returncode == 0 and test_count >= EXPECTED_EXISTING_TESTS,
            "count": test_count,
            "returncode": tests.returncode,
        },
        "smoke": {
            "passed": smoke.returncode == 0
            and smoke_json.get("status") == "ok"
            and smoke_json.get("raw_credentials_printed") is False,
            "returncode": smoke.returncode,
            "status": smoke_json.get("status"),
        },
        "credential_output": {"passed": not leaked},
    }


def _safe_print(payload: dict[str, Any], secrets: list[str]) -> None:
    output = json.dumps(payload, indent=2, sort_keys=True)
    if any(secret and secret in output for secret in secrets) or RAW_CREDENTIAL_PATTERN.search(output):
        raise RuntimeError("refusing to print evaluator output containing a raw credential")
    print(output)


def _capture_baseline(path: Path, runs: int, force: bool) -> int:
    if path.exists() and not force:
        raise FileExistsError(f"baseline already exists: {path}")
    benchmark, secrets = _benchmark(runs)
    baseline = {
        "schema": "finalisma.performance-baseline/v1",
        "harness_version": HARNESS_VERSION,
        "harness_sha256": _harness_digest(),
        "captured_at_epoch": int(time.time()),
        "environment": _environment(),
        "target_improvement_percent": TARGET_IMPROVEMENT_PERCENT,
        "max_p95_regression_percent": MAX_P95_REGRESSION_PERCENT,
        "benchmark": benchmark,
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(baseline, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    _safe_print(
        {
            "status": "BASELINE_CAPTURED",
            "baseline": str(path.relative_to(PROJECT_ROOT)),
            "weighted_median_ms": benchmark["weighted_median_ms"],
            "weighted_p95_ms": benchmark["weighted_p95_ms"],
            "semantic_digests": {
                name: result["semantic_digest"] for name, result in benchmark["scenarios"].items()
            },
        },
        secrets,
    )
    return 0


def _evaluate(path: Path, runs: int) -> int:
    if not path.exists():
        raise FileNotFoundError(
            f"locked baseline missing: {path}; capture it before optimization with --capture-baseline"
        )
    baseline = json.loads(path.read_text(encoding="utf-8"))
    failures: list[str] = []
    if baseline.get("schema") != "finalisma.performance-baseline/v1":
        failures.append("unsupported baseline schema")
    if baseline.get("harness_version") != HARNESS_VERSION:
        failures.append("harness version differs from baseline")
    if baseline.get("harness_sha256") != _harness_digest():
        failures.append("benchmark harness changed after baseline capture")
    if baseline.get("environment") != _environment():
        failures.append("runtime environment differs from baseline")
    if baseline.get("benchmark", {}).get("runs") != runs:
        failures.append("run count differs from baseline")

    current, secrets = _benchmark(runs)
    baseline_benchmark = baseline["benchmark"]
    baseline_median = float(baseline_benchmark["weighted_median_ms"])
    current_median = float(current["weighted_median_ms"])
    improvement = -_percent_change(baseline_median, current_median)
    if improvement < TARGET_IMPROVEMENT_PERCENT:
        failures.append(
            f"weighted median improvement {improvement:.2f}% is below {TARGET_IMPROVEMENT_PERCENT:.2f}%"
        )

    comparisons: dict[str, Any] = {}
    for name in SCENARIO_WEIGHTS:
        before = baseline_benchmark["scenarios"][name]
        after = current["scenarios"][name]
        median_change = _percent_change(float(before["median_ms"]), float(after["median_ms"]))
        p95_change = _percent_change(float(before["p95_ms"]), float(after["p95_ms"]))
        digest_matches = before["semantic_digest"] == after["semantic_digest"]
        comparisons[name] = {
            "median_change_percent": round(median_change, 2),
            "p95_change_percent": round(p95_change, 2),
            "semantic_digest_matches": digest_matches,
        }
        if p95_change > MAX_P95_REGRESSION_PERCENT:
            failures.append(f"{name} p95 regressed by {p95_change:.2f}%")
        if not digest_matches:
            failures.append(f"{name} semantic digest changed")

    gates = _run_quality_gates()
    if not gates["tests"]["passed"]:
        failures.append("unit/site test gate failed")
    if not gates["smoke"]["passed"]:
        failures.append("protocol smoke gate failed")
    if not gates["credential_output"]["passed"]:
        failures.append("raw credential detected in gate output")

    payload = {
        "status": "PASS" if not failures else "FAIL",
        "baseline": {
            "weighted_median_ms": baseline_median,
            "weighted_p95_ms": baseline_benchmark["weighted_p95_ms"],
        },
        "current": {
            "weighted_median_ms": current_median,
            "weighted_p95_ms": current["weighted_p95_ms"],
        },
        "weighted_median_improvement_percent": round(improvement, 2),
        "scenarios": comparisons,
        "quality_gates": gates,
        "failures": failures,
    }
    _safe_print(payload, secrets)
    return 0 if not failures else 1


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--baseline", type=Path, required=True)
    parser.add_argument("--runs", type=int, default=7, choices=range(3, 21), metavar="3-20")
    parser.add_argument("--capture-baseline", action="store_true")
    parser.add_argument("--force", action="store_true", help="replace an existing baseline")
    return parser


def main() -> int:
    arguments = _parser().parse_args()
    baseline = arguments.baseline
    if not baseline.is_absolute():
        baseline = PROJECT_ROOT / baseline
    if arguments.capture_baseline:
        return _capture_baseline(baseline, arguments.runs, arguments.force)
    return _evaluate(baseline, arguments.runs)


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (FileExistsError, FileNotFoundError, RuntimeError, AssertionError, KeyError) as exc:
        print(json.dumps({"status": "ERROR", "error": str(exc)}, indent=2), file=sys.stderr)
        raise SystemExit(2) from exc
