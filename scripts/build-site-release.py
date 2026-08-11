"""Build a deployment-ready Weft site from the truthful local source.

The source bundle deliberately contains no invented public origin or founder
address. This command accepts those two deployment-owned values and writes an
isolated release bundle under artifacts/ by default.
"""

from __future__ import annotations

import argparse
import hashlib
import html
import json
import re
import shutil
from pathlib import Path
from urllib.parse import urlsplit


ROOT = Path(__file__).resolve().parents[1]
SOURCE = ROOT / "site"
DEFAULT_OUTPUT = ROOT / "artifacts" / "release-site"
CONTACT_MARKER = "<!-- WEFT_DEPLOY_CONTACT -->"
NOINDEX_PATTERN = re.compile(r'<meta\s+name="robots"\s+content="[^"]*noindex', re.I)


def normalize_origin(value: str) -> str:
    parsed = urlsplit(value.strip())
    if parsed.scheme != "https" or not parsed.netloc:
        raise ValueError("origin must be an absolute HTTPS origin")
    if parsed.username or parsed.password or parsed.query or parsed.fragment:
        raise ValueError("origin cannot contain credentials, a query, or a fragment")
    if parsed.path not in ("", "/"):
        raise ValueError("origin must not contain a deployment subpath")
    return f"https://{parsed.netloc}"


def normalize_contact_url(value: str) -> str:
    candidate = value.strip()
    if any(character in candidate for character in ('"', "'", "<", ">", "\r", "\n")):
        raise ValueError("contact URL contains an unsafe character")
    parsed = urlsplit(candidate)
    if parsed.scheme == "https" and parsed.netloc and not parsed.username and not parsed.password:
        return candidate
    if parsed.scheme == "mailto" and "@" in parsed.path and parsed.path.strip():
        return candidate
    raise ValueError("contact URL must be an HTTPS URL or mailto address")


def _public_path(relative: Path) -> str:
    path = relative.as_posix()
    return "/" if path == "index.html" else f"/{path}"


def _insert_before_title(document: str, markup: str) -> str:
    line_match = re.search(r"(?m)^([ \t]*)<title>", document)
    if line_match:
        indent = line_match.group(1)
        insertion = f"{indent}{markup.strip()}\n"
        return document[: line_match.start()] + insertion + document[line_match.start() :]
    if "<title>" in document:
        return document.replace("<title>", f"{markup.strip()}<title>", 1)
    raise ValueError("HTML page has no title marker")


