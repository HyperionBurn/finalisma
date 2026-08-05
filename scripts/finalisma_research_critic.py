#!/usr/bin/env python3
"""All-or-nothing structural critic for the Finalisma interoperability research."""

from __future__ import annotations

import argparse
import hashlib
import json
import platform
import re
import subprocess
import sys
from collections import Counter
from pathlib import Path
from urllib.parse import urlparse


ALLOWED_STATUSES = {
    "verified",
    "documented-unverified",
    "adapter-required",
    "unsupported",
}
PRIMARY_DOMAINS = {
    "a2a-protocol.org",
    "agentclientprotocol.com",
    "cloud.google.com",
    "code.claude.com",
    "code.visualstudio.com",
    "cursor.com",
    "developers.googleblog.com",
    "developers.openai.com",
    "docs.ag-ui.com",
    "docs.cline.bot",
    "docs.crewai.com",
    "docs.github.com",
    "google-gemini.github.io",
    "help.openai.com",
    "microsoft.github.io",
    "modelcontextprotocol.io",
    "openai.github.io",
    "opencode.ai",
    "reference.langchain.com",
    "zed.dev",
}


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def add_check(checks: list[dict[str, object]], name: str, passed: bool, detail: str) -> None:
    checks.append({"name": name, "passed": bool(passed), "detail": detail})


def git_revision(root: Path) -> str | None:
    try:
        result = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=root,
            check=True,
            capture_output=True,
            text=True,
            timeout=5,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    return result.stdout.strip() or None


def extract_markdown_urls(text: str) -> list[str]:
    return re.findall(r"\[[^\]]+\]\((https://[^)]+)\)", text)


