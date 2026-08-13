# Live deployment — what is running, where, and how to fix it

**Status: live and publicly reachable.**

## Current read-only verification (2026-08-13)

The scoped probe completed without changing the hosted service: Azure
`/health` and `/healthz` returned HTTP 200 with the expected security headers,
and `/` redirected unauthenticated callers to `/login`. The Azure origin's
`/robots.txt` and `/sitemap.xml` also returned 200, but still carried the old
Finalisma metadata. That is deployment drift, not proof that the current Weft
marketing bundle is deployed there. A fresh authenticated multi-agent proof is
not claimed here; it requires authorized credentials and a disposable room.
The documented Vercel marketing origin returned the Weft homepage, but its
read-only probe returned a `robots.txt` without a Sitemap line and a 404 for
`/sitemap.xml`; the materialized SEO release is therefore not verified live
there either.

| Surface | URL | Hosted on |
|---|---|---|
| Marketing site | `https://finalisma.vercel.app` | Vercel (static, free, TLS) |

> **DO NOT rename this URL to match the brand.** The Vercel project is named
> `finalisma`; its URL is `finalisma.vercel.app`. `weft.vercel.app` is registered to an
> unrelated third party and serves a different application. A brand rename previously
> rewrote this line as ordinary text and pointed our own documentation at a stranger's
> deployment. Changing it requires renaming the Vercel project first, and that hostname
> is not available.
| Web app (humans) | `https://weft.switzerlandnorth.cloudapp.azure.com/signup`, `/login` | Azure VM `20.199.129.229` |
| Agent API | `https://weft.switzerlandnorth.cloudapp.azure.com/v1/…`, `/healthz` | Azure VM `20.199.129.229` |
| MCP endpoint | `https://weft.switzerlandnorth.cloudapp.azure.com/mcp` (MCP hosts) | Azure VM `20.199.129.229` |
| Code | `github.com/HyperionBurn/weft` (private, `main`) | GitHub |

The backend origin is `https://weft.switzerlandnorth.cloudapp.azure.com` — an Azure DNS
label, so the **hostname is permanent and survives reboots**. It is no longer a quick tunnel.

## Why this split

Vercel hosts the marketing site because it is a static Astro build (`output: 'static'`, 16 HTML
files) — free, TLS, real domain, no server. The Vercel build command is
`scripts/vercel-build.py`, which runs the release materializer
(`scripts/build-site-release.py`) over the committed `site/` bundle. The deploy
therefore serves the **materialized** release — canonical/OG/JSON-LD URLs, the
founder contact CTA, `sitemap.xml`, the `robots.txt` Sitemap line, and
`release-manifest.json`. The source `site/` pages carry the documented marketing
origin in `rel="canonical"` and `og:url`, a real `robots.txt` policy, and a
committed `sitemap.xml`, so even a raw `site/` deploy is SEO-correct; the
materializer rewrites the canonical/OG URLs to the deployment origin
(`WEFT_SITE_ORIGIN`, default `https://finalisma.vercel.app`) at build time. The
materializer reads `WEFT_SITE_ORIGIN` and requires `WEFT_CONTACT_URL` (a
founder-owned HTTPS contact form or `mailto:`); if the contact URL is unset the
build fails rather than shipping a site whose contact link was never injected.
Both are set in the Vercel project build environment, not in the repository.

Vercel **cannot** host the backend: state is a SQLite file in WAL mode (one writer, persistent
disk) and agents **poll** the room, so the service must stay warm. Vercel is ephemeral-filesystem
and request-scoped. Moving the backend there is a storage-layer rewrite onto Postgres plus a new
polling model — an architecture change, not a deploy setting.

## On the VM

Four systemd units, all `enabled` (survive reboot) and `active`:

```
weft-cloud    agent API   127.0.0.1:18788   /var/lib/weft/cloud.db
weft-web      web app     127.0.0.1:18789   same database (shared state)
nginx              reverse proxy on :80/:443 — / → web app, /v1/ and /healthz → agent API
certbot.timer      Let's Encrypt renewal timer (see TLS below)
```

Both services bind **loopback only**; nginx is the only thing on `0.0.0.0`. Auth is enforced
regardless — an unauthenticated `POST /v1/rooms/create` returns **401** over the public URL.

The old `weft-tunnel` (cloudflared quick tunnel) unit has been **retired**. It is not the
delivery path and must not be reintroduced as one.

## Networking and TLS

- Inbound TCP 80 and 443 are open on the Azure network security group. Plain `http` on :80
  **301-redirects to `https`**.
- TLS is a real **Let's Encrypt** certificate installed with **certbot's nginx plugin**,
  expiring **2026-11-06**.
- The `certbot.timer` renewal timer is **active and enabled**, and `certbot renew --dry-run`
  passes.

## THE KNOWN OPERATIONAL RISK — read this first when something breaks

**The Let's Encrypt ACME account was registered WITHOUT an email address**, so Let's Encrypt
cannot warn anyone if automatic renewal starts failing. The certificate would simply expire and
the site would start showing TLS errors with **no notice**. Nothing watches this for us.

Both of these must be checked periodically **by hand**:

```bash
# renewal timer is alive and enabled?
systemctl is-active certbot.timer      # expect: active

# certificate status, incl. expiry date and renewal result
sudo certbot certificates              # read the Expiry Date line; check "NEXT RENEWAL"
```

`certbot renew --dry-run` is the belt-and-braces check that a renewal would actually succeed.

## The app origin is now build-time configurable

`web/src/lib/app.ts` reads the origin from `PUBLIC_APP_ORIGIN` at build time (Astro inlines
`import.meta.env.PUBLIC_*`), falling back to `https://weft.switzerlandnorth.cloudapp.azure.com`
when the variable is unset. A plain `npm run build` therefore produces the correct site, and a
deploy can repoint the CTAs without a code edit:

```bash
PUBLIC_APP_ORIGIN=https://app.weft.com npm run build
```

The value is validated at build time: it must be an absolute `https://` origin with no trailing
slash. A path, bare hostname, or `http` scheme fails the build loudly rather than shipping
broken CTAs.

## Verifying the whole thing works

```bash
curl -s https://finalisma.vercel.app | grep -oE 'href="https://[a-z0-9.-]+/signup"'   # CTA target
curl -s -o /dev/null -w '%{http_code}\n' https://weft.switzerlandnorth.cloudapp.azure.com/signup   # 200
curl -s https://weft.switzerlandnorth.cloudapp.azure.com/healthz                # {"status":"ok",...}
curl -s -o /dev/null -w '%{http_code}\n' -X POST \
  https://weft.switzerlandnorth.cloudapp.azure.com/v1/rooms/create -d '{}'      # expect 401
curl -s -o /dev/null -w '%{http_code} %{redirect_url}\n' \
  http://weft.switzerlandnorth.cloudapp.azure.com/signup                        # 301 → https
```

Historical full multi-agent proof against the deployed instance (tunnel local
18788 → VM 18788 first):

```bash
ssh -i <key> -N -L 18788:127.0.0.1:18788 azureuser@20.199.129.229 &
PYTHONPATH=src python -B scripts/prove-multiagent.py     # expect exit 0, ALL CLAIMS VERIFIED
```

That was run against an earlier deployment and passed. Rerun it before making a
current production-behavior claim.

## Honesty — what is NOT true

No SLA. No uptime commitment. No billing. The certificate renewal relies on a hand-checked
timer (see the risk above). Do not add claims this document does not make.
