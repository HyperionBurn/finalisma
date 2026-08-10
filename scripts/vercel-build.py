"""Vercel build hook: materialize the release bundle before static deploy.

``vercel.json`` points ``outputDirectory`` at ``artifacts/release-site`` and
runs this script as ``buildCommand``. Raw ``site/`` is the source of truth but
deliberately contains no public origin or founder address; this hook injects
those two deployment-owned values through ``build-site-release.py`` so the
deployed bundle carries the canonical/OG/JSON-LD URLs, the founder contact
CTA, ``sitemap.xml``, the ``robots.txt`` Sitemap line, and
``release-manifest.json`` that a raw ``site/`` deploy silently loses.

Values come from the Vercel project's build environment, not from the repo:

- ``WEFT_SITE_ORIGIN`` — public HTTPS origin (defaults to the documented
  marketing URL ``https://finalisma.vercel.app``).
- ``WEFT_CONTACT_URL`` — REQUIRED, no default. A founder-owned HTTPS contact
  form or ``mailto:`` address. The build FAILS LOUDLY if it is unset: a deploy
  that omits it would ship the raw homepage contact marker instead of a real
  CTA, so we refuse to ship that instead of faking an address.

Python 3.10+ is required on the Vercel build image (the materializer uses
``str | None`` annotations and ``Path.is_relative_to``).
"""

from __future__ import annotations

import importlib.util
import json
import os
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_ORIGIN = "https://finalisma.vercel.app"
OUTPUT = ROOT / "artifacts" / "release-site"


def _load_release_module():
    spec = importlib.util.spec_from_file_location(
        "build_site_release", ROOT / "scripts" / "build-site-release.py"
    )
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def build_release_for_deploy(origin: str | None = None, contact_url: str | None = None) -> dict[str, object]:
    """Run the release materializer with deployment-owned values.

    Raises ``ValueError`` with an actionable message when ``contact_url`` is
    unset, so a misconfigured Vercel project fails the build instead of
    shipping a site whose contact CTA was never injected.
    """
    resolved_origin = origin or os.environ.get("WEFT_SITE_ORIGIN") or DEFAULT_ORIGIN
    resolved_contact = contact_url if contact_url is not None else os.environ.get("WEFT_CONTACT_URL", "")
    if not resolved_contact:
        raise ValueError(
            "WEFT_CONTACT_URL is unset. Set it in the Vercel project build "
            "environment to a founder-owned HTTPS contact form or mailto: "
            "address so the release materializer can inject the contact CTA. "
            "Refusing to deploy a site without its contact link."
        )
    module = _load_release_module()
    return module.build_release(
        origin=resolved_origin,
        contact_url=resolved_contact,
        output=OUTPUT,
        force=True,
    )


def main(argv: list[str] | None = None) -> int:
    try:
        result = build_release_for_deploy()
    except ValueError as error:
        print(json.dumps({"status": "ERROR", "error": str(error)}, indent=2), file=sys.stderr)
        return 1
    print(json.dumps({"status": "RELEASE_MATERIALIZED", "output": str(OUTPUT.relative_to(ROOT)), **result}, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