def _rewrite_page(document: str, *, page_url: str, origin: str, contact_url: str | None) -> str:
    canonical = f'  <link rel="canonical" href="{page_url}">'
    if re.search(r'<link\s+rel="canonical"\s+href="[^"]*">', document, re.I):
        document = re.sub(
            r'\s*<link\s+rel="canonical"\s+href="[^"]*">',
            f"\n{canonical}",
            document,
            count=1,
            flags=re.I,
        )
    else:
        document = _insert_before_title(document, canonical)

    og_url = f'  <meta property="og:url" content="{page_url}">'
    if re.search(r'<meta\s+property="og:url"\s+content="[^"]*">', document, re.I):
        document = re.sub(
            r'\s*<meta\s+property="og:url"\s+content="[^"]*">',
            f"\n{og_url}",
            document,
            count=1,
            flags=re.I,
        )
    else:
        document = _insert_before_title(document, og_url)

    if 'property="og:site_name"' not in document:
        document = _insert_before_title(document, '  <meta property="og:site_name" content="Weft">')

    document = re.sub(
        r'(<meta\s+(?:property="og:(?:image|video)"|name="twitter:image")\s+content=")(/[^"]+)(">)',
        lambda match: f"{match.group(1)}{origin}{match.group(2)}{match.group(3)}",
        document,
        flags=re.I,
    )
    document = re.sub(
        r'("(?:thumbnailUrl|contentUrl|embedUrl)"\s*:\s*")(/[^"]+)(")',
        lambda match: f"{match.group(1)}{origin}{match.group(2)}{match.group(3)}",
        document,
    )

    if CONTACT_MARKER in document:
        if not contact_url:
            raise ValueError("homepage contact marker requires a contact URL")
        escaped = html.escape(contact_url, quote=True)
        external = contact_url.startswith("https://")
        attributes = ' target="_blank" rel="noopener noreferrer"' if external else ""
        label = "Open founder contact" if external else "Email the founder"
        link = f'<a class="btn" data-founder-contact href="{escaped}"{attributes}>{label} <span class="glyph" aria-hidden="true">↗</span></a>'
        document = document.replace(CONTACT_MARKER, link, 1)
    return document


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def build_release(*, origin: str, contact_url: str, output: Path, force: bool = False) -> dict[str, object]:
    normalized_origin = normalize_origin(origin)
    normalized_contact = normalize_contact_url(contact_url)
    resolved_output = output.resolve()
    resolved_root = ROOT.resolve()
    resolved_source = SOURCE.resolve()
    if not resolved_output.is_relative_to(resolved_root):
        raise ValueError("output must stay inside the Weft project")
    if resolved_output in (resolved_root, resolved_source) or resolved_source.is_relative_to(resolved_output):
        raise ValueError("output cannot be the project root or contain the source site")
    if resolved_output.exists():
        if not force:
            raise FileExistsError(f"release output already exists: {resolved_output}")
        shutil.rmtree(resolved_output)

    resolved_output.parent.mkdir(parents=True, exist_ok=True)
    shutil.copytree(SOURCE, resolved_output)

    indexed_urls: list[str] = []
    page_count = 0
    for page in sorted(resolved_output.rglob("*.html")):
        relative = page.relative_to(resolved_output)
        document = page.read_text(encoding="utf-8")
        if NOINDEX_PATTERN.search(document):
            continue
        public_url = f"{normalized_origin}{_public_path(relative)}"
        document = _rewrite_page(
            document,
            page_url=public_url,
            origin=normalized_origin,
            contact_url=normalized_contact if relative == Path("index.html") else None,
        )
        page.write_text(document, encoding="utf-8", newline="\n")
        indexed_urls.append(public_url)
        page_count += 1

    sitemap_rows = "\n".join(f"  <url><loc>{html.escape(url)}</loc></url>" for url in indexed_urls)
    sitemap = (
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        '<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">\n'
        f"{sitemap_rows}\n"
        "</urlset>\n"
    )
    (resolved_output / "sitemap.xml").write_text(sitemap, encoding="utf-8", newline="\n")

    robots_path = resolved_output / "robots.txt"
    robots = robots_path.read_text(encoding="utf-8").rstrip()
    sitemap_line = f"Sitemap: {normalized_origin}/sitemap.xml"
    if re.search(r"(?m)^\s*Sitemap:", robots):
        robots = re.sub(r"(?m)^\s*Sitemap:.*$", sitemap_line, robots, count=1)
    else:
        robots = f"{robots}\n{sitemap_line}"
    robots_path.write_text(f"{robots}\n", encoding="utf-8", newline="\n")

    media_paths = [
        resolved_output / "assets" / "weft-demo.mp4",
        resolved_output / "assets" / "weft-demo.webm",
        resolved_output / "assets" / "weft-demo-poster.png",
    ]
    manifest = {
        "schema": "weft.site-release/v1",
        "origin": normalized_origin,
        "contact_scheme": urlsplit(normalized_contact).scheme,
        "page_count": page_count,
        "sitemap_url_count": len(indexed_urls),
        "media_sha256": {path.name: _sha256(path) for path in media_paths},
    }
    (resolved_output / "release-manifest.json").write_text(
        json.dumps(manifest, indent=2) + "\n",
        encoding="utf-8",
        newline="\n",
    )
    return {**manifest, "output": str(resolved_output)}


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Build a deployment-ready Weft static bundle")
    parser.add_argument("--origin", required=True, help="Public HTTPS origin, for example https://weft.example")
    parser.add_argument("--contact-url", required=True, help="Founder-owned HTTPS contact form or mailto URL")
    parser.add_argument("--output", default=str(DEFAULT_OUTPUT), help="Project-local release output directory")
    parser.add_argument("--force", action="store_true", help="Replace an existing release output directory")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        result = build_release(
            origin=args.origin,
            contact_url=args.contact_url,
            output=Path(args.output),
            force=args.force,
        )
    except (FileExistsError, ValueError) as error:
        raise SystemExit(str(error)) from error
    print(json.dumps(result, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
