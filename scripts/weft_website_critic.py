"""Deterministic professor-critic gate for the Weft launch website."""

from __future__ import annotations

import argparse
import json
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SITE = ROOT / "site"


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Audit the Weft website launch evidence")
    parser.add_argument(
        "--qa",
        default=str(ROOT / "artifacts" / "design-qa" / "qa-results.json"),
        help="Path to the rendered-browser QA result JSON",
    )
    parser.add_argument(
        "--report",
        default=str(ROOT / "design-qa.md"),
        help="Path to the human design-QA report",
    )
    return parser


def _as_number(value: object) -> float | None:
    """Coerce a QA measurement to float, or None when it is absent/stale.

    A JSON ``null`` (or a missing key) means the baseline was never measured,
    not that it is zero. Returning ``None`` lets callers emit a distinct
    "cannot compare" failure instead of crashing on ``float(None)``.
    """
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    qa_path = Path(args.qa).resolve()
    report_path = Path(args.report).resolve()
    failures: list[str] = []

    if not qa_path.is_file():
        failures.append(f"missing rendered QA artifact: {qa_path}")
        qa: dict[str, object] = {}
    else:
        qa = json.loads(qa_path.read_text(encoding="utf-8"))

    if not report_path.is_file():
        failures.append(f"missing design QA report: {report_path}")
        report = ""
    else:
        report = report_path.read_text(encoding="utf-8")

    required_pages = [
        SITE / "index.html",
        SITE / "404.html",
        SITE / "license.html",
        SITE / "demo.html",
        SITE / "demo-stage.html",
        SITE / "docs" / "index.html",
        SITE / "docs" / "quickstart.html",
        SITE / "docs" / "protocol.html",
        SITE / "docs" / "security.html",
        SITE / "docs" / "compatibility.html",
        SITE / "blog" / "index.html",
    ]
    for page in required_pages:
        if not page.is_file():
            failures.append(f"missing static launch page: {page.relative_to(ROOT)}")

    home = (SITE / "index.html").read_text(encoding="utf-8")
    for phrase in (
        "One link. Many agents.",
        "Speaks MCP",
        "MCP is a public protocol; these names identify the hosts that speak it",
        "MCP is the tool protocol your hosts already speak",
        "data-cohort-form",
        "single-node",
        "Simulated account · no credentials · no live session",
        'property="og:site_name" content="Weft"',
        'name="twitter:image" content="/assets/og-card.png"',
    ):
        if phrase not in home:
            failures.append(f"landing page missing required launch evidence: {phrase}")
    for unsupported in (
        "verified agent handoff layer",
        "Weft A2A Standard 1.0",
        "universally compatible",
    ):
        if unsupported.lower() in home.lower():
            failures.append(f"landing page contains unsupported claim: {unsupported}")

    top = qa.get("topLevelChecks", {}) if isinstance(qa, dict) else {}
    interactions = qa.get("interactions", {}) if isinstance(qa, dict) else {}
    signals = qa.get("signals", {}) if isinstance(qa, dict) else {}
    performance = qa.get("performanceChecks", {}) if isinstance(qa, dict) else {}

    expected_top = (
        "oneH1",
        "htmlHasJsClass",
        "canvasPresent",
        "canvasWrapPresent",
        "readoutHasLive",
        "gatePresent",
        "generatedLinkNonEmpty",
        "formLabels",
        "revealDefaultVisible",
    )
    for key in expected_top:
        if top.get(key) is not True:
            failures.append(f"rendered top-level check failed: {key}")
    if int(top.get("fallbackRows", 0) or 0) < 5:
        failures.append("rendered top-level check failed: fallbackRows < 5")
    if int(top.get("tierTabsCount", 0) or 0) < 4:
        failures.append("rendered top-level check failed: tierTabsCount < 4")
    if int(top.get("tierPanelsCount", 0) or 0) < 4:
        failures.append("rendered top-level check failed: tierPanelsCount < 4")
    if int(top.get("stepsCount", 0) or 0) < 4:
        failures.append("rendered top-level check failed: stepsCount < 4")
    if int(top.get("unlabelledColourRows", 0) or 0) != 0:
        failures.append("rendered top-level check failed: unlabelledColourRows is non-zero")
    links_with_no_name = _as_number(top.get("linksWithNoName"))
    if links_with_no_name is None:
        failures.append("rendered top-level check failed: linksWithNoName is absent")
    elif links_with_no_name != 0:
        failures.append("rendered top-level check failed: linksWithNoName is non-zero")
    if top.get("thirdParty"):
        failures.append("homepage loaded a third-party runtime resource")

    if not isinstance(interactions, dict):
        failures.append("rendered interaction evidence is missing")
        interactions = {}
    if not interactions.get("linkCopied", "").startswith("weft."):
        failures.append("link-copy interaction did not produce a Weft room link")
    tier = interactions.get("tierSwitched", {})
    if not all((tier.get("httpVisible"), tier.get("stdioHidden"), tier.get("selectedTab") == "true")):
        failures.append("tier-switch interaction did not select Streamable HTTP")
    if "mcp" not in interactions.get("tierCopied", "").lower():
        failures.append("tier-copy interaction did not produce MCP configuration")
    if interactions.get("cohortApplicationPrepared") is not True:
        failures.append("cohort brief interaction did not prepare a brief")
    if interactions.get("noJsCohort", {}).get("leakFree") is not True:
        failures.append("no-JavaScript cohort check did not remain leak-free")
    if interactions.get("gateTriggered") is not True:
        failures.append("evidence-gate refusal interaction was not triggered")
    supporting = interactions.get("supportingPageChecks", {})
    if not supporting or not all(all(checks.values()) for checks in supporting.values()):
        failures.append("supporting-page checks are incomplete or failed")

    mobile = interactions.get("mobileResults", []) if isinstance(interactions, dict) else []
    if len(mobile) != 3:
        failures.append("expected three mobile viewport results")
    else:
        for result in mobile:
            if result.get("documentWidth") != result.get("viewportWidth"):
                failures.append(f"horizontal overflow at {result.get('viewport')}")
            for key in (
                "canvasWrapHasHeight",
                "canvasMounted",
                "fallbackHasEvents",
                "eventsPresent",
                "readoutLive",
                "openingPrimaryCtaInFirstFold",
                "noHorizontalOverflow",
            ):
                if result.get(key) is not True:
                    failures.append(f"mobile check failed at {result.get('viewport')}: {key}")
            if result.get("offenders"):
                failures.append(f"mobile layout has overflow offenders at {result.get('viewport')}")
        if mobile[0].get("navInitiallyInert") is not True:
            failures.append("mobile navigation is not inert while closed")
        if mobile[0].get("navOpen") != "true" or mobile[0].get("navOpenInert") is not False:
            failures.append("mobile navigation did not open interactively")
        if mobile[0].get("navClosed") != "false" or mobile[0].get("navClosedInert") is not True:
            failures.append("mobile navigation did not close back to inert")

    demo = interactions.get("demoPageChecks", {}) if isinstance(interactions, dict) else {}
    for key in (
        "oneH1",
        "boundaryVisible",
        "controls",
        "durationExpected",
        "dimensionsExpected",
        "hasMp4AndWebm",
        "hasEnglishCaptions",
        "posterSet",
        "noHorizontalOverflow",
    ):
        if demo.get(key) is not True:
            failures.append(f"recorded demo check failed: {key}")

    demo_html = (SITE / "demo.html").read_text(encoding="utf-8")
    for marker in (
        '"@type": "VideoObject"',
        'content="/assets/weft-demo.mp4"',
        'content="index,follow,max-video-preview:-1,max-image-preview:large"',
        'property="og:site_name" content="Weft"',
        'name="twitter:image" content="/assets/weft-demo-poster.png"',
    ):
        if marker not in demo_html:
            failures.append(f"recorded demo missing search/share metadata: {marker}")

    video_assets = {
        "MP4": SITE / "assets" / "weft-demo.mp4",
        "WebM": SITE / "assets" / "weft-demo.webm",
        "poster": SITE / "assets" / "weft-demo-poster.png",
        "captions": SITE / "assets" / "weft-demo.vtt",
        "transcript": SITE / "assets" / "demo-transcript.json",
    }
    for label, asset in video_assets.items():
        if not asset.is_file() or asset.stat().st_size == 0:
            failures.append(f"missing recorded-demo asset: {label}")

    for key in ("consoleErrors", "failedRequests", "badResponses"):
        if signals.get(key):
            failures.append(f"browser signal is not empty: {key}")
    accessibility = qa.get("accessibility", {}) if isinstance(qa, dict) else {}
    if accessibility.get("totalAxeViolations") != 0:
        failures.append("accessibility scan reported violations")
    cls = _as_number(performance.get("cls"))
    if cls is None:
        failures.append("CLS baseline is absent (cls is null) — cannot compare")
    elif cls > 0.1:
        failures.append("CLS exceeds 0.1")
    lcp = _as_number(performance.get("lcp"))
    if lcp is None:
        failures.append("local LCP baseline is absent (lcp is null) — cannot compare")
    elif lcp > 2500:
        failures.append("local LCP exceeds 2500 ms")
    transfer_bytes = _as_number(performance.get("transferBytes"))
    if transfer_bytes is None:
        failures.append("transfer baseline is absent (transferBytes is null) — cannot compare")
    elif int(transfer_bytes) > 2_000_000:
        failures.append("homepage transfer exceeds 2 MB")
    long_tasks = performance.get("longTasks", [])
    if any(_as_number(duration) is not None and float(duration) > 200 for duration in long_tasks):
        failures.append("homepage has a long task over 200 ms")

    if "final result: passed" not in report.lower():
        failures.append("design QA report does not end in a passing result")

    verdict = "fail" if failures else "pass"
    print(json.dumps({"verdict": verdict, "failures": failures}, indent=2))
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
