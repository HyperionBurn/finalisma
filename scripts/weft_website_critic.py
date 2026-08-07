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
        "One incident. Two agents. One account of what happened.",
        "MCP — the open protocol",
        "Verified host (OpenCode 1.18.13)",
        "data-cohort-form",
        "65",
        "Watch the 42-second proof",
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

    expected_top = {
        "oneH1": True,
        "initialStoryCtaHidden": True,
        "ruleFixed": True,
        "allUnpostedLabelled": True,
        "formLabels": True,
        "htmlHasJsClass": True,
    }
    for key, expected in expected_top.items():
        if top.get(key) is not expected:
            failures.append(f"rendered top-level check failed: {key}")
    if float(top.get("maxRuleDrift", 999)) > 1.5:
        failures.append("ledger split drift exceeds 1.5 CSS pixels")
    if top.get("thirdParty"):
        failures.append("homepage loaded a third-party runtime resource")

    scroll = interactions.get("scrollChecks", {}) if isinstance(interactions, dict) else {}
    for key in ("allPinned", "advances", "reachesDone", "planeHasDepth", "entriesFitBody"):
        if scroll.get(key) is not True:
            failures.append(f"desktop reconciliation check failed: {key}")
    for family in ("reducedMotionChecks", "noJsChecks", "noJsMobileChecks", "supportingPageChecks"):
        value = interactions.get(family, {}) if isinstance(interactions, dict) else {}
        if not value:
            failures.append(f"missing rendered evidence family: {family}")

    mobile = interactions.get("mobileResults", []) if isinstance(interactions, dict) else []
    if len(mobile) != 3:
        failures.append("expected three mobile viewport results")
    else:
        for result in mobile:
            if result.get("documentWidth") != result.get("viewportWidth"):
                failures.append(f"horizontal overflow at {result.get('viewport')}")
            for key in (
                "storyStatic",
                "storyTrackCompact",
                "storyContentVisible",
                "planeFlat",
                "openingPrimaryCtaInFirstFold",
            ):
                if result.get(key) is not True:
                    failures.append(f"mobile check failed at {result.get('viewport')}: {key}")
        if mobile[0].get("tapStoryBalanced") != "balanced":
            failures.append("mobile tap-through story did not balance")

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
    if float(performance.get("cls", 1)) > 0.1:
        failures.append("CLS exceeds 0.1")
    if float(performance.get("lcp", 99999)) > 2500:
        failures.append("local LCP exceeds 2500 ms")
    if int(performance.get("transferBytes", 9_999_999)) > 2_000_000:
        failures.append("homepage transfer exceeds 2 MB")

    if "final result: passed" not in report.lower():
        failures.append("design QA report does not end in a passing result")

    verdict = "fail" if failures else "pass"
    print(json.dumps({"verdict": verdict, "failures": failures}, indent=2))
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