def count_numbered_table_rows(section: str) -> int:
    return len(re.findall(r"^\|\s*\d+\s*\|", section, flags=re.MULTILINE))


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--report", required=True, type=Path)
    parser.add_argument("--matrix", required=True, type=Path)
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    root = Path.cwd().resolve()
    report_path = args.report.resolve()
    matrix_path = args.matrix.resolve()
    checks: list[dict[str, object]] = []
    errors: list[str] = []
    warnings: list[str] = []

    for label, path in (("report", report_path), ("matrix", matrix_path)):
        present = path.is_file()
        add_check(checks, f"artifact.{label}.exists", present, str(path))
        if not present:
            errors.append(f"Missing {label}: {path}")

    if errors:
        print(json.dumps({"verdict": "fail", "errors": errors, "checks": checks}, indent=2))
        return 1

    report = report_path.read_text(encoding="utf-8")
    try:
        matrix = json.loads(matrix_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        errors.append(f"Matrix JSON is invalid: {exc}")
        matrix = {}

    hosts = matrix.get("hosts", []) if isinstance(matrix, dict) else []
    summary = matrix.get("summary", {}) if isinstance(matrix, dict) else {}
    status_counts = Counter(
        host.get("status") for host in hosts if isinstance(host, dict)
    )

    schema_ok = matrix.get("schema_version") == "1.0"
    add_check(checks, "matrix.schema", schema_ok, str(matrix.get("schema_version")))
    if not schema_ok:
        errors.append("Matrix schema_version must be 1.0.")

    host_count_ok = isinstance(hosts, list) and len(hosts) >= 8
    add_check(checks, "rubric.1.host_surfaces", host_count_ok, f"hosts={len(hosts)}")
    if not host_count_ok:
        errors.append("At least eight host surfaces are required.")

    ids = [host.get("id") for host in hosts if isinstance(host, dict)]
    unique_ids_ok = len(ids) == len(set(ids)) and all(isinstance(value, str) and value for value in ids)
    add_check(checks, "matrix.unique_host_ids", unique_ids_ok, f"ids={len(ids)}")
    if not unique_ids_ok:
        errors.append("Host IDs must be non-empty and unique.")

    required_host_fields = {
        "id",
        "name",
        "vendor",
        "surfaces",
        "official_docs",
        "accessed_at",
        "product_version_scope",
        "documented_transport_modes",
        "configuration_surface",
        "documented_auth",
        "finalisma_path",
        "status",
        "tested_in_this_repo",
        "blocking_caveat",
        "next_verification",
    }
    host_field_errors: list[str] = []
    for host in hosts:
        if not isinstance(host, dict):
            host_field_errors.append("non-object host row")
            continue
        missing = sorted(required_host_fields - set(host))
        if missing:
            host_field_errors.append(f"{host.get('id', '<unknown>')}: missing {', '.join(missing)}")
        if host.get("accessed_at") != "2026-07-30":
            host_field_errors.append(f"{host.get('id', '<unknown>')}: stale/missing accessed_at")
        parsed = urlparse(str(host.get("official_docs", "")))
        if parsed.scheme != "https" or parsed.hostname not in PRIMARY_DOMAINS:
            host_field_errors.append(f"{host.get('id', '<unknown>')}: non-primary official_docs")
        status = host.get("status")
        if status not in ALLOWED_STATUSES:
            host_field_errors.append(f"{host.get('id', '<unknown>')}: invalid status {status!r}")
        if not isinstance(host.get("tested_in_this_repo"), bool):
            host_field_errors.append(f"{host.get('id', '<unknown>')}: tested_in_this_repo must be boolean")
        if status == "verified":
            if host.get("tested_in_this_repo") is not True or not host.get("evidence_artifact"):
                host_field_errors.append(
                    f"{host.get('id', '<unknown>')}: verified requires tested_in_this_repo=true and evidence_artifact"
                )
        if not host.get("documented_transport_modes") or not host.get("documented_auth"):
            host_field_errors.append(f"{host.get('id', '<unknown>')}: transport/auth evidence is empty")

    host_fields_ok = not host_field_errors
    add_check(checks, "rubric.5.machine_readable_host_matrix", host_fields_ok, "; ".join(host_field_errors) or "all host rows complete")
    errors.extend(host_field_errors)

    totals_ok = summary.get("host_count") == len(hosts)
    for status in ALLOWED_STATUSES:
        totals_ok = totals_ok and summary.get(status) == status_counts.get(status, 0)
    add_check(checks, "matrix.status_totals", totals_ok, f"summary={summary}; actual={dict(status_counts)}")
    if not totals_ok:
        errors.append("Matrix host_count or status totals do not match host rows.")

    report_count_match = re.search(r"Host matrix count:\s*(\d+)", report)
    report_totals_match = re.search(
        r"Evidence status totals:\s*verified=(\d+);\s*documented-unverified=(\d+);\s*adapter-required=(\d+);\s*unsupported=(\d+)\.",
        report,
    )
    report_matrix_ok = bool(report_count_match and int(report_count_match.group(1)) == len(hosts))
    if report_totals_match:
        expected = [
            status_counts.get("verified", 0),
            status_counts.get("documented-unverified", 0),
            status_counts.get("adapter-required", 0),
            status_counts.get("unsupported", 0),
        ]
        report_matrix_ok = report_matrix_ok and [int(value) for value in report_totals_match.groups()] == expected
    else:
        report_matrix_ok = False
    add_check(checks, "report.matrix_agreement", report_matrix_ok, "report count/status markers versus JSON")
    if not report_matrix_ok:
        errors.append("Report and matrix host counts/status totals disagree.")

    competitor_section_match = re.search(
        r"## Competitor and alternative map(?P<body>.*?)(?:\n## Repository gap map)",
        report,
        flags=re.DOTALL,
    )
    competitor_count = count_numbered_table_rows(competitor_section_match.group("body")) if competitor_section_match else 0
    competitors_ok = competitor_count >= 8
    add_check(checks, "rubric.2.competitor_map", competitors_ok, f"systems={competitor_count}")
    if not competitors_ok:
        errors.append("Competitor map must contain at least eight numbered systems.")

    selected_wedge_count = report.count("### Selected wedge")
    wedge_terms = [
        "Buyer",
        "Painful trigger",
        "Current workaround",
        "Activation",
        "Retention",
        "Pricing test",
        "Success threshold",
        "Kill criterion",
        "Disconfirming evidence",
    ]
    wedge_ok = (
        selected_wedge_count == 1
        and "Decision: EVIDENCE-BACKED CROSS-HOST INCIDENT-TRIAGE HANDOFF" in report
        and all(term in report for term in wedge_terms)
    )
    add_check(checks, "rubric.3.exactly_one_wedge", wedge_ok, f"selected_headings={selected_wedge_count}")
    if not wedge_ok:
        errors.append("Exactly one fully specified selected wedge is required.")

    contract_terms = [
        "| Discovery |",
        "| Identity |",
        "| Consent |",
        "| Capabilities |",
        "| Tasks |",
        "| Ordered events |",
        "| Idempotency |",
        "| Reconnect |",
        "| Evidence |",
        "| Cancellation |",
        "| Errors |",
    ]
    contract_ok = all(term in report for term in contract_terms) and all(
        term in report for term in ("MCP now", "A2A adapter", "Finalisma-specific")
    )
    add_check(checks, "rubric.4.protocol_contract", contract_ok, "11 required areas and ownership mappings")
    if not contract_ok:
        errors.append("Interop Profile is missing a required contract area or ownership mapping.")

    security_terms = [
        "single-node SQLite",
        "OAuth/OIDC",
        "shared transactional storage",
        "distributed rate limiting",
        "outbox/dead-letter",
        "storage-enforced tenant isolation",
        "TLS",
    ]
    security_ok = all(term.lower() in report.lower() for term in security_terms)
    add_check(checks, "rubric.6.security_and_deployment_truth", security_ok, "single-node and hosted gates")
    if not security_ok:
        errors.append("Security/deployment boundary is incomplete.")

    local_symbol_requirements = {
        root / "src" / "finalisma_mcp" / "core.py": [
            "TASK_STATUSES",
            "def create_task(",
            "def verify_task(",
            "def session_send(",
        ],
        root / "src" / "finalisma_mcp" / "server.py": [
            "class _Metrics",
            "def handle_json_rpc(",
            "def run_http(",
        ],
        root / "docs" / "SECURITY_GATES.md": [
            "Must pass before multi-instance hosted traffic",
        ],
    }
    local_errors: list[str] = []
    for path, symbols in local_symbol_requirements.items():
        if not path.is_file():
            local_errors.append(f"missing local evidence file {path.relative_to(root)}")
            continue
        content = path.read_text(encoding="utf-8")
        for symbol in symbols:
            if symbol not in content:
                local_errors.append(f"{path.relative_to(root)} missing {symbol}")
    gap_ok = (
        not local_errors
        and "### P0:" in report
        and "### P1:" in report
        and "### P2:" in report
        and "src/finalisma_mcp/core.py" in report
        and "src/finalisma_mcp/server.py" in report
        and all(
            symbol in report
            for symbol in (
                "PROTOCOL_NAME",
                "MODEL_SLOTS",
                "join_pairing()",
                "verify_task()",
                "TASK_STATUSES",
                "session_send()",
                "handle_json_rpc()",
                "FinalismaDispatcher._apply_team_scope()",
                "_Metrics",
            )
        )
    )
    add_check(checks, "rubric.7.repo_gap_map", gap_ok, "; ".join(local_errors) or "P0/P1/P2 and symbols grounded")
    errors.extend(local_errors)
    if not gap_ok and not local_errors:
        errors.append("Repository gap map is missing priority tiers or local code references.")

    day_rows = len(re.findall(r"^\|\s*\d{1,2}\s*\|", re.search(
        r"## Fourteen-day launch experiment(?P<body>.*?)(?:\n## Claim ledger)", report, flags=re.DOTALL
    ).group("body"), flags=re.MULTILINE)) if "## Fourteen-day launch experiment" in report else 0
    experiment_terms = [
        "$500",
        "$1,000",
        "6 of 8",
        "3 repeat",
        "2 pay",
        "link_created",
        "second_handoff_completed",
        "deposit_committed",
        "YC evidence memo",
        "Product Hunt package",
        "Proof bundle",
        "Design-partner case note",
        "Decision record",
    ]
    experiment_ok = day_rows == 14 and all(term in report for term in experiment_terms)
    add_check(checks, "rubric.8.fourteen_day_experiment", experiment_ok, f"day_rows={day_rows}")
    if not experiment_ok:
        errors.append("14-day experiment, thresholds, pricing, or instrumentation is incomplete.")

    urls = extract_markdown_urls(report)
    primary_urls = [url for url in urls if urlparse(url).hostname in PRIMARY_DOMAINS]
    source_ok = len(set(primary_urls)) >= 15 and all(url.startswith("https://") for url in urls)
    add_check(checks, "rubric.9.primary_source_quality", source_ok, f"unique_primary_urls={len(set(primary_urls))}")
    if not source_ok:
        errors.append("Report needs at least 15 distinct direct links on approved primary domains.")

    placeholder_hits = re.findall(r"\[FILL\]|\bTBD\b|\bTODO\b", report, flags=re.IGNORECASE)
    banned_hits = re.findall(
        r"literally anything|works with any two agents|a universal installer",
        report,
        flags=re.IGNORECASE,
    )
    limitations_ok = "## Limitations and open falsifiers" in report and len(re.findall(r"^\d+\.", report, flags=re.MULTILINE)) >= 10
    reproducibility_ok = (
        not placeholder_hits
        and not banned_hits
        and limitations_ok
        and "scripts/finalisma_research_critic.py" in report
        and "research/interop-matrix.json" in report
    )
    add_check(
        checks,
        "rubric.10.reproducibility_and_limitations",
        reproducibility_ok,
        f"placeholders={placeholder_hits}; banned={banned_hits}; limitations={limitations_ok}",
    )
    if placeholder_hits:
        errors.append(f"Placeholder markers found: {placeholder_hits}")
    if banned_hits:
        errors.append(f"Unsupported absolute compatibility phrases found: {banned_hits}")
    if not limitations_ok:
        errors.append("Explicit limitations/falsifiers are incomplete.")

    if status_counts.get("verified", 0) == 0:
        warnings.append("No host is verified; launch compatibility remains a documented hypothesis.")
    warnings.append("A structural critic cannot replace live host runs, customer evidence, or independent protocol conformance testing.")

    failed_checks = [check["name"] for check in checks if not check["passed"]]
    verdict = "pass" if not errors and not failed_checks else "fail"
    result = {
        "verdict": verdict,
        "rubric_dimensions_passed": 10 - sum(
            1 for check in checks if str(check["name"]).startswith("rubric.") and not check["passed"]
        ),
        "rubric_dimensions_required": 10,
        "errors": errors,
        "warnings": warnings,
        "metrics": {
            "host_count": len(hosts),
            "status_counts": {status: status_counts.get(status, 0) for status in sorted(ALLOWED_STATUSES)},
            "competitor_count": competitor_count,
            "fourteen_day_rows": day_rows,
            "unique_primary_source_urls": len(set(primary_urls)),
        },
        "artifacts": {
            "report": {"path": str(report_path), "sha256": sha256_file(report_path)},
            "matrix": {"path": str(matrix_path), "sha256": sha256_file(matrix_path)},
        },
        "environment": {
            "python": platform.python_version(),
            "platform": platform.platform(),
            "repository_revision": git_revision(root),
        },
        "checks": checks,
    }
    print(json.dumps(result, indent=2, sort_keys=True))
    return 0 if verdict == "pass" else 1


if __name__ == "__main__":
    raise SystemExit(main())
