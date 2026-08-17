"""Build a deployment-ready Weft site from the truthful local source.

The source bundle carries the documented marketing origin (canonical/OG URLs,
robots Sitemap line, committed sitemap.xml) but no invented placeholder domain
or founder address. This command accepts the deployment-owned origin and
contact values, rewrites the canonical/OG URLs and robots Sitemap line to the
deployment origin, and writes an isolated release bundle under artifacts/ by
default.
"""

from __future__ import annotations

import argparse
import hashlib
import html
import json
import re
import shutil
from pathlib import Path
from urllib.parse import urljoin, urlsplit, urlunsplit


ROOT = Path(__file__).resolve().parents[1]
SOURCE = ROOT / "site"
DEFAULT_OUTPUT = ROOT / "artifacts" / "release-site"
CONTACT_MARKER = "<!-- WEFT_DEPLOY_CONTACT -->"
NOINDEX_PATTERN = re.compile(r'<meta\s+name="robots"\s+content="[^"]*noindex', re.I)
_SOURCE_ORIGIN_HOSTS = {"finalisma.vercel.app"}


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


def _meta_tag(document: str, attribute: str, name: str) -> re.Match[str] | None:
    """Return a matching meta element without depending on attribute order."""

    for match in re.finditer(r"<meta\b[^>]*>", document, flags=re.I):
        attributes = {
            key.lower(): value
            for key, _quote, value in re.findall(
                r"([:\w-]+)\s*=\s*(['\"])(.*?)\2", match.group(0), flags=re.I | re.S
            )
        }
        if attributes.get(attribute.lower(), "").lower() == name.lower():
            return match
    return None


def _meta_content(document: str, attribute: str, name: str) -> str | None:
    match = _meta_tag(document, attribute, name)
    if match is None:
        return None
    content = re.search(r"\bcontent\s*=\s*(['\"])(.*?)\1", match.group(0), flags=re.I | re.S)
    return content.group(2) if content else None


def _ensure_meta(document: str, *, attribute: str, name: str, content: str) -> str:
    """Insert or replace one canonicalized share meta element."""

    markup = f'<meta {attribute}="{name}" content="{html.escape(content, quote=True)}">'
    match = _meta_tag(document, attribute, name)
    if match is None:
        return _insert_before_title(document, markup)
    return document[: match.start()] + markup + document[match.end() :]


def _title_text(document: str) -> str:
    match = re.search(r"<title\b[^>]*>(.*?)</title\s*>", document, flags=re.I | re.S)
    if match is None:
        raise ValueError("HTML page has no title marker")
    return html.unescape(re.sub(r"<[^>]+>", "", match.group(1))).strip()


def _public_url(value: str, *, page_url: str, origin: str) -> str:
    """Resolve local asset URLs against the deployment origin.

    Existing source pages intentionally carry the documented marketing origin
    or page-relative asset paths. Both are local release references and must
    follow the deployment origin; genuinely external URLs remain untouched.
    """

    candidate = html.unescape(value.strip())
    parsed = urlsplit(candidate)
    origin_parts = urlsplit(origin)
    allowed_hosts = {origin_parts.netloc.lower(), *_SOURCE_ORIGIN_HOSTS}
    if parsed.scheme and parsed.scheme not in {"http", "https"}:
        return candidate
    if parsed.netloc and parsed.netloc.lower() not in allowed_hosts:
        return candidate
    resolved = urlsplit(urljoin(page_url, candidate))
    if resolved.scheme not in {"http", "https"} or resolved.netloc.lower() not in allowed_hosts:
        return candidate
    return urlunsplit(
        (
            origin_parts.scheme,
            origin_parts.netloc,
            resolved.path or "/",
            resolved.query,
            resolved.fragment,
        )
    )


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

    title = _title_text(document)
    description = _meta_content(document, "name", "description") or title
    og_title = _meta_content(document, "property", "og:title") or title
    og_description = _meta_content(document, "property", "og:description") or description
    source_image = (
        _meta_content(document, "property", "og:image")
        or _meta_content(document, "name", "twitter:image")
        or "/assets/og-card.png"
    )
    image = _public_url(source_image, page_url=page_url, origin=origin)
    image_alt = _meta_content(document, "property", "og:image:alt") or (
        "Weft coordination product surface"
    )
    document = _ensure_meta(document, attribute="property", name="og:site_name", content="Weft")
    document = _ensure_meta(document, attribute="property", name="og:title", content=og_title)
    document = _ensure_meta(document, attribute="property", name="og:description", content=og_description)
    document = _ensure_meta(document, attribute="property", name="og:image", content=image)
    document = _ensure_meta(document, attribute="property", name="og:image:alt", content=image_alt)
    document = _ensure_meta(document, attribute="name", name="twitter:card", content="summary_large_image")
    document = _ensure_meta(
        document,
        attribute="name",
        name="twitter:title",
        content=_meta_content(document, "name", "twitter:title") or og_title,
    )
    document = _ensure_meta(
        document,
        attribute="name",
        name="twitter:description",
        content=_meta_content(document, "name", "twitter:description") or og_description,
    )
    document = _ensure_meta(
        document,
        attribute="name",
        name="twitter:image",
        content=_public_url(
            _meta_content(document, "name", "twitter:image") or source_image,
            page_url=page_url,
            origin=origin,
        ),
    )
    document = _ensure_meta(
        document,
        attribute="name",
        name="twitter:image:alt",
        content=_meta_content(document, "name", "twitter:image:alt") or image_alt,
    )

    video = _meta_content(document, "property", "og:video")
    if video:
        document = _ensure_meta(
            document,
            attribute="property",
            name="og:video",
            content=_public_url(video, page_url=page_url, origin=origin),
        )

    document = re.sub(
        r'("(?:thumbnailUrl|contentUrl|embedUrl)"\s*:\s*")([^"]+)(")',
        lambda match: f"{match.group(1)}{_public_url(match.group(2), page_url=page_url, origin=origin)}{match.group(3)}",
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
        resolved_output / "assets" / "weft-demo.vtt",
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
