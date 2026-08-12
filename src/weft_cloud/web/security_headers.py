"""Security response headers shared by every browser-facing surface.

Single source of truth for the header block so the web app
(``weft_cloud.web.app``), the standalone marketing-site server
(``scripts/weft-site.py``) and the cloud service's human-facing ``/j/`` page
cannot drift. Standard library only — this ships inside the coordinator's
"zero runtime dependencies" promise.

The Content-Security-Policy is DERIVED from what the shipped pages actually
load, not pasted from a template. Measured against the committed ``site/``
bundle (2026-08-11):

- ``script-src 'self' 'unsafe-inline'`` — the static marketing pages are
  Astro-built with inline ``<script type=\"module\">`` blocks (hero canvas
  init, mobile nav, demo interactivity, scroll reveal) plus same-origin
  ``/_astro/*.js`` modules. Static files cannot carry nonces, so inline
  script is the forced relaxation.
- ``style-src 'self' 'unsafe-inline'`` — the marketing pages use inline
  ``style=\"--delay:…\"`` attributes on scroll-reveal elements, plus
  same-origin stylesheets (``styles.css`` / ``demo.css`` /
  ``/_astro/*.css``).
- ``img-src 'self' data:`` — ``site/styles.css`` sets a
  ``url(\"data:image/svg+xml,…\")`` noise background used by the
  guide/blog/legal pages and ``demo.html``.
- ``font-src 'self'`` — ``@font-face`` loads ``/assets/fonts/*.woff2``
  (self-hosted, no CDN).
- ``media-src 'self'`` — ``demo.html`` embeds ``/assets/weft-demo.*`` video.
- ``connect-src 'self'`` — ``demo-stage.js`` fetches
  ``assets/demo-transcript.json`` (same-origin).
- ``manifest-src 'self'`` — ``demo.html`` links ``site.webmanifest``.
- Everything else is ``'none'``: no third-party origins, no frames, no
  objects anywhere in the bundle.

No ``upgrade-insecure-requests``: every subresource is a same-origin relative
URL, and self-hosters may serve the static bundle over plain http, where that
directive would rewrite subresources to https and break them. HSTS is safe to
send regardless (browsers only honour it over https).
"""

from __future__ import annotations

HTML_CSP = (
    "default-src 'none'; "
    "script-src 'self' 'unsafe-inline'; "
    "style-src 'self' 'unsafe-inline'; "
    "img-src 'self' data:; "
    "font-src 'self'; "
    "media-src 'self'; "
    "connect-src 'self'; "
    "manifest-src 'self'; "
    "object-src 'none'; "
    "base-uri 'self'; "
    "form-action 'self'; "
    "frame-src 'none'; "
    "frame-ancestors 'none'"
)

# Headers sent on every response, whatever the content type. HSTS is ignored
# by browsers over plain http, so an http-only self-host keeps working while
# an https deployment gets the upgrade guarantee.
ALWAYS_HEADERS: tuple[tuple[str, str], ...] = (
    ("Strict-Transport-Security", "max-age=31536000; includeSubDomains"),
    ("X-Content-Type-Options", "nosniff"),
    # no-referrer, not same-origin: a room join link is /j/rm_<token> and that
    # token IS a bearer credential — whoever holds it can join the room. It
    # lives in the URL. Without this header, following any external link from
    # a page that shows the link puts the full URL, token included, into the
    # Referer sent to a third party. That is a credential leak.
    ("Referrer-Policy", "no-referrer"),
    ("X-Frame-Options", "DENY"),
    (
        "Permissions-Policy",
        # Deliberately NOT clipboard-write: the marketing pages copy room
        # links / briefs with navigator.clipboard.writeText, whose default
        # allowlist already grants self-origin.
        "geolocation=(), microphone=(), camera=()",
    ),
)


def security_headers(*, html: bool = False) -> list[tuple[str, str]]:
    """Return the header block for one response.

    ``html=True`` also emits the Content-Security-Policy; CSP is meaningless
    on JSON/asset responses and is scoped to HTML responses on purpose.
    """
    headers: list[tuple[str, str]] = list(ALWAYS_HEADERS)
    if html:
        headers.append(("Content-Security-Policy", HTML_CSP))
    return headers
